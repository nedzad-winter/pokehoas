"""Tests for the AbilityDex endpoint (`GET /api/abilitydex`).

Run:  python3 tests/test_abilitydex.py
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

TMP = tempfile.mkdtemp(prefix="pokemon-abilitydex-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import abilities, db, seed  # noqa: E402
from app.api import abilitydex as abilitydex_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


db.init_db()
seed.seed_from_fallback()
abilities.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")
NOW = datetime.now(timezone.utc).isoformat()


def by_key(result: dict) -> dict[str, dict]:
    return {entry["key"]: entry for entry in result["abilities"]}


# ------------------------------------------------------------- catalog
result = abilitydex_api.get_abilitydex(current_user=alice)
entries = by_key(result)
check("Every catalog ability is listed", set(abilities.catalog()) <= set(entries),
      len(entries))
check("No duplicate abilities", len(entries) == len(result["abilities"]))
check("Intimidate is active in battle",
      entries["intimidate"]["active"] is True and entries["intimidate"]["inactive"] is None)
check("Air Lock is inactive with its reason",
      entries["air_lock"]["active"] is False and entries["air_lock"]["inactive"] == "weather")
check("Entries carry both languages",
      entries["blaze"]["name"] == "Blaze" and entries["blaze"]["name_de"]
      and entries["blaze"]["description"] and entries["blaze"]["description_de"])

# ------------------------------------------------- seen species only
check("Nothing seen yet: no species listed",
      all(entry["species"] == [] for entry in result["abilities"]))

# Alice has seen Charmander (#4): Blaze regular, Solar Power hidden.
db.execute(
    "INSERT INTO dex_entry (species_id, user_id, seen_count, caught_count, shiny_caught) "
    "VALUES (4, ?, 1, 0, 0)",
    (alice["id"],),
)
entries = by_key(abilitydex_api.get_abilitydex(current_user=alice))
check("Seen species shows up as regular holder",
      {"id": 4, "hidden": False} in entries["blaze"]["species"], entries["blaze"]["species"])
check("Hidden ability is flagged as hidden",
      {"id": 4, "hidden": True} in entries["solar_power"]["species"], entries["solar_power"]["species"])
check("Unseen species stay hidden (Bulbasaur not under Overgrow)",
      all(sp["id"] != 1 for sp in entries["overgrow"]["species"]))
check("Another user's sightings don't leak",
      by_key(abilitydex_api.get_abilitydex(current_user=bob))["blaze"]["species"] == [])

# ----------------------------------------------------- owned count
for ability in ("blaze", "blaze", "brand_new_ability"):
    db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "current_hp, party_slot, caught_at, ability) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (alice["id"], 4, 5, 0, 0, json.dumps({}), "hardy", None, None, NOW, ability),
    )
entries = by_key(abilitydex_api.get_abilitydex(current_user=alice))
check("Owned counts the user's Pokémon with that ability", entries["blaze"]["owned"] == 2,
      entries["blaze"]["owned"])
check("Abilities nobody owns have owned=0", entries["torrent"]["owned"] == 0)
check("Bob owns none of them",
      by_key(abilitydex_api.get_abilitydex(current_user=bob))["blaze"]["owned"] == 0)
check("Ability missing from the catalog is still listed with a readable name",
      entries["brand_new_ability"]["name"] == "Brand New Ability"
      and entries["brand_new_ability"]["active"] is False,
      entries.get("brand_new_ability"))


# ------------------------------------------------------------------ end
print()
if failures:
    print(f"{len(failures)} test(s) failed: {failures}")
    sys.exit(1)
print("All tests passed.")
