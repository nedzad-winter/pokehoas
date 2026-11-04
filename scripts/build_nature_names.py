#!/usr/bin/env python3
"""Adds `nature.<key>` strings to data/i18n/{en,fr,es,it,ja,ko}.json from the official nature names
in PokeAPI. German and Polish get the English names on purpose (the frontend comment on natureLabel
explains why), so that every language file has the same keys.

Run from the repo root (needs network access to pokeapi.co):

    python scripts/build_nature_names.py
"""

from __future__ import annotations

import ast
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "pokemon_encounter" / "app"
LANGS = ("en", "fr", "es", "it", "ja", "ko")


def fetch(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "pokehoas-name-builder"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def project_natures() -> list[str]:
    tree = ast.parse((APP / "spawn.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "NATURES" for t in node.targets):
            return list(ast.literal_eval(node.value))
    sys.exit("NATURES not found in spawn.py")


def main() -> None:
    natures = project_natures()
    names: dict[str, dict[str, str]] = {}
    for key in natures:
        doc = fetch(f"https://pokeapi.co/api/v2/nature/{key}")
        by_lang = {n["language"]["name"]: n["name"] for n in doc.get("names") or []}
        if "ja" not in by_lang and "ja-Hrkt" in by_lang:
            by_lang["ja"] = by_lang["ja-Hrkt"]
        names[key] = {lang: by_lang[lang] for lang in LANGS if lang in by_lang}
        missing = [lang for lang in LANGS if lang not in names[key]]
        print(f"{key}: {names[key].get('fr')}" + (f"  (manque : {missing})" if missing else ""))

    for lang in (*LANGS, "de", "pl"):
        path = APP / "data" / "i18n" / f"{lang}.json"
        text = path.read_text(encoding="utf-8", newline="")
        eol = "\r\n" if "\r\n" in text else "\n"
        text = text.replace("\r\n", "\n")
        if '"nature.hardy"' in text:
            print("déjà présent :", lang)
            continue
        match = re.search(r'  "nav\.achievements":[^\n]*\n', text)
        if not match:
            sys.exit(f"{lang}: repère nav.achievements introuvable")
        lines = "".join(f'  "nature.{key}": {json.dumps(names[key].get(lang) or names[key]["en"], ensure_ascii=False)},\n'
                        for key in sorted(natures))
        text = text[:match.start()] + lines + text[match.start():]
        path.write_text(text.replace("\n", eol), encoding="utf-8", newline="")
        print("ok :", lang)


if __name__ == "__main__":
    main()
