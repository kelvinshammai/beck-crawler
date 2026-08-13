"""
Generates/updates the update-strains skill's HTML catalog page
(`catalog_template.html`) for a given association, following a never-
delete, mark-availability policy — existing products always stay in the
catalog, refreshed if seen again this run or marked unavailable otherwise.

The template embeds its data as two `<script type="application/json">`
blocks (`#products-data`, `#category-meta-data`) rather than a raw JS
object literal, so this module can read/write them with the stdlib `json`
module instead of needing a JS-object-literal parser.

Typical usage (see SKILL.md's "Agent 2: build/update the HTML catalog" section):

    import catalog

    catalog.init_catalog("catalogo-abecmed/index.html", "ABECMED")
    existing = catalog.load_products("catalogo-abecmed/index.html")
    merged = catalog.merge_products(existing, discovered_products, seen_at="12/08/2026 19:30")
    catalog.save_products("catalogo-abecmed/index.html", merged)
"""

import json
import re
import unicodedata
from pathlib import Path

TEMPLATE_PATH = Path(__file__).parent / "catalog_template.html"

_BLOCK_RE = {
    "products": re.compile(
        r'(<script id="products-data" type="application/json">\n?)(.*?)(\n?</script>)',
        re.DOTALL,
    ),
    "category_meta": re.compile(
        r'(<script id="category-meta-data" type="application/json">\n?)(.*?)(\n?</script>)',
        re.DOTALL,
    ),
}


def slugify(text):
    """Turn a product/variant name into a stable id for merge-by-id, e.g.
    'Papaya (indoor)' -> 'papaya-indoor'."""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def init_catalog(html_path, association_label, header_sub=None, footer_note=None):
    """Create a fresh catalog page at html_path from the template. Never
    overwrites an existing file — the never-delete policy starts here: if a
    catalog already exists at this path, update it via
    load_products/merge_products/save_products instead of calling this
    again. Returns True if a new file was created, False if one already
    existed (and was left untouched)."""
    html_path = Path(html_path)
    if html_path.exists():
        return False
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = template.replace("{{ASSOCIATION_LABEL}}", association_label)
    html = html.replace(
        "{{HEADER_SUB}}",
        header_sub or f"Produtos disponíveis na {association_label}.",
    )
    html = html.replace(
        "{{FOOTER_NOTE}}",
        footer_note
        or "Preços e disponibilidade capturados ao vivo do bot da associação "
        "e podem estar desatualizados.",
    )
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")
    return True


def _read_block(html, key):
    m = _BLOCK_RE[key].search(html)
    if not m:
        raise ValueError(
            f"Couldn't find the '{key}' data block — is this actually a "
            "update-strains catalog page (created via init_catalog)?"
        )
    return json.loads(m.group(2))


def _write_block(html, key, data):
    new_json = json.dumps(data, ensure_ascii=False, indent=2)
    new_html, n = _BLOCK_RE[key].subn(
        lambda m: m.group(1) + new_json + m.group(3), html, count=1
    )
    if n == 0:
        raise ValueError(
            f"Couldn't find the '{key}' data block — is this actually a "
            "update-strains catalog page (created via init_catalog)?"
        )
    return new_html


def load_products(html_path):
    return _read_block(Path(html_path).read_text(encoding="utf-8"), "products")


def save_products(html_path, products):
    html_path = Path(html_path)
    html = _write_block(html_path.read_text(encoding="utf-8"), "products", products)
    html_path.write_text(html, encoding="utf-8")


def load_category_meta(html_path):
    return _read_block(Path(html_path).read_text(encoding="utf-8"), "category_meta")


def save_category_meta(html_path, category_meta):
    """category_meta shape: {categoryKey: {"label": str, "icon": str}}.
    Optional — categories without an entry here just render with a generic
    label/icon (see catalog_template.html's categoryLabel()/iconFor())."""
    html_path = Path(html_path)
    html = _write_block(
        html_path.read_text(encoding="utf-8"), "category_meta", category_meta
    )
    html_path.write_text(html, encoding="utf-8")


def merge_products(existing, discovered, seen_at):
    """Never-delete, mark-availability merge:
      - every product ever captured (by "id") stays in the result forever —
        never drop an entry just because this run didn't see it;
      - a product present in `discovered` this run gets its fields
        refreshed, `available` set True, `lastSeenAvailable` set to
        `seen_at`;
      - a product from `existing` NOT present in `discovered` this run gets
        `available` set False, keeping its last-known data untouched;
      - a product in `discovered` whose "id" was never seen before is
        appended as new.

    Every product dict must have a stable "id" key — see `slugify`. Order is
    preserved (existing order, then newly-appended ones) so repeated runs
    don't visually shuffle the catalog.
    """
    by_id = {p["id"]: dict(p) for p in existing}
    order = [p["id"] for p in existing]
    seen_ids = set()
    for p in discovered:
        pid = p["id"]
        seen_ids.add(pid)
        merged = {**by_id.get(pid, {}), **p}
        merged["available"] = True
        merged["lastSeenAvailable"] = seen_at
        by_id[pid] = merged
        if pid not in order:
            order.append(pid)
    for pid, p in by_id.items():
        if pid not in seen_ids:
            p["available"] = False
    return [by_id[pid] for pid in order]
