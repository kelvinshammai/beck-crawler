---
name: update-strains
description: Use when the user wants to check current patient product availability and prices, and/or generate or update an HTML product catalog page, on a cannabis association's Typebot-based patient chatbot. Invoked as `/update-strains <associacao> <cpf>` — always does both: an immediate price-list reply plus a background HTML catalog build. Trigger phrases: "/update-strains", "disponibilidade da [associação]", "preço na [associação]", "checar estoque de [associação]", "gerar catálogo da [associação]", "página de produtos da [associação]".
---

# update-strains: multi-association patient bot availability check

Drives *any* cannabis association's Typebot-based patient chatbot to extract
current product availability and prices, given the association's name and a
patient's CPF.

## Parsing the command

`/update-strains <associacao> <cpf>` — first token is the association
identifier (matched against `associations.json`, case/accent-insensitive),
second is the CPF. Every run does both the quick check and the catalog
build (see "Execution model" below) — there's no separate "just the price"
mode.

- If either argument is missing, **ask the user** — never guess an
  association or invent/reuse a CPF from memory or a previous conversation.
  CPF is sensitive third-party personal data; only use one the user has
  explicitly supplied for this request.
- **Resolve the association before spawning either agent below.** Call
  `load_association()` yourself first. If it returns `None`, run the
  "Unknown association" procedure yourself (main thread, not a subagent) —
  asking the user for the bot URL, confirming it, discovering the flow —
  since both agents need a working association config, and duplicating that
  discovery/back-and-forth across two agents would just confuse the user
  with the same questions twice. Only spawn the two agents once you have a
  resolved `assoc`.

## Execution model: two agents

Every `/update-strains <associacao> <cpf>` run dispatches two independent
`Agent` calls once `assoc` is resolved — a fast one whose result becomes
this turn's reply, and a slower one that builds the HTML catalog in the
background:

1. **Foreground agent — price check.** Call `Agent` with
   `run_in_background: false` (its result is what you reply with — you
   need it before you can answer). Prompt it with the resolved association
   config (or its key), the CPF, the absolute path to this skill's
   directory, and an instruction to follow this file's "Agent 1: quick
   price check" section and return the table format from "Output". Relay
   its result to the user essentially verbatim — that table *is* the reply.
2. **Background agent — catalog build.** Call `Agent` in the same turn
   (parallel tool call), `run_in_background: true` (the default — don't
   pass `false` here). Prompt it with the same association/CPF plus the
   target catalog path in the **current project** (default
   `catalogo-<associacao-key>/index.html` — see "Agent 2" below), and an
   instruction to follow this file's "Agent 2: build/update the HTML
   catalog" section. Mention in your reply that the catalog page is being
   generated and you'll follow up when it's ready — then actually relay
   its result (or failure) when the background notification arrives; never
   fabricate what it found before it reports back.

Use `subagent_type: "general-purpose"` for both — each needs `Bash` to run
`crawler.py`/`catalog.py` (plain Python, no special environment), and Agent
2 additionally needs `Read` (to look at downloaded photos) and, if the API
path fails, the `mcp__claude-in-chrome__*` tools for the browser fallback.

Both agents log in and walk the bot **independently** (separate sessions,
each starting from `startChat`) rather than sharing one crawl — that's two
logins instead of one, but it keeps the fast reply genuinely fast and the
two concerns fully decoupled. Each agent still obeys `REQUEST_DELAY` and
every other guardrail below on its own; running two agents does not excuse
either from being polite to the third-party server.

## Guardrails — apply to both agents, every association

- **Rate limit**: `REQUEST_DELAY` between every API call, already built into
  `continue_chat()`. Never parallelize requests against someone else's
  production server — this applies per-agent too; the two agents running
  concurrently is about decoupling fast/slow work, not about hammering the
  bot faster.
- **Never place a real order**: Agent 1 stops at the category screen (the
  one showing the price list + type buttons) — the price text is already
  there, so there's no need to click a THC/CBD/etc. button or an individual
  product at all (see "Known caveats" below). Agent 2 *does* click into
  products (that's the point, for photos/description) but stops the instant
  a click looks unsafe — see its own section for the specific checks. Never
  send a quantity, never send anything that looks like "continuar"/"enviar"
  beyond CPF, notice acknowledgements, and — Agent 2 only — a single product
  click immediately followed by "back". If a screen asks for quantity or
  payment info, you've gone too far — back out immediately using the
  association's "back" choice. This applies even when you don't know the
  bot's exact wording: judge by what the screen is asking for, not by
  matching a specific label.
- **CPF handling**: never invent, never reuse across different people's
  requests without asking, never log/print/commit outside the conversation.
- **Live data**: don't cache results across sessions/runs; always re-run for
  a fresh check.

## Agent 1: quick price check

Typebot exposes a public, unauthenticated REST API that the chat widget itself calls
(`POST {base_url}/typebots/{typebot_slug}/startChat`, then
`POST {base_url}/sessions/{sessionId}/continueChat`). `crawler.py` in this
skill's directory implements this generically:

```python
from crawler import load_association, run_availability_check

assoc = load_association("abecmed")   # resolves via associations.json
report = run_availability_check(assoc, cpf)   # fetch_details=False (default) — never clicks a product
```

Button text isn't hardcoded anywhere. Choices are matched by **keyword**
(`find_choice`/`find_all_matching` in
`crawler.py`), using an association's `flow_hints` when present and a
generic default set otherwise (patient identification, category names,
cannabinoid types, "back"). Read `crawler.py`'s docstring before running it —
it documents every function and the guardrails above in code form. Format
the result per "Output" below and return that as this agent's result —
that's what becomes the reply to the user.

## Unknown association

If `load_association()` returns `None`:

1. Ask the user for that association's patient bot URL (and confirm it's the
   association's *own* official site — this registry may end up shared with
   other people, so a wrong/unverified domain here is a real risk, not just
   a data-quality issue).
2. Try `start_chat()` against that URL's Typebot API directly (guess the
   typebot slug from the URL path if the widget is embedded, or ask the user
   to open browser devtools / use the fallback method below to observe the
   real `startChat` request once).
3. Walk the flow using the **default** keyword sets in `crawler.py` — if a
   button's intent is ambiguous (e.g. an unfamiliar category name), show the
   raw choice list to the user and ask rather than guessing which one means
   "flowers"/"concentrates"/etc.
4. Once the flow is confirmed working end-to-end for that association
   (reaches a type-selection screen with real prices), record what you
   learned as `flow_hints` and **offer** to add the association to
   `associations.json` via `save_association()` — don't add it silently
   without telling the user, since this file may be shared/committed.

## Fallback method: browser automation via the Claude in Chrome extension

Use this if the API is unavailable, the flow's contract changed, or you
still need to discover a new association's exact flow before scripting it.
This skill uses the **Claude in Chrome extension** (`mcp__claude-in-chrome__*`
tools), not Playwright MCP — it's the browser automation people installing
this skill are expected to have available. If those tools aren't loaded yet,
load them first:

```
ToolSearch: select:mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__computer,mcp__claude-in-chrome__read_page,mcp__claude-in-chrome__tabs_create_mcp,mcp__claude-in-chrome__tabs_close_mcp
```

### Procedure

1. **Tab context**: call `tabs_context_mcp` first to see existing tabs;
   create a new tab with `tabs_create_mcp` rather than reusing an unrelated
   one.
2. **Navigate** to the association's bot URL.
3. **Read the page** (`read_page`, or `find` for a specific element) to see
   the current chat widget state — don't assume ABECMED's exact button
   labels ("Sou Paciente", etc.) apply to a different association's bot;
   identify elements by what they clearly do (e.g. "the button that
   identifies the visitor as a patient", "the CPF/identifier text field").
4. **Identify as patient**, using whatever the equivalent control is.
5. **Submit CPF** into the identifier field.
6. **Acknowledge notices**: click through any informational/warning messages
   the same way, re-reading the page after each click since new messages
   stream in.
7. **Main menu**: record every top-level category button offered (don't
   assume it's exactly Flores/Concentrados/Óleos — a different association
   may structure this differently), then for each product category:
   a. Click the category.
   b. Acknowledge interstitial notices as in step 6.
   c. Read the product list block: monthly limit line, cannabinoid-type
      sub-lists, and which types have an actual selectable button.
   d. Click whatever the equivalent of "voltar"/back is before checking the
      next category.
8. Skip anything that looks like an order-status lookup (asks for an order
   reference, not pricing).
9. **Same guardrail as the API method**: never proceed past type-selection
   into an actual order. If unsure whether a click starts a real order, stop
   and confirm with the user first.
10. Avoid triggering native browser dialogs (alert/confirm/prompt) — this
    flow is click/type-only against a chat widget, so this shouldn't come
    up, but don't click anything you haven't identified.
11. Once the flow is confirmed, write down the discovered labels/selectors
    as that association's `flow_hints` (see "Unknown association" above) so
    future runs can use the faster API method.

## Output

Return a table per category actually offered by that association's bot (not
a fixed Flores/Concentrados/Óleos list), each row:
`Produto | Variante | Preço | Categoria (THC/CBD/...)`, plus the monthly
limit text for that category. Note explicitly if a mentioned cannabinoid
type had no clickable button (temporarily unavailable, not simply unlisted).

## Agent 2: build/update the HTML catalog

This is the background agent from "Execution model" — runs on every
`/update-strains` invocation, not an opt-in mode. Implemented in code
(`catalog.py`) rather than hand-editing HTML, generalized to any
association's category names. **The generated page lives in the project
where the skill is run** — never inside this beck-crawler skill repo, since
each association's catalog is that association's own content. Default
path: `catalogo-<associacao-key>/index.html` +
`catalogo-<associacao-key>/imagens/` in the current project — the
orchestrating turn should pass this path (or a user-specified override)
into this agent's prompt explicitly, since a fresh agent has no way to infer
"the current project" on its own.

When this agent finishes, its result should state plainly what happened:
the path written, how many products found/updated/marked unavailable, and
whether any photos were fetched — that's what gets relayed to the user when
the background notification arrives.

### Workflow

The mechanical part of this — crawling, price/text parsing, cropping known
two-panel photos, the never-delete/mark-availability merge, writing the
file — is exactly what `cli.py` does standalone (see the top-level
`README.md`'s "Rodar sem o Claude" section for what it does and doesn't
do). **Run it via Bash instead of re-deriving these steps by hand:**

```bash
cd <this skill's own directory — same one containing this SKILL.md and cli.py>
python3 cli.py <associacao-key> <cpf> <path-in-current-project>
```

Don't hardcode a specific install path (`.claude/skills/update-strains/`,
`~/.claude/skills/update-strains/`, or wherever a plugin install lands) —
`cd` into wherever *this file itself* was loaded from.

This alone gets you a fully-updated catalog with prices, photos, and
(for associations already marked `"photo_template": "two_panel"` in
`associations.json`) cropped photo/info halves plus lineage/ratio/THC/
terpenes/sensation parsed from the bot's text. Read `cli.py`'s own
docstring and `_drill_into_products`'s safety checks in `crawler.py` before
running it — it clicks further into the bot than the price-only check
(that's the whole point, for photos), and it can fail loudly on purpose
(see `looks_like_unsafe_response`) if a click looks unsafe. **If it exits
with an error**, stop: don't retry automatically, don't guess a workaround,
tell the user what happened and fall back to the browser method for a
supervised single check.

What `cli.py` *can't* do, and what this agent's own value-add on top of it
is:

1. **Read every downloaded product photo** (`imagens/*.jpg` under the
   catalog's directory — the Read tool, not code) and decide, per photo,
   whether it matches ABECMED's two-panel template. For an association
   `cli.py` already knew about, it already cropped and you're just
   filling in the numbers below. For one it didn't (no
   `"photo_template"` set, or a genuinely new association), *you* make
   that visual call — see "Reading product photos" below.
2. **Fill in CBD%/moisture/water-activity** by reading the info-panel
   image directly — these exist *only* as pixels in that image, `cli.py`
   never sees them. Update the already-written catalog:
   `existing = catalog.load_products(path)`, edit the relevant product
   dicts in place, `catalog.save_products(path, existing)`.
3. **If this association wasn't marked `"photo_template"` yet** and you
   confirm visually it does use the two-panel layout, crop it yourself
   with `crawler.crop_two_panel_image()`, update `img`/`infoImg`
   accordingly, and — after telling the user — add
   `"photo_template": "two_panel"` to its `associations.json` entry so
   `cli.py` handles it automatically next time.

### Product schema

Required: `id` (stable slug), `name`, `category` (the association's own
category key, e.g. `"flor"`), `price` (display string), `available` (bool),
`lastSeenAvailable` (display string timestamp).

Optional (page renders fine with any subset present/absent — check what the
association's bot actually exposes rather than assuming ABECMED's full set
applies): `cannabinoid` (THC/CBD/...), `grow`, `thc` (number, %), `cbd`
(display string, e.g. `"<2%"`), `moisture`, `waterActivity`, `ratio`
(`{sativa, indica}`, both 0-100), `terpenes` (string array), `sensation`
(string array), `aromas` (string array), `formTags` (string array), `lineage`
(string), `img`/`infoImg` (relative path under `imagens/`), `note` (shown in
the detail drawer — use it for caveats like "sem estoque, dados incompletos"
the same way ABECMED's catalog does).

### Reading product photos (visual step, not automated)

`fetch_details=True` downloads whatever single photo each product's detail
card returns, verbatim, to `imagens/<slug>.jpg` — it does **not** try to
crop or read numbers out of it, since that's speculative for an association
never seen before. After downloading:

1. **Look at the image** (Read tool) before assuming anything about its
   layout.
2. If it's a plain product photo, use it as `img` directly.
3. If it visually matches ABECMED's known template (strain close-up on the
   left, a lab "Características Organolépticas" panel on the right), you can
   split it with `crawler.crop_two_panel_image(path, out_photo, out_info)` —
   a brightness-transition-scan crop (not a hardcoded pixel offset, so it
   tolerates a differently-sized batch). Use the photo half as `img`, the
   info half as `infoImg`, and **read the THC/CBD/moisture/water-activity
   values off the info panel visually** — they're only present as an image,
   not as text the bot sends. Don't invent or copy values from a different
   product's card.
4. **The info panel's numbers can disagree with the bot's text description**
   for the same product (e.g. a photographed batch showing 14% THC while the
   text description says 21%) — these are apparently two different data
   sources (per-batch lab result vs. generic strain copy). Capture both if
   they differ; don't silently overwrite one with the other. Use `note` to
   flag the discrepancy.

## Known caveats

- **A category with zero current stock may not offer a "back" choice at
  all.** On ABECMED, "Quero adquirir concentrados!" with no product in
  stock shows an "out of stock, want oil or flowers instead?" upsell
  instead of the usual price-list screen (with a VOLTAR button), with no
  back option. `crawler.py` handles this by stopping and attaching a `note`
  to that category's entry rather than guessing a click — treat a `note`
  field in the output as "this category's real state is out-of-stock text,
  not a price list; read `price_text` directly, don't expect
  `available_type_buttons`." If more categories were still pending in the
  same run, they weren't checked — re-run or fall back to the browser
  method for the rest.
- **Agent 1 never clicks a cannabinoid-type button (THC/CBD/etc.) for
  pricing.** The full per-type price list is already present as text on the
  screen right after picking a category — clicking a type button only gets
  you bare product *names* with no price attached, and offers no benefit
  for a price-only check. `run_availability_check`'s default
  (`fetch_details=False`) intentionally never does this; only Agent 2
  (`fetch_details=True`) drills further, and only for building the catalog.

## Notes

- This is live production data belonging to third parties — don't cache
  results across sessions, always re-run for a fresh check.
- `associations.json` holds bot **config** only (URL, slug, UI-copy hints) —
  never add a CPF or any patient data to it.
- Each association's bot is independently built and can change its flow at
  any time without notice; adapt by re-observing rather than assuming a
  previously-recorded `flow_hints` entry stays correct forever. If a
  previously-working association starts failing, treat it like an unknown
  association and re-run discovery.
- Scope: both a quick price-list reply (Agent 1) and a rich per-product
  catalog page with photos (Agent 2) run on every invocation now — see
  "Execution model". Agent 2's photo-panel reading is still a visual,
  per-association judgment call (see its "Reading product photos" section),
  not something guaranteed to work identically for a bot that doesn't share
  ABECMED's two-panel template.
