"""Tests fuer die Regions-Sperre (Region lock): pro Nutzer waehlbar, wie der
wilde Spawn-Pool freigeschaltet wird.

  unlocked         alle Generationen von Anfang an
  region           Standard: besiegte Region schaltet IHRE Generation frei
  previous_region  besiegte Region schaltet die NAECHSTE Generation frei

Aufruf:  python3 tests/test_region_lock.py
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

TMP = tempfile.mkdtemp(prefix="pokemon-region-lock-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import achievements, db, i18n, seed, spawn, trainer_battle, trainers  # noqa: E402
from app.api import users as users_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


def clear_region(user_id: str, region: str) -> list[dict]:
    ids = [t["id"] for t in trainers.all_trainers() if t["region"] == region]
    for tid in ids[:-1]:
        achievements.unlock(user_id, achievements.trainer_key(tid))
    return achievements.on_trainer_defeated(user_id, ids[-1])


def picked_generations(n: int, user_id: str | None) -> set[int]:
    return {s["generation"] for s in (spawn.pick_species(user_id=user_id) for _ in range(n)) if s}


# ---------------------------------------------------------------- Migration
# Eine Datenbank aus der Zeit VOR der Option: app_user ohne Spalte region_lock.
legacy = Path(TMP) / "legacy.sqlite"
conn = sqlite3.connect(legacy)
conn.row_factory = sqlite3.Row
conn.executescript(
    "CREATE TABLE app_user (id TEXT PRIMARY KEY, name TEXT NOT NULL, display_name TEXT,"
    " starter_species_id INTEGER, language TEXT NOT NULL DEFAULT 'en',"
    " created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);"
    "INSERT INTO app_user (id, name, created_at, last_seen_at) VALUES ('old','Old','x','x');"
)
db._migrate_region_lock(conn)
check("Migration legt die Spalte region_lock an",
      "region_lock" in db._table_columns(conn, "app_user"))
check("Bestehende Nutzer stehen nach der Migration auf Option 2 ('region')",
      conn.execute("SELECT region_lock FROM app_user WHERE id='old'").fetchone()[0] == "region")
conn.execute("UPDATE app_user SET region_lock = 'unlocked' WHERE id='old'")
db._migrate_region_lock(conn)
check("Migration ist idempotent und ueberschreibt keine Wahl",
      conn.execute("SELECT region_lock FROM app_user WHERE id='old'").fetchone()[0] == "unlocked")
conn.close()

# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()
for name in ("alice", "bob", "carol"):
    db.upsert_user(name, name.title())
regions = trainers.regions()
total = len(regions)
all_gens = set(range(1, total + 1))

check("Neue Nutzer starten mit Option 2 ('region')", db.user_region_lock("alice") == "region")
check("Unbekannter Nutzer faellt auf den Standard zurueck", db.user_region_lock("nobody") == "region")
try:
    db.set_user_region_lock("alice", "bogus")
    check("Ungueltiger Modus wird abgelehnt", False)
except ValueError:
    check("Ungueltiger Modus wird abgelehnt", True)

# ------------------------------------------------- Option 2: 'region' (Standard)
gens = achievements.unlocked_generations("alice")
check("region: ohne Fortschritt nur Generation 1", gens == {1}, gens)
clear_region("alice", "kanto")
check("region: Kanto besiegt schaltet nichts frei", achievements.unlocked_generations("alice") == {1})
clear_region("alice", "johto")
check("region: Johto besiegt schaltet Gen 2 (nicht Gen 3) frei",
      achievements.unlocked_generations("alice") == {1, 2}, achievements.unlocked_generations("alice"))

# ------------------------------------------------- Option 1: 'unlocked'
db.set_user_region_lock("bob", "unlocked")
check("unlocked: alle Generationen ohne jeden Fortschritt",
      achievements.unlocked_generations("bob") == all_gens, achievements.unlocked_generations("bob"))
picks = picked_generations(1500, "bob")
check("unlocked: der Spawn-Pool enthaelt hoehere Generationen sofort",
      len(picks) > 1 and picks <= all_gens, picks)
check("Bobs Modus beeinflusst Alice nicht (pro Nutzer)",
      achievements.unlocked_generations("alice") == {1, 2})

# ------------------------------------------------- Option 3: 'previous_region'
db.set_user_region_lock("carol", "previous_region")
check("previous_region: ohne Fortschritt nur Generation 1",
      achievements.unlocked_generations("carol") == {1})
check("previous_region: Spawns bleiben bei Gen 1", picked_generations(300, "carol") == {1})
clear_region("carol", "kanto")
check("previous_region: Kanto (Gen 1) besiegt schaltet Gen 2 frei",
      achievements.unlocked_generations("carol") == {1, 2}, achievements.unlocked_generations("carol"))
check("previous_region: Gen 3 bleibt gesperrt", 3 not in achievements.unlocked_generations("carol"))
clear_region("carol", "johto")
check("previous_region: Johto besiegt schaltet Gen 3 frei",
      achievements.unlocked_generations("carol") == {1, 2, 3}, achievements.unlocked_generations("carol"))
last = regions[-1]["key"]
for region in regions:
    for t in trainers.all_trainers():
        if t["region"] == region["key"]:
            achievements.unlock("carol", achievements.trainer_key(t["id"]))
check("previous_region: die letzte Region schaltet keine (nicht existente) Generation frei",
      achievements.unlocked_generations("carol") == all_gens, achievements.unlocked_generations("carol"))
check("unlock_generation: letzte Region hat im Modus previous_region nichts freizuschalten",
      achievements.unlock_generation(last, "previous_region") is None)

# Wechsel des Modus wirkt sofort und behaelt den Fortschritt.
db.set_user_region_lock("alice", "previous_region")
check("Moduswechsel region -> previous_region: Kanto+Johto besiegt = Gen 1-3",
      achievements.unlocked_generations("alice") == {1, 2, 3}, achievements.unlocked_generations("alice"))
db.set_user_region_lock("alice", "region")
check("Moduswechsel zurueck: wieder Gen 1-2", achievements.unlocked_generations("alice") == {1, 2})

# ------------------------------------------------- Haushaltsweiter Pool
check("Ohne Nutzer: Vereinigung ueber alle Nutzer (jeweils nach eigenem Modus)",
      achievements.unlocked_generations() == all_gens)

# ------------------------------------------------- Achievement-Texte
def region_desc(user_mode: str, region: str, lang: str = "en") -> str:
    defs = {d["key"]: d for d in achievements.definitions(lang, user_mode)}
    return defs[achievements.region_key(region)]["description"]


check("region: Johto-Erfolg nennt Generation 2 und den Spawn-Pool",
      "generation 2" in region_desc("region", "johto") and "spawn pool" in region_desc("region", "johto"))
check("region: Kanto-Erfolg verspricht nichts", "spawn pool" not in region_desc("region", "kanto"))
check("previous_region: Kanto-Erfolg schaltet Generation 2 frei",
      "generation 2" in region_desc("previous_region", "kanto"))
check("previous_region: Johto-Erfolg schaltet Generation 3 frei",
      "generation 3" in region_desc("previous_region", "johto"))
check("previous_region: letzte Region verspricht nichts",
      "spawn pool" not in region_desc("previous_region", last))
check("unlocked: kein Regions-Erfolg verspricht eine Freischaltung",
      all("spawn pool" not in region_desc("unlocked", r["key"]) for r in regions))
check("Regions-Erfolg-Text ist uebersetzt (de)",
      "Spawn-Pool" in region_desc("region", "johto", "de"), region_desc("region", "johto", "de"))
ov = achievements.overview("carol")
check("achievements.overview meldet den Modus und nutzt ihn fuer die Texte",
      ov["region_lock"] == "previous_region"
      and "generation 2" in next(a for a in ov["achievements"]
                                 if a["key"] == achievements.region_key("kanto"))["description"])

# ------------------------------------------------- Liga-Ansicht
def region_view(user_id: str) -> dict:
    view = trainer_battle.overview(user_id)
    return view["region_lock"], {r["key"]: r for r in view["regions"]}


mode, view = region_view("carol")
check("Liga-Ansicht meldet den Modus", mode == "previous_region")
check("previous_region: Kanto schaltet Gen 2 frei und das ist aktiv (Carol hat Kanto)",
      view["kanto"]["unlocks_generation"] == 2 and view["kanto"]["unlock_active"] is True)
check("previous_region: letzte Region hat nichts freizuschalten", view[last]["unlocks_generation"] is None)
mode, view = region_view("bob")
check("unlocked: keine Region schaltet etwas frei",
      mode == "unlocked" and all(r["unlocks_generation"] is None for r in view.values()))
mode, view = region_view("alice")
check("region: Kanto schaltet nichts frei, Johto Gen 2 (aktiv), Hoenn Gen 3 (noch nicht)",
      view["kanto"]["unlocks_generation"] is None and view["johto"]["unlocks_generation"] == 2
      and view["johto"]["unlock_active"] is True and view["hoenn"]["unlocks_generation"] == 3
      and view["hoenn"]["unlock_active"] is False)

# ------------------------------------------------- API
dave = db.upsert_user("dave", "Dave")
me = users_api.me(current_user=dave, is_admin=False)
check("/users/me meldet Modus und die moeglichen Modi",
      me["region_lock"] == "region" and me["region_lock_modes"] == list(db.REGION_LOCK_MODES), me)
res = users_api.set_region_lock(users_api.RegionLockChoice(region_lock="unlocked"), current_user=dave)
check("PUT /users/me/region-lock speichert den Modus",
      res["region_lock"] == "unlocked" and db.user_region_lock("dave") == "unlocked")
try:
    users_api.set_region_lock(users_api.RegionLockChoice(region_lock="bogus"), current_user=dave)
    check("PUT mit ungueltigem Modus -> 400", False)
except HTTPException as exc:
    check("PUT mit ungueltigem Modus -> 400", exc.status_code == 400, exc.detail)
check("Fehlertext ist uebersetzt", i18n.t("de", "error.invalid_region_lock") != "error.invalid_region_lock")

# Starter-Wahl ("frisch starten") nimmt den Modus gleich mit.
erin = db.upsert_user("erin", "Erin")
users_api.choose_starter(users_api.StarterChoice(species_id=users_api.STARTER_DEX_IDS[0],
                                                 region_lock="previous_region"), current_user=erin)
check("Starter-Wahl setzt die gewaehlte Regions-Sperre", db.user_region_lock("erin") == "previous_region")
frank = db.upsert_user("frank", "Frank")
users_api.choose_starter(users_api.StarterChoice(species_id=users_api.STARTER_DEX_IDS[0]),
                         current_user=frank)
check("Starter-Wahl ohne Angabe behaelt den Standard 'region'", db.user_region_lock("frank") == "region")
gina = db.upsert_user("gina", "Gina")
try:
    users_api.choose_starter(users_api.StarterChoice(species_id=users_api.STARTER_DEX_IDS[0],
                                                     region_lock="bogus"), current_user=gina)
    check("Starter-Wahl mit ungueltiger Sperre -> 400", False)
except HTTPException as exc:
    check("Starter-Wahl mit ungueltiger Sperre -> 400", exc.status_code == 400)
check("... und der Nutzer hat danach noch keinen Starter",
      db.get_user("gina")["starter_species_id"] is None)

print()
if failures:
    print(f"{len(failures)} FAIL")
    sys.exit(1)
print("all ok")
