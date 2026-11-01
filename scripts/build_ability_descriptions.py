#!/usr/bin/env python3
"""Builds pokemon_encounter/app/data/ability_descriptions_i18n.json: the official in-game
description of every ability in abilities.json, in the PokeAPI languages the UI supports.
German stays in abilities.json (`description_de`); PokeAPI has no Polish, so Polish falls
back to English in the frontend.

Run from the repo root (needs network access to pokeapi.co):

    python scripts/build_ability_descriptions.py
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
LANGS = ("es", "fr", "it", "ja", "ko")


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


def clean(text: str) -> str:
    return " ".join(text.replace("\u00ad", "").split())


def descriptions_of(doc: dict) -> dict[str, str]:
    """Latest flavor text per language (PokeAPI lists them oldest to newest)."""
    latest: dict[str, str] = {}
    for entry in doc.get("flavor_text_entries") or []:
        latest[entry["language"]["name"]] = clean(entry["flavor_text"])
    result = {lang: latest[lang] for lang in LANGS if latest.get(lang)}
    if "ja" not in result and latest.get("ja-Hrkt"):
        result["ja"] = latest["ja-Hrkt"]
    return result


def main() -> None:
    keys = list(json.loads((DATA / "abilities.json").read_text(encoding="utf-8")))

    def one(key: str) -> tuple[str, dict[str, str] | None]:
        doc = fetch(f"{POKEAPI}/ability/{key.replace('_', '-')}")
        return key, (descriptions_of(doc) if doc else None)

    result: dict[str, dict[str, str]] = {}
    missing: list[str] = []
    print(f"abilities: fetching {len(keys)} ...", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for key, texts in pool.map(one, keys):
            if texts:
                result[key] = texts
            else:
                missing.append(key)
    print(f"  ok {len(result)}, missing {len(missing)}: {', '.join(missing[:15])}")
    for lang in LANGS:
        print(f"  {lang}: {sum(1 for t in result.values() if lang in t)}")

    entries = ",\n".join(f"  {json.dumps(k)}: {json.dumps(result[k], ensure_ascii=False)}"
                         for k in sorted(result))
    path = DATA / "ability_descriptions_i18n.json"
    path.write_text("{\n" + entries + "\n}\n", encoding="utf-8", newline="")
    print(f"written: {path} ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
