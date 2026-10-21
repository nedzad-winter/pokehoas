"""Tests fuer die Einstellung "Item beim Umzug in die Box automatisch ins
Inventar zurueckgeben" (siehe app/api/team.py: set_party, app/db.py:
box_item_return). Default ist an; ausgeschaltet bleibt das getragene Item
auf dem Pokemon in der Box liegen.

Aufruf:  python3 tests/test_box_item_return.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-box-item-return-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, db, held_items, i18n, seed  # noqa: E402
from app.api import team as team_api  # noqa: E402
from app.api import users as users_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


NOW = datetime.now(timezone.utc).isoformat()
IVS = {k: 10 for k in ("hp", "atk", "def", "spa", "spd", "spe")}
ITEM_KEY = next(iter(held_items.catalog()))


# ---------------------------------------------------------------- Migration
# Eine Datenbank aus der Zeit VOR der Option: app_user ohne Spalte box_item_return.
legacy = Path(TMP) / "legacy.sqlite"
conn = sqlite3.connect(legacy)
conn.row_factory = sqlite3.Row
conn.executescript(
    "CREATE TABLE app_user (id TEXT PRIMARY KEY, name TEXT NOT NULL, display_name TEXT,"
    " starter_species_id INTEGER, language TEXT NOT NULL DEFAULT 'en',"
    " created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);"
    "INSERT INTO app_user (id, name, created_at, last_seen_at) VALUES ('old','Old','x','x');"
)
db._migrate_box_item_return(conn)
check("Migration legt die Spalte box_item_return an",
      "box_item_return" in db._table_columns(conn, "app_user"))
check("Bestehende Nutzer bleiben nach der Migration bei an (der gewuenschte Standard)",
      conn.execute("SELECT box_item_return FROM app_user WHERE id='old'").fetchone()[0] == 1)
conn.execute("UPDATE app_user SET box_item_return = 0 WHERE id='old'")
db._migrate_box_item_return(conn)
check("Migration ist idempotent und ueberschreibt keine Wahl",
      conn.execute("SELECT box_item_return FROM app_user WHERE id='old'").fetchone()[0] == 0)
conn.close()


# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")

check("Neue Nutzer starten mit an", db.user_box_item_return("alice") is True)
check("Unbekannter Nutzer faellt auf den Standard (an) zurueck", db.user_box_item_return("nobody") is True)

db.set_user_box_item_return("alice", False)
check("Ausschalten wird gespeichert", db.user_box_item_return("alice") is False)
db.set_user_box_item_return("alice", True)
check("Wieder Einschalten wird gespeichert", db.user_box_item_return("alice") is True)


def add_owned(user_id: str, party_slot: int | None, held_item: str | None = None) -> int:
    level = 10
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "party_slot, held_item, current_hp, caught_at) VALUES (?,?,?,?,0,?,?,?,?,?,?)",
        (user_id, 1, level, battle.exp_for_level(level, "medium_slow"), json.dumps(IVS),
         "hardy", party_slot, held_item, None, NOW),
    )


def owned(owned_id: int) -> dict:
    return db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))


def inventory_qty(user_id: str, item_key: str) -> int:
    row = db.query_one(
        "SELECT quantity FROM inventory WHERE user_id = ? AND item_key = ?", (user_id, item_key)
    )
    return row["quantity"] if row else 0


# ---------------------------------------------------- Default: item wandert zurueck
holder = add_owned("alice", party_slot=1, held_item=ITEM_KEY)
keeper = add_owned("alice", party_slot=2, held_item=None)

team_api.set_party(team_api.PartyOrder(order=[keeper]), current_user=db.get_user("alice"))

check("Item-Traeger landet in der Box", owned(holder)["party_slot"] is None)
check("Getragenes Item wird beim Umzug in die Box abgelegt", owned(holder)["held_item"] is None)
check("Abgelegtes Item landet im Inventar",
      inventory_qty("alice", ITEM_KEY) == 1, inventory_qty("alice", ITEM_KEY))

# --------------------------------------------------- Ausgeschaltet: Item bleibt
db.set_user_box_item_return("alice", False)
holder2 = add_owned("alice", party_slot=1, held_item=ITEM_KEY)
# keeper bleibt im Team (Slot 2 aus dem vorigen Aufruf), also muss er weiter angegeben werden.
team_api.set_party(team_api.PartyOrder(order=[keeper]), current_user=db.get_user("alice"))

check("Item-Traeger landet in der Box (Einstellung aus)", owned(holder2)["party_slot"] is None)
check("Item bleibt auf dem Pokemon, wenn die Einstellung aus ist", owned(holder2)["held_item"] == ITEM_KEY)
check("Inventar bleibt unveraendert, wenn die Einstellung aus ist",
      inventory_qty("alice", ITEM_KEY) == 1, inventory_qty("alice", ITEM_KEY))

db.set_user_box_item_return("alice", True)

# ------------------------------------------------- Bleibt im Team: Item bleibt
stays_in_team = add_owned("alice", party_slot=None, held_item=ITEM_KEY)
team_api.set_party(
    team_api.PartyOrder(order=[keeper, stays_in_team]), current_user=db.get_user("alice")
)
check("Ins Team aufgenommenes Pokemon behaelt sein Item",
      owned(stays_in_team)["held_item"] == ITEM_KEY and owned(stays_in_team)["party_slot"] == 2)

# --------------------------------------------------------------------- API
me = users_api.me(current_user=db.get_user("alice"), is_admin=False)
check("/users/me meldet box_item_return", me["box_item_return"] is True, me)

res = users_api.set_box_item_return(
    users_api.BoxItemReturnChoice(box_item_return=False), current_user=db.get_user("bob")
)
check("PUT box-item-return speichert die Wahl pro Nutzer",
      res == {"box_item_return": False} and db.user_box_item_return("bob") is False
      and db.user_box_item_return("alice") is True)

check("i18n-Schluessel existieren in en und de",
      all(i18n.t(lang, key) != key for lang in ("en", "de")
          for key in ("box_item_return.setting", "box_item_return.setting_hint")))

print()
if failures:
    print(f"{len(failures)} FAIL")
    sys.exit(1)
print("all ok")
