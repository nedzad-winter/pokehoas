"""Tests fuer die Kampfanimationsgeschwindigkeit (Battle animation speed):
pro Nutzer waehlbar, rein ein Frontend-CSS-Timing-Schalter (siehe
www/index.html renderBattle/applyAnimationSpeed) - das Backend speichert und
validiert nur die Wahl.

Aufruf:  python3 tests/test_animation_speed.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-animation-speed-test-")
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
# Eine Datenbank aus der Zeit VOR der Option: app_user ohne Spalte animation_speed.
legacy = Path(TMP) / "legacy.sqlite"
conn = sqlite3.connect(legacy)
conn.row_factory = sqlite3.Row
conn.executescript(
    "CREATE TABLE app_user (id TEXT PRIMARY KEY, name TEXT NOT NULL, display_name TEXT,"
    " starter_species_id INTEGER, language TEXT NOT NULL DEFAULT 'en',"
    " created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);"
    "INSERT INTO app_user (id, name, created_at, last_seen_at) VALUES ('old','Old','x','x');"
)
db._migrate_animation_speed(conn)
check("Migration legt die Spalte animation_speed an",
      "animation_speed" in db._table_columns(conn, "app_user"))
check("Bestehende Nutzer bleiben nach der Migration bei 'off' (bisheriges Verhalten - keine Animationen)",
      conn.execute("SELECT animation_speed FROM app_user WHERE id='old'").fetchone()[0] == "off")
conn.execute("UPDATE app_user SET animation_speed = 'fast' WHERE id='old'")
db._migrate_animation_speed(conn)
check("Migration ist idempotent und ueberschreibt keine Wahl",
      conn.execute("SELECT animation_speed FROM app_user WHERE id='old'").fetchone()[0] == "fast")
conn.close()

# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")

check("Neue Nutzer starten mit 'normal' (Animationen an, Standardgeschwindigkeit)",
      db.user_animation_speed("alice") == "normal")
check("Unbekannter Nutzer faellt auf den Standard zurueck", db.user_animation_speed("nobody") == "normal")
try:
    db.set_user_animation_speed("alice", "bogus")
    check("Ungueltige Geschwindigkeit wird abgelehnt", False)
except ValueError:
    check("Ungueltige Geschwindigkeit wird abgelehnt", True)

for speed in db.ANIMATION_SPEEDS:
    db.set_user_animation_speed("alice", speed)
    check(f"Geschwindigkeit '{speed}' wird gespeichert", db.user_animation_speed("alice") == speed)
db.set_user_animation_speed("alice", "off")

# ---------------------------------------------------------------- API
me = users_api.me(current_user=db.get_user("alice"), is_admin=False)
check("/users/me meldet Geschwindigkeit und die moeglichen Werte",
      me["animation_speed"] == "off" and me["animation_speeds"] == list(db.ANIMATION_SPEEDS), me)

res = users_api.set_animation_speed(
    users_api.AnimationSpeedChoice(animation_speed="slow"), current_user=db.get_user("bob")
)
check("PUT animation-speed speichert die Geschwindigkeit pro Nutzer",
      res == {"animation_speed": "slow"} and db.user_animation_speed("bob") == "slow"
      and db.user_animation_speed("alice") == "off")

try:
    users_api.set_animation_speed(
        users_api.AnimationSpeedChoice(animation_speed="bogus"), current_user=db.get_user("bob")
    )
    check("PUT mit ungueltiger Geschwindigkeit -> 400", False)
except HTTPException as exc:
    check("PUT mit ungueltiger Geschwindigkeit -> 400", exc.status_code == 400)
check("Fehlertext ist uebersetzt", i18n.t("de", "error.invalid_animation_speed") != "error.invalid_animation_speed")

check("i18n-Schluessel existieren in en und de",
      all(i18n.t(lang, key) != key for lang in ("en", "de")
          for key in ("animation_speed.title", "animation_speed.hint",
                      "animation_speed.off", "animation_speed.fast",
                      "animation_speed.normal", "animation_speed.slow",
                      "animation_speed.desc.off", "animation_speed.desc.fast",
                      "animation_speed.desc.normal", "animation_speed.desc.slow",
                      "error.invalid_animation_speed")))

print()
if failures:
    print(f"{len(failures)} FAIL")
    sys.exit(1)
print("all ok")
