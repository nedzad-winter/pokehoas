"""Tests fuer die IV-Untergrenze (species.min_ivs, siehe app/economy.py) im
Pokedex-Detail: `/api/pokedex/{species_id}` liefert das schon freigeschaltene
Minimum je Statuswert, damit die Dex-Detailansicht es anzeigen kann
(www/index.html openSpeciesDex).

Aufruf:  python3 tests/test_pokedex_min_ivs.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-dex-min-ivs-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import db, economy, seed  # noqa: E402
from app.api import team as team_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
species_id = db.query("SELECT id FROM species ORDER BY id")[0]["id"]

entry = team_api.get_pokedex_entry(species_id, current_user=alice)
check("fresh species reports the starting IV floor on every stat",
      entry["min_ivs"] == {k: economy.IV_MIN_START for k in economy.IV_KEYS},
      entry["min_ivs"])

# Raising the species floor (every catch does this, see economy.raise_species_min_ivs)
# must show up in the same endpoint.
raised = dict(entry["min_ivs"])
raised["atk"] = economy.IV_MAX
db.execute("UPDATE species SET min_ivs = ? WHERE id = ?",
           (__import__("json").dumps(raised), species_id))

entry_after = team_api.get_pokedex_entry(species_id, current_user=alice)
check("raised floor value is reflected", entry_after["min_ivs"]["atk"] == economy.IV_MAX)
check("other stats stay at the starting floor",
      entry_after["min_ivs"]["hp"] == economy.IV_MIN_START)

if failures:
    print(f"\n{len(failures)} FAIL(s): {failures}")
    sys.exit(1)
print("\nAll pokedex IV floor checks passed.")
