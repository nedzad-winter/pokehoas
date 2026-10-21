"""Tests fuer die IV-Untergrenze im IV-Chart des Pokemon-Menues: describe_owned
(und damit GET /api/team/{id}) liefert die haushaltsweite IV-Untergrenze der
Art (species.min_ivs, siehe economy.species_min_ivs) zusaetzlich zu den IVs
des einzelnen Pokemon - die Grafik zeichnet sie als zweite, gestrichelte
Huelle (www/index.html drawIvChart).

Aufruf:  python3 tests/test_iv_floor_chart.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-iv-floor-chart-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, db, economy, i18n, seed  # noqa: E402
from app.api import team as team_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


NOW = datetime.now(timezone.utc).isoformat()
IVS = {k: 10 for k in ("hp", "atk", "def", "spa", "spd", "spe")}

db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")

SPECIES_ID = 25  # Pikachu, wie in test_economy.py


def add_owned(species_id: int) -> int:
    level = 10
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "party_slot, current_hp, caught_at) VALUES (?,?,?,?,0,?,?,?,?,?)",
        (alice["id"], species_id, level, battle.exp_for_level(level, "medium_slow"), json.dumps(IVS),
         "hardy", 1, None, NOW),
    )


def owned(owned_id: int) -> dict:
    return db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))


# --------------------------------------------------------------- Default
db.execute("UPDATE species SET min_ivs = NULL WHERE id = ?", (SPECIES_ID,))
mon = add_owned(SPECIES_ID)
described = team_api.describe_owned(owned(mon))
check("describe_owned meldet min_ivs (Default 5 auf jedem Wert)",
      described["min_ivs"] == {k: 5 for k in ("hp", "atk", "def", "spa", "spd", "spe")},
      described.get("min_ivs"))
check("min_ivs ist NICHT mit den eigenen IVs des Pokemon identisch",
      described["min_ivs"] != described["ivs"])

# --------------------------------------------------------------- Angehobene Untergrenze
db.execute("UPDATE species SET min_ivs = ? WHERE id = ?",
           (json.dumps({"hp": 5, "atk": 5, "def": 7, "spa": 5, "spd": 5, "spe": 12}), SPECIES_ID))
described = team_api.describe_owned(owned(mon))
check("describe_owned spiegelt eine angehobene Untergrenze wider",
      described["min_ivs"] == {"hp": 5, "atk": 5, "def": 7, "spa": 5, "spd": 5, "spe": 12},
      described["min_ivs"])
check("describe_owned.min_ivs stimmt mit economy.species_min_ivs ueberein",
      described["min_ivs"] == economy.species_min_ivs(SPECIES_ID))

# --------------------------------------------------------------- API
fetched = team_api.get_pokemon(mon, current_user=db.get_user("alice"))
check("GET /api/team/{id} liefert min_ivs", fetched["min_ivs"] == described["min_ivs"], fetched.get("min_ivs"))

db.execute("UPDATE species SET min_ivs = NULL WHERE id = ?", (SPECIES_ID,))

# --------------------------------------------------------------- i18n
check("i18n-Schluessel fuer die Legende existiert in en und de",
      all(i18n.t(lang, "team.iv_floor_legend") != "team.iv_floor_legend" for lang in ("en", "de")))

print()
if failures:
    print(f"{len(failures)} FAIL")
    sys.exit(1)
print("all ok")
