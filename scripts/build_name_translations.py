#!/usr/bin/env python3
"""Builds pokemon_encounter/app/data/names_i18n.json: ability, move and type names in the
PokeAPI languages the UI supports (English stays in abilities.json / moves.json as `name`).
PokeAPI has no Polish names, so Polish falls back to English in the frontend.

Run from the repo root (needs network access to pokeapi.co):

    python scripts/build_name_translations.py
"""

from __future__ import annotations

import concurrent.futures
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "pokemon_encounter" / "app" / "data"
POKEAPI = "https://pokeapi.co/api/v2"
LANGS = ("de", "es", "fr", "it", "ja", "ko")


def fetch(url: str) -> dict | None:
    request = urllib.request.Request(url, headers={"User-Agent": "pokehoas-name-builder"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
        except Exception:  # noqa: BLE001 - network is best effort, retried below
            pass
        time.sleep(1 + attempt)
    return None


def names_of(doc: dict) -> dict[str, str]:
    by_lang = {n["language"]["name"]: n["name"] for n in doc.get("names") or []}
    result = {lang: by_lang[lang] for lang in LANGS if lang in by_lang}
    if "ja" not in result and "ja-Hrkt" in by_lang:
        result["ja"] = by_lang["ja-Hrkt"]
    return result


def collect(kind: str, keys: list[str]) -> tuple[dict[str, dict[str, str]], list[str]]:
    def one(key: str) -> tuple[str, dict[str, str] | None]:
        doc = fetch(f"{POKEAPI}/{kind}/{key.replace('_', '-')}")
        return key, (names_of(doc) if doc else None)

    result: dict[str, dict[str, str]] = {}
    missing: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for key, names in pool.map(one, keys):
            if names:
                result[key] = names
            else:
                missing.append(key)
    return result, missing


def main() -> None:
    abilities = list(json.loads((DATA / "abilities.json").read_text(encoding="utf-8")))
    moves = list(json.loads((DATA / "moves.json").read_text(encoding="utf-8")))
    type_list = fetch(f"{POKEAPI}/type?limit=100")
    if not type_list:
        sys.exit("PokeAPI is not reachable.")
    types = [t["name"] for t in type_list["results"] if t["name"] not in ("unknown", "shadow")]

    out: dict[str, dict[str, dict[str, str]]] = {}
    for kind, label, keys in (("ability", "abilities", abilities),
                              ("move", "moves", moves),
                              ("type", "types", types)):
        print(f"{label}: fetching {len(keys)} ...", flush=True)
        names, missing = collect(kind, keys)
        out[label] = names
        note = f" - not found: {', '.join(missing[:15])}{' ...' if len(missing) > 15 else ''}" if missing else ""
        print(f"  ok {len(names)}, missing {len(missing)}{note}", flush=True)

    parts = []
    for label, names in out.items():
        entries = ",\n".join(f"  {json.dumps(k)}: {json.dumps(names[k], ensure_ascii=False)}"
                             for k in sorted(names))
        parts.append(f"{json.dumps(label)}: {{\n{entries}\n}}")
    path = DATA / "names_i18n.json"
    path.write_text("{\n" + ",\n".join(parts) + "\n}\n", encoding="utf-8", newline="")
    print(f"written: {path} ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
