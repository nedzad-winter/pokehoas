"""Tests fuer den Team-Tab-Schalter "IVs anzeigen": pro Nutzer waehlbar,
rein ein Frontend-Anzeige-Schalter (siehe www/index.html monCard/renderTeam) -
das Backend speichert nur die Wahl, Default ist an (bisheriges Verhalten).

Aufruf:  python3 tests/test_show_team_ivs.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-show-team-ivs-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import db, i18n, seed  # noqa: E402
from app.api import users as users_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


# ---------------------------------------------------------------- Migration
# Eine Datenbank aus der Zeit VOR der Option: app_user ohne Spalte show_team_ivs.
legacy = Path(TMP) / "legacy.sqlite"
conn = sqlite3.connect(legacy)
conn.row_factory = sqlite3.Row
conn.executescript(
    "CREATE TABLE app_user (id TEXT PRIMARY KEY, name TEXT NOT NULL, display_name TEXT,"
    " starter_species_id INTEGER, language TEXT NOT NULL DEFAULT 'en',"
    " created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);"
    "INSERT INTO app_user (id, name, created_at, last_seen_at) VALUES ('old','Old','x','x');"
)
db._migrate_show_team_ivs(conn)
check("Migration legt die Spalte show_team_ivs an",
      "show_team_ivs" in db._table_columns(conn, "app_user"))
check("Bestehende Nutzer bleiben nach der Migration bei an (bisheriges Verhalten - IVs waren immer sichtbar)",
      conn.execute("SELECT show_team_ivs FROM app_user WHERE id='old'").fetchone()[0] == 1)
conn.execute("UPDATE app_user SET show_team_ivs = 0 WHERE id='old'")
db._migrate_show_team_ivs(conn)
check("Migration ist idempotent und ueberschreibt keine Wahl",
      conn.execute("SELECT show_team_ivs FROM app_user WHERE id='old'").fetchone()[0] == 0)
conn.close()

# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")

check("Neue Nutzer starten mit an", db.user_show_team_ivs("alice") is True)
check("Unbekannter Nutzer faellt auf den Standard (an) zurueck", db.user_show_team_ivs("nobody") is True)

db.set_user_show_team_ivs("alice", False)
check("Ausschalten wird gespeichert", db.user_show_team_ivs("alice") is False)
db.set_user_show_team_ivs("alice", True)
check("Wieder Einschalten wird gespeichert", db.user_show_team_ivs("alice") is True)

# ---------------------------------------------------------------- API
me = users_api.me(current_user=db.get_user("alice"), is_admin=False)
check("/users/me meldet show_team_ivs", me["show_team_ivs"] is True, me)

res = users_api.set_show_team_ivs(
    users_api.ShowTeamIvsChoice(show_team_ivs=False), current_user=db.get_user("bob")
)
check("PUT show-team-ivs speichert die Wahl pro Nutzer",
      res == {"show_team_ivs": False} and db.user_show_team_ivs("bob") is False
      and db.user_show_team_ivs("alice") is True)

check("i18n-Schluessel existieren in en und de",
      all(i18n.t(lang, key) != key for lang in ("en", "de")
          for key in ("team_ivs.setting", "team_ivs.setting_hint")))

print()
if failures:
    print(f"{len(failures)} FAIL")
    sys.exit(1)
print("all ok")
