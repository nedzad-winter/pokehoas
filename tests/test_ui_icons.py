"""Tests for the shipped UI icons: gym badges and type plates.

The profile shows an image per gym badge (www/img/badges/<trainer id>.png) and
every type tag is the game's type plate (www/img/types/<type>.png). Both are
derived from data - a new gym leader or type must not silently end up without
an icon. Regenerate the files with scripts/fetch_ui_icons.py.

Usage:  python3 tests/test_ui_icons.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WWW = ROOT / "pokemon_encounter" / "www"
DATA = ROOT / "pokemon_encounter" / "app" / "data"

TYPES = ["normal", "fire", "water", "grass", "electric", "ice", "fighting",
         "poison", "ground", "flying", "psychic", "bug", "rock", "ghost",
         "dragon", "dark", "steel", "fairy"]

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def is_png(path: Path) -> bool:
    return path.is_file() and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# ------------------------------------------------------------- Gym badges
trainers = json.loads((DATA / "trainers.json").read_text(encoding="utf-8"))
gyms = [t["id"] for t in trainers["trainers"] if t["kind"] == "gym_leader"]
check("trainers.json contains gym leaders", bool(gyms), f"{len(gyms)} leaders")

missing = [tid for tid in gyms if not is_png(WWW / "img" / "badges" / f"{tid}.png")]
check("every gym leader has a badge icon", not missing, ", ".join(missing))

badge_files = {p.stem for p in (WWW / "img" / "badges").glob("*.png")}
check("no badge icons without a gym leader", badge_files <= set(gyms),
      ", ".join(sorted(badge_files - set(gyms))))

# ------------------------------------------------------------ Type plates
missing = [t for t in TYPES if not is_png(WWW / "img" / "types" / f"{t}.png")]
check("every type has a type plate", not missing, ", ".join(missing))

type_files = {p.stem for p in (WWW / "img" / "types").glob("*.png")}
check("no type plates without a type", type_files == set(TYPES),
      ", ".join(sorted(type_files ^ set(TYPES))))

# --------------------------------------------------------------- Frontend
index = (WWW / "index.html").read_text(encoding="utf-8")
check("frontend uses the badge icons", "/static/img/badges/" in index)
check("frontend uses the type plates", "/static/img/types/" in index)
check("frontend type list matches this test",
      all(f"'{t}'" in index for t in TYPES))


# -------------------------------------------------------------------- End
print()
if failures:
    print(f"{len(failures)} test(s) failed: {failures}")
    sys.exit(1)
print("All tests passed.")
