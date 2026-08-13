"""
Reference implementation for the update-strains skill: talks to a Typebot
public REST API (the same unauthenticated API the chat widget itself calls)
to check patient product availability/pricing for *any* cannabis association
that runs its patient bot on Typebot.

Association config (URL/slug/button-text hints) comes from
`associations.json` (or explicit CLI args for an association not yet
registered), and button matching is keyword-based instead of exact-string,
since different associations' bots use different copy for the same intent.

Guardrails (do not relax these when adapting to a new association):
  - REQUEST_DELAY between every API call. Never parallelize. This hits a
    third party's production server with no documented API/ToS for this use.
  - Never proceed into an actual order. By default (`fetch_details=False`),
    this never even clicks a type/product button — the price list is read
    straight off the category screen's text. `fetch_details=True` opts into
    clicking one product at a time for its detail card (text + photo), but
    checks after every single click that the result still looks like a
    preview and not a quantity/payment screen (see `looks_like_unsafe_response`
    and `_drill_into_products`), aborting loudly instead of guessing further
    the moment it doesn't. Never send anything that looks like
    "Continuar"/"Enviar" beyond CPF, notice acknowledgements, and — only in
    `fetch_details` mode — a single product click followed immediately by
    "back".
  - CPF (or any other patient identifier) is sensitive third-party personal
    data: never invent one, never reuse one for a different person's request
    without asking, never log/print/commit it anywhere outside the immediate
    conversation. `associations.json` holds bot *config* only — never add a
    real CPF to it.

Usage:
    python crawler.py <associacao> <cpf>
        # <associacao> = key or label in associations.json (case/accent
        # insensitive), e.g. "abecmed".

    from crawler import load_association, run_availability_check
    assoc = load_association("abecmed")
    report = run_availability_check(assoc, cpf)                    # price list only
    report = run_availability_check(assoc, cpf, fetch_details=True,
                                     image_dir="./imagens")         # + photos/desc per product
"""

import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests

REQUEST_DELAY = 1.5  # seconds between API calls — be polite to a third-party server
ASSOCIATIONS_FILE = Path(__file__).parent / "associations.json"

# Default keyword sets used when an association has no (or incomplete)
# flow_hints yet. Extend these — and/or an association's flow_hints — as new
# bots reveal different phrasing.
DEFAULT_KEYWORDS = {
    "patient_choice": ["paciente"],
    "back": ["voltar"],
    "order_status": ["status", "pedido"],
    "categories": ["flor", "concentrado", "oleo", "extrato"],
    "cannabinoid_types": ["thc", "cbd", "cbg"],
}


def _normalize(text):
    """Lowercase + strip accents, for tolerant keyword matching."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


# ---------------------------------------------------------------------------
# Association registry
# ---------------------------------------------------------------------------

def load_registry():
    return json.loads(ASSOCIATIONS_FILE.read_text(encoding="utf-8"))


def load_association(identifier):
    """Resolve `identifier` (CLI arg) against associations.json by key or
    label, case/accent-insensitive. Returns the association dict (with `key`
    added) or None if not found — caller should then ask the user for the
    bot's URL rather than guessing."""
    registry = load_registry()
    norm_id = _normalize(identifier)
    for key, assoc in registry.items():
        if _normalize(key) == norm_id or _normalize(assoc.get("label", "")) == norm_id:
            return {**assoc, "key": key}
    return None


def save_association(key, label, base_url, typebot_slug, flow_hints=None):
    """Add a newly-discovered association to the registry. Only call this
    after the flow was actually confirmed working for that association, and
    after confirming with the user that base_url is genuinely that
    association's own official bot (this file gets committed/shared)."""
    registry = load_registry()
    registry[key] = {
        "label": label,
        "base_url": base_url,
        "typebot_slug": typebot_slug,
        "verified": time.strftime("%Y-%m-%d"),
        "flow_hints": flow_hints or {},
    }
    ASSOCIATIONS_FILE.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Typebot API plumbing (association-agnostic)
# ---------------------------------------------------------------------------

def _session():
    s = requests.Session()
    s.headers.update({
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    })
    return s


def start_chat(session, base_url, typebot_slug):
    r = session.post(
        f"{base_url}/typebots/{typebot_slug}/startChat",
        json={},
        headers={"referer": base_url.split("/api/")[0] + "/"},
    )
    r.raise_for_status()
    return r.json()


def continue_chat(session, base_url, chat_session_id, message):
    time.sleep(REQUEST_DELAY)
    r = session.post(
        f"{base_url}/sessions/{chat_session_id}/continueChat",
        json={"message": message},
    )
    r.raise_for_status()
    return r.json()


def _leaf_texts(node):
    """Yield every leaf 'text' string under a richText node, depth-first.
    Typebot nests plain paragraph runs one level deep (children[].text), but
    bulleted price lists go two levels deeper (ul > li > lic > runs) — this
    walks arbitrary depth instead of assuming a fixed shape, since a flat
    one-level reader would silently drop list content (e.g. bullet items
    like "Papaya (indoor) - R$ 85.00" live under ul/li/lic)."""
    if not isinstance(node, dict):
        return
    children = node.get("children")
    if children:
        for child in children:
            yield from _leaf_texts(child)
    elif "text" in node:
        yield node["text"]


def extract_text(messages):
    out = []
    for m in messages:
        if m.get("type") != "text":
            continue
        for block in m["content"].get("richText", []):
            if block.get("type") in ("ul", "ol"):
                # Each child of a list block is one item (li); render one
                # line per item instead of concatenating them together.
                for item in block.get("children", []):
                    line = "".join(_leaf_texts(item)).strip()
                    if line:
                        out.append(f"- {line}")
            else:
                line = "".join(_leaf_texts(block)).strip()
                if line:
                    out.append(line)
    return "\n".join(out)


def extract_image_url(messages):
    for m in messages:
        if m.get("type") in ("image", "embed"):
            return m["content"].get("url")
    return None


def download_image(url, dest_path):
    """Product photo URLs are presigned (short expiry, ~40min on ABECMED) —
    download immediately rather than storing the URL for later."""
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(r.content)
    return dest_path


def crop_two_panel_image(path, out_left_path, out_right_path):
    """ABECMED's flower photos are a fixed two-panel template: a strain
    close-up on the left, a lab 'Características Organolépticas' info panel
    on the right. This is NOT assumed to generalize to other associations —
    only use it after visually confirming (by looking at a downloaded image)
    that a given association's bot ships the same kind of composite. Detects
    the split column via a brightness-transition scan (biggest jump in
    average column brightness within the middle 30-70% of the width) rather
    than a hardcoded pixel offset, since a batch can ship a differently-sized
    template. Requires Pillow (`pip install Pillow`); raises ImportError with
    a clear message if it's missing rather than failing on a cryptic line.
    """
    try:
        from PIL import Image
    except ImportError as e:
        raise ImportError(
            "crop_two_panel_image needs Pillow: pip install Pillow"
        ) from e

    img = Image.open(path).convert("RGB")
    width, height = img.size
    px = img.load()
    col_brightness = []
    for x in range(width):
        total = 0
        for y in range(0, height, max(1, height // 50)):  # sample rows, not every pixel
            r, g, b = px[x, y]
            total += (r + g + b) / 3
        col_brightness.append(total)

    lo, hi = int(width * 0.3), int(width * 0.7)
    split_x = max(
        range(lo, hi),
        key=lambda x: abs(col_brightness[x] - col_brightness[x - 1]),
    )

    img.crop((0, 0, split_x, height)).save(out_left_path)
    img.crop((split_x, 0, width, height)).save(out_right_path)
    return split_x


# ---------------------------------------------------------------------------
# Per-product detail (opt-in — only used when the caller asks for a richer
# catalog than the plain price list; see run_availability_check's
# `fetch_details` parameter and SKILL.md's "Generating the HTML catalog
# page" section)
# ---------------------------------------------------------------------------

# If any of these show up in a post-click response, treat it as a sign the
# bot moved toward a real order rather than a preview/detail card, and abort
# instead of guessing further. Not exhaustive by design — the "no back
# choice found" check below is the primary guardrail; this is a second
# tripwire for cases where a back choice exists but the screen is still
# clearly transactional (e.g. a quantity screen that also offers "Voltar").
ORDER_PROMPT_WARNING_WORDS = [
    "quantidade", "unidades", "pagamento", "pix", "gerar os dados",
    "confirmar pedido", "valor total", "carrinho", "finalizar compra",
]

# Typebot's own generic rejection of an unrecognized message — not this
# association's custom copy, so it's safe to hardcode rather than derive per
# association. A wrong navigation assumption (e.g. sending a message the bot
# doesn't recognize at its current screen) shows up as one of these — treat
# it as a signal to stop, not as real data, so a broken assumption for a new
# association's differently-shaped flow never gets silently recorded.
ENGINE_ERROR_MARKERS = ["invalid message", "please, try again", "please try again"]


def looks_like_unsafe_response(resp):
    """Heuristic safety check: True if this response should NOT be trusted
    as either a valid price list or a valid product-preview card — either
    Typebot's engine rejected the last message outright (a broken
    navigation assumption, not real data), or content-wise it looks like it
    moved toward a real order (quantity/payment) rather than a preview.
    Judged by content, not by matching a specific bot's wording, so it
    should hold even for an association never seen before."""
    text = _normalize(extract_text(resp.get("messages", [])))
    if any(_normalize(w) in text for w in ENGINE_ERROR_MARKERS):
        return True
    if any(_normalize(w) in text for w in ORDER_PROMPT_WARNING_WORDS):
        return True
    inp = resp.get("input")
    if inp and inp.get("type") not in ("choice input", "text input"):
        return True  # e.g. a numeric quantity stepper input
    return False


def fetch_product_detail(session, assoc, chat_session_id, product_choice):
    """Clicks one product name and returns (text, image_url, detail_resp).
    Does NOT click anything further — the caller must inspect the result
    (via `looks_like_unsafe_response` and/or checking for a back choice) before
    deciding whether it's safe to continue, and is responsible for sending
    the "back" choice itself afterward. This split is deliberate: never
    auto-drive past a product click without a safety check in between.
    """
    base_url = assoc["base_url"]
    detail_resp = continue_chat(session, base_url, chat_session_id, product_choice)
    text = extract_text(detail_resp.get("messages", []))
    img_url = extract_image_url(detail_resp.get("messages", []))
    return text, img_url, detail_resp


def choice_items(resp):
    inp = resp.get("input")
    if inp and inp.get("type") == "choice input":
        return [it["content"] for it in inp["items"]]
    return []


def is_text_input(resp):
    inp = resp.get("input")
    return bool(inp and inp.get("type") == "text input")


# ---------------------------------------------------------------------------
# Keyword-based choice matching (replaces exact-string matching so this works
# across bots with different copy for the same intent)
# ---------------------------------------------------------------------------

def find_choice(choices, keywords):
    """Return the first item in `choices` whose normalized text contains any
    of `keywords` (also normalized), or None if nothing matches."""
    norm_keywords = [_normalize(k) for k in keywords]
    for choice in choices:
        norm_choice = _normalize(choice)
        if any(k in norm_choice for k in norm_keywords):
            return choice
    return None


def find_all_matching(choices, keywords):
    norm_keywords = [_normalize(k) for k in keywords]
    return [c for c in choices if any(k in _normalize(c) for k in norm_keywords)]


def keywords_for(assoc, field):
    hints = assoc.get("flow_hints", {}) if assoc else {}
    value = hints.get(field) or DEFAULT_KEYWORDS.get(field, [])
    # associations.json allows a single string for a single-keyword field
    # (e.g. "back": "voltar") — normalize to a list. Iterating a bare string
    # in find_choice/find_all_matching would otherwise match on individual
    # *characters* instead of the whole word: "voltar" would degrade to
    # single-letter keywords like "t", spuriously matching "THC" before
    # "VOLTAR" ever got a chance.
    if isinstance(value, str):
        return [value]
    return value


# ---------------------------------------------------------------------------
# High-level flow
# ---------------------------------------------------------------------------

def reach_main_menu(session, assoc, cpf):
    """Drives start -> identify as patient -> submit CPF -> acknowledge any
    notices -> returns (chat_session_id, resp) sitting at the main menu."""
    base_url, slug = assoc["base_url"], assoc["typebot_slug"]
    resp = start_chat(session, base_url, slug)
    chat_session_id = resp["sessionId"]

    choices = choice_items(resp)
    patient_choice = find_choice(choices, keywords_for(assoc, "patient_choice"))
    if patient_choice:
        resp = continue_chat(session, base_url, chat_session_id, patient_choice)
    elif not is_text_input(resp):
        raise RuntimeError(
            "Couldn't find a 'sou paciente'-style choice or a direct text "
            "prompt at the start of the flow — bot structure may differ; "
            "inspect `resp` manually before proceeding."
        )

    if is_text_input(resp):
        resp = continue_chat(session, base_url, chat_session_id, cpf)

    # Acknowledge any interstitial notices: keep clicking the single
    # available choice-input option until the menu offers more than one
    # choice (a real menu) or a category-shaped choice appears.
    category_keywords = keywords_for(assoc, "categories")
    for _ in range(15):  # hard cap so a genuinely stuck flow raises, not loops forever
        choices = choice_items(resp)
        if not choices:
            raise RuntimeError(
                "Stuck with no choice-input before reaching a menu — bot "
                "flow may differ from what this crawler expects."
            )
        if len(choices) > 1 or find_choice(choices, category_keywords):
            break
        resp = continue_chat(session, base_url, chat_session_id, choices[0])

    return chat_session_id, resp


def _advance_through_single_choice_screens(session, base_url, chat_session_id, resp):
    """Keeps clicking the sole available choice until the bot presents a
    screen with more than one option (a real menu / the price-list screen) —
    used to skip past a chain of "Estou ciente!"-style notice
    acknowledgements without needing to know their exact wording."""
    for _ in range(15):  # hard cap so a genuinely stuck flow raises, not loops forever
        choices = choice_items(resp)
        if not choices or len(choices) > 1:
            return resp
        resp = continue_chat(session, base_url, chat_session_id, choices[0])
    return resp


def _drill_into_products(session, assoc, chat_session_id, cat_choices, type_choices, image_dir):
    """Opt-in path used by `fetch_details=True`: for each type button
    (THC/CBD/...), click into its product list, then click into each
    product for its detail card (text + photo), downloading the photo
    locally. Follows the pattern confirmed safe on ABECMED (product click ->
    preview card -> Voltar -> back at the category/type-selection screen),
    but re-checks safety on every single click instead of trusting that
    pattern for an unverified association.

    Returns {type_choice: {product_name: {"desc": str, "img_path": str|None}}}.
    Raises RuntimeError immediately (leaving remaining products/types
    unfetched) the moment a click looks like it left preview territory —
    by design, this is not caught and retried automatically.
    """
    base_url = assoc["base_url"]
    back_keywords = keywords_for(assoc, "back")
    by_type = {}
    for type_choice in type_choices:
        product_list_resp = continue_chat(session, base_url, chat_session_id, type_choice)
        product_names = choice_items(product_list_resp)
        products = {}
        for i, product_choice in enumerate(product_names):
            text, img_url, detail_resp = fetch_product_detail(
                session, assoc, chat_session_id, product_choice
            )
            detail_choices = choice_items(detail_resp)
            back_from_detail = find_choice(detail_choices, back_keywords)
            if looks_like_unsafe_response(detail_resp) or not back_from_detail:
                raise RuntimeError(
                    f"Stopping: clicking {product_choice!r} led to a screen "
                    f"that doesn't look like a safe product-preview card "
                    f"(choices={detail_choices!r}). Never proceed past this "
                    "point automatically — re-check this association's "
                    "product-detail flow manually (browser fallback) before "
                    "trying fetch_details=True again."
                )
            img_path = None
            if img_url and image_dir:
                slug = re.sub(r"[^a-z0-9]+", "-", _normalize(product_choice)).strip("-")
                img_path = str(download_image(img_url, Path(image_dir) / f"{slug}.jpg"))
            products[product_choice] = {"desc": text, "img_path": img_path}
            # back_from_detail returns to the *type-selection* screen, not
            # the product list (observed on ABECMED) — if there's
            # another product left in this type, re-click type_choice to get
            # the product list again first: the next product name isn't a
            # valid choice from the type-selection screen on its own, so
            # skipping this would get a generic "Invalid message" response
            # from the bot instead of a real detail card.
            continue_chat(session, base_url, chat_session_id, back_from_detail)
            if i < len(product_names) - 1:
                continue_chat(session, base_url, chat_session_id, type_choice)
        by_type[type_choice] = products
    return by_type


def run_availability_check(assoc, cpf, debug=False, fetch_details=False, image_dir=None):
    """Top-level entry point: returns
    {category_choice_label: {"price_text": str, "available_type_buttons": [...], "note": str|None, "products": {...}|None}}

    By default (`fetch_details=False`) this never clicks a cannabinoid-type
    (THC/CBD/...) button, let alone an individual product: confirmed against
    the real ABECMED bot that the full per-type price list is already
    present as text on the screen right after picking a category (see
    `extract_text`'s docstring for why a naive text reader missed it) — so
    for a plain price check there's no upside to going deeper, and it avoids
    ever coming near the quantity/payment steps.

    `fetch_details=True` opts into also drilling into each type's product
    list and each product's detail card (text + photo) for a richer catalog
    (see `_drill_into_products`) — pass `image_dir` (a directory path) to
    download photos there. This clicks further into the bot than the
    default mode, with a safety check after every single click (see
    `looks_like_unsafe_response`) that aborts loudly rather than guessing past
    anything that doesn't look like a plain preview card. Only use this once
    you're comfortable with what it does — read `_drill_into_products` and
    SKILL.md's "Generating the HTML catalog page" section first.

    `debug=True` adds a "_raw_messages" list to each category's dict with
    the unprocessed message objects from that screen — useful when
    onboarding an unfamiliar association and `price_text` doesn't obviously
    carry the price info (bot copy varies; see SKILL.md "Unknown
    association").
    """
    session = _session()
    chat_session_id, resp = reach_main_menu(session, assoc, cpf)

    category_keywords = keywords_for(assoc, "categories")
    order_status_keywords = keywords_for(assoc, "order_status")
    back_keywords = keywords_for(assoc, "back")
    type_keywords = keywords_for(assoc, "cannabinoid_types")
    menu_choices = choice_items(resp)
    categories = [
        c for c in menu_choices
        if find_choice([c], category_keywords) and not find_choice([c], order_status_keywords)
    ]
    if not categories:
        raise RuntimeError(
            f"No category-shaped choice found among {menu_choices!r} — "
            "association's flow_hints['categories'] probably needs updating."
        )

    base_url = assoc["base_url"]
    report = {}
    for category in categories:
        # Each category is entered fresh from the main menu, so state here
        # is always: main menu -> category. Every path below either returns
        # to the main menu or explains why it couldn't, before the next
        # category is attempted.
        cat_resp = continue_chat(session, base_url, chat_session_id, category)
        cat_resp = _advance_through_single_choice_screens(session, base_url, chat_session_id, cat_resp)

        choices = choice_items(cat_resp)
        available_types = find_all_matching(choices, type_keywords)
        entry = {
            "price_text": extract_text(cat_resp.get("messages", [])),
            "available_type_buttons": available_types,
        }
        if debug:
            entry["_raw_messages"] = cat_resp.get("messages", [])

        if any(_normalize(w) in _normalize(entry["price_text"]) for w in ENGINE_ERROR_MARKERS):
            # Typebot rejected a message on the way here — a navigation
            # assumption is wrong for this association. Never record this as
            # if it were real price data (this exact failure mode — an
            # engine error silently treated as valid — bit twice during
            # development; see ENGINE_ERROR_MARKERS).
            entry["note"] = (
                f"Bot rejected a message while reaching this category "
                f"(response: {entry['price_text']!r}). Stopping here rather "
                "than trusting this as real price data — re-check via the "
                "browser fallback."
            )
            report[category] = entry
            break

        back_choice = find_choice(choices, back_keywords)
        if not back_choice and choices:
            # Landed on a screen with no recognizable "back" option — e.g.
            # ABECMED's "concentrados" category branched into an unrelated
            # "Adquirir óleo / Adquirir flores" upsell instead of a price
            # list the one time this was tested (2026-08-12). Surface it
            # rather than guessing a click, and don't attempt further
            # categories from this session — state after here is uncertain.
            entry["note"] = (
                f"Unexpected screen with no 'back' choice: {choices!r}. "
                "Stopping here — re-check this category via the browser "
                "fallback instead of guessing further clicks."
            )
            report[category] = entry
            break

        if fetch_details and available_types:
            entry["products"] = _drill_into_products(
                session, assoc, chat_session_id, choices, available_types, image_dir
            )

        if back_choice:
            continue_chat(session, base_url, chat_session_id, back_choice)

        report[category] = entry

    return report


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python crawler.py <associacao> <cpf>", file=sys.stderr)
        sys.exit(1)
    assoc_id, cpf_arg = sys.argv[1], sys.argv[2]
    association = load_association(assoc_id)
    if association is None:
        print(
            f"'{assoc_id}' not found in associations.json. Ask the user for "
            "that association's bot URL and typebot slug, or run the "
            "SKILL.md discovery flow instead of guessing.",
            file=sys.stderr,
        )
        sys.exit(1)
    result = run_availability_check(association, cpf_arg)
    print(json.dumps(result, ensure_ascii=False, indent=2))
