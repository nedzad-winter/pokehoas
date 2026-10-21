"""Trainer-Portraits (trainers.sprite_url_for) - siehe trainers.py.

Aufruf:  python3 tests/test_trainers.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import db, trainers  # noqa: E402

db.init_db()

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


all_t = trainers.all_trainers()
check("Trainer vorhanden", len(all_t) > 0)

for t in all_t:
    url = t.get("sprite_url")
    check(f"{t['id']} hat sprite_url", bool(url) and url.startswith(trainers.SPRITE_BASE_URL),
          str(url))
    check(f"{t['id']} sprite_url hat keine Leerzeichen/Grossbuchstaben",
          url == url.lower().replace(" ", "") if url else False, str(url))

override_ids = set(trainers._SPRITE_OVERRIDES)
known_ids = {t["id"] for t in all_t}
check("Alle Overrides zeigen auf existierende Trainer", override_ids <= known_ids,
      str(override_ids - known_ids))

# trainer_info() (trainer_battle.py) muss das sprite_url mit durchreichen -
# sowohl in der Liga-Uebersicht als auch im Kampfbildschirm.
from app import trainer_battle  # noqa: E402

for t in all_t[:5]:
    info = trainer_battle.trainer_info(t)
    check(f"trainer_info({t['id']}) enthaelt sprite_url", info.get("sprite_url") == t["sprite_url"])

print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
