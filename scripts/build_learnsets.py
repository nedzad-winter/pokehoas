#!/usr/bin/env python3
"""Builds `pokemon_encounter/app/data/learnsets.json`: which species learns
which move of `data/moves.json`, and how/when - as in the main games.

Source: Pokémon Showdown's learnset data (`learnsets.json` + `pokedex.json`),
which follows Bulbapedia for Scarlet/Violet. Codes look like `9L15` (gen 9,
level 15), `9M` (TM), `9E` (egg move), `9R` (Volt Tackle via Light Ball).

* Generation: Scarlet/Violet (gen 9) for every species that is in it. A
  species that never made it into SV uses the newest generation it has
  learnset data for (`gen` in the output), e.g. gen 8 (Sword/Shield, BDSP).
* Level-up: lowest level. Moves learned on evolution and moves only the Move
  Reminder offers count as level 1 - Showdown already stores them like that.
* Egg moves: only listed for the base stage of a line (Showdown/Bulbapedia
  move pages convention). `R` (Volt Tackle) is treated as an egg move.
* Only base forms (no regional or other formes) - the game's species table is
  one row per National Dex number.
* Move tutors are left out: SV has none, and the game has no tutor.

Output (species id -> entry, keys sorted):

    {"25": {"gen": 9, "level": {"thunder_shock": 1, ...}, "tm": [...], "egg": [...]}}

Run from the repo root (network access to play.pokemonshowdown.com, or a
directory that already holds both files):

    python3 scripts/build_learnsets.py
    python3 scripts/build_learnsets.py --cache /path/to/showdown-data
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "pokemon_encounter" / "app" / "data"
SHOWDOWN = "https://play.pokemonshowdown.com/data"
MAX_DEX = 1025


def _load(name: str, cache: Path | None) -> dict:
    if cache is not None:
        return json.loads((cache / name).read_text(encoding="utf-8"))
    request = urllib.request.Request(f"{SHOWDOWN}/{name}", headers={"User-Agent": "PokeHOAS"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _showdown_id(move_key: str) -> str:
    """`will_o_wisp` -> `willowisp` (Showdown ids are lowercase alphanumerics)."""
    return move_key.replace("_", "")


def build(learnsets: dict, pokedex: dict, move_keys: list[str]) -> dict[str, dict]:
    by_showdown = {_showdown_id(key): key for key in move_keys}
    out: dict[str, dict] = {}
    for sd_key, entry in pokedex.items():
        num = entry.get("num", 0)
        if not 1 <= num <= MAX_DEX or entry.get("forme") or entry.get("baseSpecies"):
            continue
        learnset = (learnsets.get(sd_key) or {}).get("learnset") or {}
        relevant = {by_showdown[m]: codes for m, codes in learnset.items() if m in by_showdown}
        # Newest generation with any level-up/TM/egg data for this species.
        gens = [int(code[0]) for codes in learnset.values() for code in codes
                if code[:1].isdigit() and code[1:2] in ("L", "M", "E")]
        if not gens:
            continue
        gen = max(gens)
        level: dict[str, int] = {}
        tm: set[str] = set()
        egg: set[str] = set()
        for key, codes in relevant.items():
            for code in codes:
                if not code.startswith(str(gen)):
                    continue
                kind, rest = code[1], code[2:]
                if kind == "L":
                    lvl = max(1, int(rest))
                    level[key] = min(level.get(key, lvl), lvl)
                elif kind == "M":
                    tm.add(key)
                elif kind in ("E", "R"):
                    egg.add(key)
        out[str(num)] = {
            "gen": gen,
            "level": dict(sorted(level.items())),
            "tm": sorted(tm),
            "egg": sorted(egg),
        }
    return dict(sorted(out.items(), key=lambda item: int(item[0])))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cache", type=Path, help="directory with learnsets.json + pokedex.json")
    args = parser.parse_args()

    move_keys = list(json.loads((DATA / "moves.json").read_text(encoding="utf-8")))
    learnsets = _load("learnsets.json", args.cache)
    pokedex = _load("pokedex.json", args.cache)
    result = build(learnsets, pokedex, move_keys)

    missing = [n for n in range(1, MAX_DEX + 1) if str(n) not in result]
    if missing:
        raise SystemExit(f"no learnset for species {missing}")
    lines = ",\n".join(f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}" for k, v in result.items())
    (DATA / "learnsets.json").write_text("{\n" + lines + "\n}\n", encoding="utf-8")
    by_gen: dict[int, int] = {}
    for entry in result.values():
        by_gen[entry["gen"]] = by_gen.get(entry["gen"], 0) + 1
    print(f"{len(result)} species written, by generation: {dict(sorted(by_gen.items()))}")


if __name__ == "__main__":
    main()
