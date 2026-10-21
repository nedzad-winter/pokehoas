"""Einmalige Geschenke (`app/gifts.py`): werden genau einmal vergeben, bleiben
offen solange Nutzer oder Spezies fehlen, und landen im freien Team-Platz -
bei vollem Team in der Box.

Aufruf:  python3 tests/test_gifts.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-gifts-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import db, gifts, seed  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


db.init_db()
seed.seed_from_fallback()
nedy = db.upsert_user("nedy-id", "nedy")["id"]
GIFT = {"key": "test_gift", "user_id": nedy, "species_id": 1, "level": 100}


def owned(user_id: str) -> list[dict]:
    return db.query("SELECT * FROM owned_pokemon WHERE user_id = ? ORDER BY id", (user_id,))


# --- Vergabe: Level 100 Bulbasaur in den ersten freien Team-Platz
check("Ohne Geschenk-Eintrag nichts vergeben", gifts.grant_pending(()) == 0)
check("Erster Lauf vergibt das Geschenk", gifts.grant_pending((GIFT,)) == 1)
mons = owned(nedy)
check("Genau ein Pokemon", len(mons) == 1, str(len(mons)))
mon = mons[0] if mons else {}
check("Bulbasaur", mon.get("species_id") == 1, str(mon.get("species_id")))
check("Level 100", mon.get("level") == 100, str(mon.get("level")))
check("Exp fuer Level 100", mon.get("exp", 0) > 0, str(mon.get("exp")))
check("Im Team auf Platz 1", mon.get("party_slot") == 1, str(mon.get("party_slot")))
check("HP voll", mon.get("current_hp", 0) > 0, str(mon.get("current_hp")))
dex = db.query_one("SELECT caught_count FROM dex_entry WHERE user_id = ? AND species_id = 1", (nedy,))
check("Pokedex: Bulbasaur gefangen", dex is not None and dex["caught_count"] >= 1)

# --- Kein zweites Mal
check("Zweiter Lauf vergibt nichts", gifts.grant_pending((GIFT,)) == 0)
check("Immer noch genau ein Pokemon", len(owned(nedy)) == 1)

# --- Fehlender Nutzer: offen lassen, beim naechsten Start nachholen
GIFT_LATER = {"key": "test_gift_later", "user_id": "not-yet", "species_id": 1, "level": 100}
check("Unbekannter Nutzer: nichts vergeben", gifts.grant_pending((GIFT_LATER,)) == 0)
check("Unbekannter Nutzer: nicht als erledigt markiert",
      db.query_one("SELECT 1 FROM seed_meta WHERE key = ?", ("test_gift_later",)) is None)
db.upsert_user("not-yet", "later")
check("Nutzer spaeter angelegt: jetzt vergeben", gifts.grant_pending((GIFT_LATER,)) == 1)

# --- Volles Team: Box statt Team
full = db.upsert_user("full-team", "full")["id"]
for slot in range(1, 7):
    db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, ivs, caught_at, party_slot) "
        "VALUES (?, 25, 5, '{}', '2026-01-01T00:00:00+00:00', ?)",
        (full, slot),
    )
GIFT_FULL = {"key": "test_gift_full", "user_id": full, "species_id": 1, "level": 100}
gifts.grant_pending((GIFT_FULL,))
boxed = [m for m in owned(full) if m["species_id"] == 1]
check("Volles Team: Geschenk in der Box", len(boxed) == 1 and boxed[0]["party_slot"] is None,
      str(boxed))

# --- Die echte Liste
check("Produktiv-Liste ist leer (keine personenbezogenen Geschenke im Code)", gifts.GIFTS == ())

# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
