"""Tests fuer die Favoriten-Markierung eigener Pokemon (Team oder Box), rein
kosmetisch - dient als Filter in der Box (siehe www/index.html boxView).

Aufruf:  python3 tests/test_favorite.py
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

TMP = tempfile.mkdtemp(prefix="pokemon-favorite-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from fastapi import HTTPException

from app import battle, db, i18n, seed  # noqa: E402
from app.api import team as team_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


def expect_http(label: str, status: int, fn, *args) -> None:
    try:
        fn(*args)
        check(label, False, "keine Exception")
    except HTTPException as exc:
        check(label, exc.status_code == status, f"{exc.status_code}: {exc.detail}")


NOW = datetime.now(timezone.utc).isoformat()
IVS = {k: 10 for k in ("hp", "atk", "def", "spa", "spd", "spe")}


# ---------------------------------------------------------------- Migration
# Eine Datenbank aus der Zeit VOR der Favoriten-Spalte.
legacy = Path(TMP) / "legacy.sqlite"
conn = sqlite3.connect(legacy)
conn.row_factory = sqlite3.Row
conn.executescript(
    "CREATE TABLE owned_pokemon (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,"
    " species_id INTEGER NOT NULL, level INTEGER NOT NULL DEFAULT 5);"
    "INSERT INTO owned_pokemon (user_id, species_id, level) VALUES ('old', 1, 5);"
)
db._migrate_favorite(conn)
check("Migration legt die Spalte favorite an", "favorite" in db._table_columns(conn, "owned_pokemon"))
check("Bestehende Pokemon sind nach der Migration nicht favorisiert (bisheriges Verhalten)",
      conn.execute("SELECT favorite FROM owned_pokemon WHERE user_id='old'").fetchone()[0] == 0)
conn.execute("UPDATE owned_pokemon SET favorite = 1 WHERE user_id='old'")
db._migrate_favorite(conn)
check("Migration ist idempotent und ueberschreibt keine bestehende Markierung",
      conn.execute("SELECT favorite FROM owned_pokemon WHERE user_id='old'").fetchone()[0] == 1)
conn.close()


# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")


def add_owned(user_id: str, species_id: int, party_slot: int | None = None) -> int:
    level = 10
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "party_slot, current_hp, caught_at) VALUES (?,?,?,?,0,?,?,?,?,?)",
        (user_id, species_id, level, battle.exp_for_level(level, "medium_slow"), json.dumps(IVS),
         "hardy", party_slot, None, NOW),
    )


def owned(owned_id: int) -> dict:
    return db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))


team_mon = add_owned("alice", 1, party_slot=1)
box_mon = add_owned("alice", 1, party_slot=None)
bob_mon = add_owned("bob", 1, party_slot=None)

# ------------------------------------------------------------------- Default
check("Neue Pokemon starten nicht als Favorit", owned(box_mon)["favorite"] == 0)
described = team_api.describe_owned(owned(box_mon))
check("describe_owned meldet favorite=False per Default", described["favorite"] is False)

# --------------------------------------------------------------------- API
result = team_api.set_favorite(
    box_mon, team_api.FavoriteChange(favorite=True), current_user=db.get_user("alice")
)
check("PUT .../favorite markiert als Favorit", result["favorite"] is True and owned(box_mon)["favorite"] == 1)

result = team_api.set_favorite(
    box_mon, team_api.FavoriteChange(favorite=False), current_user=db.get_user("alice")
)
check("PUT .../favorite entfernt die Markierung wieder", result["favorite"] is False and owned(box_mon)["favorite"] == 0)

# Auch Team-Mitglieder (nicht nur Box) koennen favorisiert werden.
result = team_api.set_favorite(
    team_mon, team_api.FavoriteChange(favorite=True), current_user=db.get_user("alice")
)
check("Auch Team-Pokemon (nicht nur Box) lassen sich favorisieren", result["favorite"] is True)

# Fremde Pokemon bleiben geschuetzt (wie bei jedem anderen Team-Endpunkt).
expect_http(
    "Favorit setzen auf fremdem Pokemon liefert 403", 403,
    team_api.set_favorite, bob_mon, team_api.FavoriteChange(favorite=True), db.get_user("alice"),
)
expect_http(
    "Favorit setzen auf unbekanntem Pokemon liefert 404", 404,
    team_api.set_favorite, 999999, team_api.FavoriteChange(favorite=True), db.get_user("alice"),
)

# --------------------------------------------------------------------- Team
team = team_api.get_team(current_user=db.get_user("alice"))
box_by_id = {p["id"]: p for p in team["box"]}
party_by_id = {p["id"]: p for p in team["party"]}
check("GET /api/team meldet favorite fuer Box-Pokemon", box_by_id[box_mon]["favorite"] is False)
check("GET /api/team meldet favorite fuer Team-Pokemon", party_by_id[team_mon]["favorite"] is True)

check("i18n-Schluessel existieren in en und de",
      all(i18n.t(lang, key) != key for lang in ("en", "de")
          for key in ("team.favorite_add", "team.favorite_remove", "team.favorites_only")))

print()
if failures:
    print(f"{len(failures)} FAIL")
    sys.exit(1)
print("all ok")
