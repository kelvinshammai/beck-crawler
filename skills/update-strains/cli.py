"""
Standalone command-line entry point for update-strains — no Claude Code, no
agents, just Python. Runs one crawl (fetch_details=True) against the
association's bot, prints the price list, and builds/updates the HTML
catalog page, all in a single process.

Usage:
    python cli.py <associacao> <cpf> [caminho_do_catalogo.html]

    # default caminho: catalogo-<associacao>/index.html (current directory)

Requires: requests (always), Pillow (only if the association's photos use
the two-panel crop — see below).

What this CLI does *not* do, compared to running the skill via Claude Code
(see SKILL.md's "Execution model: two agents"):

  - It's one sequential run, not "fast reply now, catalog later" — the
    price list only prints after the whole crawl (including every product
    click) finishes. For a catalog with many products this can take a
    while (REQUEST_DELAY between every call, on purpose — see crawler.py).
  - No interactive "unknown association" discovery: if `<associacao>` isn't
    in associations.json, this exits with an error instead of asking you
    questions. Add the association to associations.json yourself first
    (see SKILL.md's "Unknown association" section), or run this via Claude
    Code instead, which can do that discovery conversationally.
  - No visual judgment of product photos. It crops a photo into photo/info
    halves only when the association is explicitly marked
    `"photo_template": "two_panel"` in associations.json (true for
    "abecmed"), and pulls lineage/ratio/THC/terpenes/sensation from the
    bot's TEXT description via best-effort regexes tuned to ABECMED's copy
    (`parse_flower_text` below) — a different association's bot will very
    likely need its own version of that
    function. CBD%/moisture/water-activity live ONLY in the info-panel
    *image*, never in text the bot sends, so this CLI leaves them empty.
    Run the skill via Claude Code (Agent 2) if you want a human/Claude to
    actually look at the photo and fill those in.
"""

import re
import sys
import time
from pathlib import Path

import catalog
import crawler


def parse_flower_text(desc):
    """Best-effort extraction from ABECMED-style product description text,
    e.g.:
        "Papaya (indoor)
         Genética: Citral #13 × Ice #2 Híbrida com predominância índica
         (20% sativa / 80% indica) - THC: até 15%
         Terpenos predominantes: Mirceno - Limoneno"
    Tuned to ABECMED's exact copy — treat every field as "if we can find
    it", not guaranteed, and leave a field unset rather than guessing when
    a pattern doesn't match. A different association's bot copy will need
    its own version of this function."""
    out = {}

    m = re.search(r"Gen[ée]tica:\s*(.+?)\s*-?\s*H[íi]brida", desc, re.IGNORECASE)
    if m:
        out["lineage"] = m.group(1).strip(" -")

    m = re.search(r"\((\d+)%\s*sativa\s*/\s*(\d+)%\s*indica\)", desc, re.IGNORECASE)
    if m:
        out["ratio"] = {"sativa": int(m.group(1)), "indica": int(m.group(2))}

    m = re.search(r"THC:\s*(?:at[ée]\s*)?(\d+(?:\.\d+)?)\s*%", desc, re.IGNORECASE)
    if m:
        thc = float(m.group(1))
        out["thc"] = int(thc) if thc.is_integer() else thc

    m = re.search(r"Terpenos predominantes:\s*(.+)", desc, re.IGNORECASE)
    if m:
        out["terpenes"] = [t.strip().capitalize() for t in re.split(r"[-,]", m.group(1)) if t.strip()]

    m = re.search(r"Sensa[çc][ãa]o predominante:\s*(.+)", desc, re.IGNORECASE)
    if m:
        out["sensation"] = [t.strip().capitalize() for t in re.split(r"\s+e\s+|,", m.group(1)) if t.strip()]

    return out


def parse_prices(price_text):
    """category price_text has lines like '- Papaya (indoor) - R$ 85.00'
    (see extract_text's bullet-list handling in crawler.py) — split each on
    the last ' - ' to recover {product_name: price_string}."""
    prices = {}
    for line in (price_text or "").splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        parts = line[2:].rsplit(" - ", 1)
        if len(parts) == 2:
            prices[parts[0].strip()] = parts[1].strip()
    return prices


def category_key_for(assoc, category_label):
    cats = assoc.get("flow_hints", {}).get("categories") or crawler.DEFAULT_KEYWORDS["categories"]
    if isinstance(cats, str):
        cats = [cats]
    norm_label = crawler._normalize(category_label)
    for kw in cats:
        if crawler._normalize(kw) in norm_label:
            return crawler._normalize(kw)
    return catalog.slugify(category_label)


def build_products(assoc, report, image_dir):
    two_panel = assoc.get("photo_template") == "two_panel"
    products = []
    for category_label, entry in report.items():
        prices = parse_prices(entry.get("price_text"))
        category_key = category_key_for(assoc, category_label)
        for type_choice, prod_map in entry.get("products", {}).items():
            for name, info in prod_map.items():
                p = {
                    "id": catalog.slugify(name),
                    "name": name,
                    "category": category_key,
                    "cannabinoid": type_choice,
                    "price": prices.get(name, ""),
                }
                p.update(parse_flower_text(info.get("desc") or ""))

                img_path = info.get("img_path")
                if img_path:
                    slug = catalog.slugify(name)
                    if two_panel:
                        try:
                            photo_path = str(Path(image_dir) / f"{slug}-photo.jpg")
                            info_path = str(Path(image_dir) / f"{slug}-info.jpg")
                            crawler.crop_two_panel_image(img_path, photo_path, info_path)
                            p["img"] = f"imagens/{slug}-photo.jpg"
                            p["infoImg"] = f"imagens/{slug}-info.jpg"
                        except Exception as e:
                            print(f"  aviso: crop de {name!r} falhou ({e}), usando foto inteira", file=sys.stderr)
                            p["img"] = f"imagens/{Path(img_path).name}"
                    else:
                        p["img"] = f"imagens/{Path(img_path).name}"
                products.append(p)
    return products


def main():
    if len(sys.argv) < 3:
        print("Uso: python cli.py <associacao> <cpf> [caminho_do_catalogo.html]", file=sys.stderr)
        sys.exit(1)
    assoc_id, cpf = sys.argv[1], sys.argv[2]
    out_path = sys.argv[3] if len(sys.argv) > 3 else f"catalogo-{assoc_id.lower()}/index.html"

    assoc = crawler.load_association(assoc_id)
    if assoc is None:
        print(
            f"'{assoc_id}' não está em associations.json. Este CLI não faz "
            "descoberta interativa de associação nova — adicione manualmente "
            "primeiro (ver SKILL.md, seção 'Unknown association'), ou rode "
            "via Claude Code, que consegue conduzir essa descoberta.",
            file=sys.stderr,
        )
        sys.exit(1)

    image_dir = str(Path(out_path).parent / "imagens")
    print(f"Consultando {assoc['label']} (CPF terminando em ...{cpf[-4:]})...")
    report = crawler.run_availability_check(assoc, cpf, fetch_details=True, image_dir=image_dir)

    print(f"\n=== Preços — {assoc['label']} ===")
    for category_label, entry in report.items():
        print(f"\n{category_label}")
        print(entry.get("price_text") or entry.get("note") or "(sem dados)")

    products = build_products(assoc, report, image_dir)
    seen_at = time.strftime("%d/%m/%Y %H:%M")

    created = catalog.init_catalog(out_path, assoc["label"])
    existing = catalog.load_products(out_path)
    merged = catalog.merge_products(existing, products, seen_at=seen_at)
    catalog.save_products(out_path, merged)

    print(f"\n=== Catálogo ===")
    print(
        f"{'Criado' if created else 'Atualizado'} em {out_path} "
        f"({len(products)} produto(s) visto(s) nesta execução, {len(merged)} no total)."
    )
    print(
        "Nota: CBD%/moisture/water-activity ficam vazios nesse modo — só "
        "existem na foto da ficha técnica, que este script não lê "
        "visualmente. Rode via Claude Code (SKILL.md) pra isso."
    )


if __name__ == "__main__":
    main()
