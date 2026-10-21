"""Tests fuer die Arena-Schwierigkeit (Normal / Generationsgrenze / Gleiche
Generation) bei Trainerkaempfen: pro Nutzer waehlbar. Bei 'generation_limit'
duerfen nur Teammitglieder kaempfen, deren Art-Generation die des Trainers
(ueber dessen Region) oder genau eine Generation frueher ist - juengere UND
mehr als eine Generation aeltere Pokemon sitzen den Kampf aus. Bei
'same_generation' ist das noch strenger: nur exakt die Generation des
Trainers (der Liga) darf antreten, auch eine Generation frueher nicht mehr.

Aufruf:  python3 tests/test_gym_difficulty.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-gym-difficulty-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, db, i18n, seed, spawn, trainer_battle, trainers  # noqa: E402
from app.api import users as users_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


# ---------------------------------------------------------------- Migration
# Eine Datenbank aus der Zeit VOR der Option: app_user ohne Spalte gym_difficulty.
legacy = Path(TMP) / "legacy.sqlite"
conn = sqlite3.connect(legacy)
conn.row_factory = sqlite3.Row
conn.executescript(
    "CREATE TABLE app_user (id TEXT PRIMARY KEY, name TEXT NOT NULL, display_name TEXT,"
    " starter_species_id INTEGER, language TEXT NOT NULL DEFAULT 'en',"
    " created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);"
    "INSERT INTO app_user (id, name, created_at, last_seen_at) VALUES ('old','Old','x','x');"
)
db._migrate_gym_difficulty(conn)
check("Migration legt die Spalte gym_difficulty an", "gym_difficulty" in db._table_columns(conn, "app_user"))
check("Bestehende Nutzer bleiben nach der Migration bei 'normal' (bisheriges Verhalten)",
      conn.execute("SELECT gym_difficulty FROM app_user WHERE id='old'").fetchone()[0] == "normal")
conn.execute("UPDATE app_user SET gym_difficulty = 'generation_limit' WHERE id='old'")
db._migrate_gym_difficulty(conn)
check("Migration ist idempotent und ueberschreibt keine Wahl",
      conn.execute("SELECT gym_difficulty FROM app_user WHERE id='old'").fetchone()[0] == "generation_limit")
conn.close()

# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")
now = datetime.now().astimezone()
pikachu = db.query_one("SELECT * FROM species WHERE name = 'pikachu'")   # Gen 1
chikorita = db.query_one("SELECT * FROM species WHERE name = 'chikorita'")  # Gen 2
torchic = db.query_one("SELECT * FROM species WHERE name = 'torchic'")  # Gen 3
check("Testarten haben die erwarteten Generationen",
      pikachu["generation"] == 1 and chikorita["generation"] == 2 and torchic["generation"] == 3,
      (pikachu["generation"], chikorita["generation"], torchic["generation"]))

check("Neue Nutzer starten mit 'normal'", db.user_gym_difficulty("alice") == "normal")
check("Unbekannter Nutzer faellt auf den Standard zurueck", db.user_gym_difficulty("nobody") == "normal")
try:
    db.set_user_gym_difficulty("alice", "bogus")
    check("Ungueltige Schwierigkeit wird abgelehnt", False)
except ValueError:
    check("Ungueltige Schwierigkeit wird abgelehnt", True)

check("/users/me meldet Schwierigkeit und Auswahl",
      users_api.me(current_user=db.get_user("alice"), is_admin=False)["gym_difficulties"]
      == ["normal", "generation_limit", "same_generation"])
res = users_api.set_gym_difficulty(users_api.GymDifficultyChoice(gym_difficulty="generation_limit"),
                                   current_user=db.get_user("bob"))
check("PUT gym-difficulty speichert die Wahl pro Nutzer",
      res == {"gym_difficulty": "generation_limit"} and db.user_gym_difficulty("bob") == "generation_limit"
      and db.user_gym_difficulty("alice") == "normal")
try:
    users_api.set_gym_difficulty(users_api.GymDifficultyChoice(gym_difficulty="bogus"),
                                 current_user=db.get_user("bob"))
    check("PUT mit ungueltiger Schwierigkeit -> 400", False)
except HTTPException as exc:
    check("PUT mit ungueltiger Schwierigkeit -> 400", exc.status_code == 400)
db.set_user_gym_difficulty("bob", "normal")

check("i18n-Schluessel existieren in en und de",
      all(i18n.t(lang, key) != key for lang in ("en", "de")
          for key in ("gym_difficulty.title", "gym_difficulty.normal", "gym_difficulty.generation_limit",
                      "gym_difficulty.same_generation",
                      "gym_difficulty.desc.normal", "gym_difficulty.desc.generation_limit",
                      "gym_difficulty.desc.same_generation",
                      "gym_difficulty.hint", "error.invalid_gym_difficulty",
                      "error.no_team_for_gym_difficulty", "error.no_team_for_gym_difficulty_exact",
                      "league.gen_restricted_hint")))
# battle.log.gym_difficulty_restricted / _restricted_exact take {generation} - checked with the
# real calls below.

# Synthetische Trainer in drei Regionen unterschiedlicher Generation (Kanto =
# Gen 1, Johto = Gen 2, Hoenn = Gen 3 - siehe trainers.generation_for_region:
# 1:1 nach Reihenfolge der Regionen).
_saved_raw, _saved_trainers = trainers._raw, trainers._trainers
trainers._raw = {
    "regions": [{"key": "kanto", "label": "Kanto"}, {"key": "johto", "label": "Johto"},
                {"key": "hoenn", "label": "Hoenn"}],
    "trainers": [
        {"id": "gen1_gym", "kind": "gym_leader", "region": "kanto",
         "name": "Kanto-Leiter", "team": [["magikarp", 5]]},
        {"id": "gen2_gym", "kind": "gym_leader", "region": "johto",
         "name": "Johto-Leiterin", "team": [["magikarp", 5]]},
        {"id": "gen3_gym", "kind": "gym_leader", "region": "hoenn",
         "name": "Hoenn-Leiterin", "team": [["magikarp", 5]]},
    ],
}
trainers._trainers = None


def give_pokemon(user_id: str, species: dict, slot: int) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "caught_at, party_slot) VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, species["id"], 25, battle.exp_for_level(25), 0,
         json.dumps({k: 31 for k in spawn.STAT_KEYS}), "hardy", now.isoformat(), slot),
    )


# alice: nur ein Gen-2-Pokemon im Team, Schwierigkeit 'generation_limit'.
give_pokemon("alice", chikorita, 1)
db.set_user_gym_difficulty("alice", "generation_limit")

check("normal (Standard): Gen-2-Team darf auch gegen den Gen-1-Trainer antreten",
      db.user_gym_difficulty("bob") == "normal")

try:
    trainer_battle.start("gen1_gym", "alice")
    check("generation_limit: Gen-2-Team wird gegen den Gen-1-Trainer abgelehnt", False)
except HTTPException as exc:
    check("generation_limit: Gen-2-Team wird gegen den Gen-1-Trainer abgelehnt", exc.status_code == 409,
          exc.detail)

view = trainer_battle.start("gen2_gym", "alice")
check("generation_limit: das gleiche Team darf gegen den Gen-2-Trainer antreten",
      len(view["team"]) == 1 and not view["over"])
trainer_battle.act(view["id"], "alice", {"action": "run"})

# bob: ein Gen-1- und ein Gen-2-Pokemon im Team, Schwierigkeit 'generation_limit'.
give_pokemon("bob", pikachu, 1)
give_pokemon("bob", chikorita, 2)
db.set_user_gym_difficulty("bob", "generation_limit")

view = trainer_battle.start("gen1_gym", "bob")
check("generation_limit: gegen den Gen-1-Trainer sitzt das Gen-2-Mitglied aus",
      len(view["team"]) == 1, view["team"])
check("generation_limit: das Kampflog nennt die Einschraenkung",
      i18n.t("en", "battle.log.gym_difficulty_restricted", generation=1) in view["log"])
trainer_battle.act(view["id"], "bob", {"action": "run"})

view = trainer_battle.start("gen2_gym", "bob")
check("generation_limit: gegen den Gen-2-Trainer duerfen beide antreten",
      len(view["team"]) == 2, view["team"])
trainer_battle.act(view["id"], "bob", {"action": "run"})

# normal: das gleiche Team darf uneingeschraenkt gegen den Gen-1-Trainer antreten.
db.set_user_gym_difficulty("bob", "normal")
view = trainer_battle.start("gen1_gym", "bob")
check("normal: keine Generationsbegrenzung, das ganze Team tritt an", len(view["team"]) == 2)
trainer_battle.act(view["id"], "bob", {"action": "run"})

# overview(): 'gen_restricted' markiert Trainer, fuer die aktuell niemand im
# Team geeignet ist.
db.set_user_gym_difficulty("alice", "generation_limit")
ov = trainer_battle.overview("alice")
by_id = {t["id"]: t for region in ov["regions"] for t in region["trainers"]}
check("overview: Gen-1-Trainer ist fuer Alice (nur Gen-2-Team) 'gen_restricted'",
      by_id["gen1_gym"]["gen_restricted"] is True)
check("overview: Gen-2-Trainer ist fuer Alice nicht 'gen_restricted'",
      by_id["gen2_gym"]["gen_restricted"] is False)
db.set_user_gym_difficulty("alice", "normal")
ov = trainer_battle.overview("alice")
by_id = {t["id"]: t for region in ov["regions"] for t in region["trainers"]}
check("overview: ohne generation_limit ist niemand 'gen_restricted'",
      by_id["gen1_gym"]["gen_restricted"] is False and by_id["gen2_gym"]["gen_restricted"] is False)

# charlie: nur ein Gen-1-Pokemon, Schwierigkeit 'generation_limit'. Gegen den
# Gen-3-Trainer ist das mehr als eine Generation aelter - anders als bei einer
# reinen "diese Generation oder frueher"-Regel muss das jetzt ausgeschlossen
# werden (nur die eigene oder GENAU eine frühere Generation ist erlaubt).
charlie = db.upsert_user("charlie", "Charlie")
give_pokemon("charlie", pikachu, 1)
db.set_user_gym_difficulty("charlie", "generation_limit")

try:
    trainer_battle.start("gen3_gym", "charlie")
    check("generation_limit: Gen-1-Team wird gegen den Gen-3-Trainer abgelehnt "
          "(mehr als eine Generation aelter)", False)
except HTTPException as exc:
    check("generation_limit: Gen-1-Team wird gegen den Gen-3-Trainer abgelehnt "
          "(mehr als eine Generation aelter)", exc.status_code == 409, exc.detail)

# Ein Gen-2-Pokemon (genau eine Generation aelter) darf dagegen gegen den
# Gen-3-Trainer antreten.
give_pokemon("charlie", chikorita, 2)
view = trainer_battle.start("gen3_gym", "charlie")
check("generation_limit: Gen-2-Mitglied darf gegen den Gen-3-Trainer antreten, "
      "das Gen-1-Mitglied sitzt aus", len(view["team"]) == 1, view["team"])
trainer_battle.act(view["id"], "charlie", {"action": "run"})

ov = trainer_battle.overview("charlie")
by_id = {t["id"]: t for region in ov["regions"] for t in region["trainers"]}
check("overview: Gen-3-Trainer ist fuer Charlie nicht 'gen_restricted' (Gen-2-Mitglied passt)",
      by_id["gen3_gym"]["gen_restricted"] is False)

# Nur das Gen-1-Pokemon: fuer den Gen-3-Trainer ist niemand geeignet ->
# 'gen_restricted' muss greifen (Regression fuer die alte "oder frueher"-Regel,
# die hier faelschlich False liefern wuerde).
db.execute("DELETE FROM owned_pokemon WHERE user_id = 'charlie' AND species_id = ?", (chikorita["id"],))
ov = trainer_battle.overview("charlie")
by_id = {t["id"]: t for region in ov["regions"] for t in region["trainers"]}
check("overview: Gen-3-Trainer ist fuer Charlie (nur Gen-1-Team) 'gen_restricted'",
      by_id["gen3_gym"]["gen_restricted"] is True)

# dana: ein Gen-1- und ein Gen-2-Pokemon, Schwierigkeit 'same_generation'.
# Anders als 'generation_limit' darf hier KEINE aeltere Generation antreten -
# nur exakt die Generation der Liga (des Trainers).
dana = db.upsert_user("dana", "Dana")
give_pokemon("dana", pikachu, 1)
give_pokemon("dana", chikorita, 2)
db.set_user_gym_difficulty("dana", "same_generation")

view = trainer_battle.start("gen2_gym", "dana")
check("same_generation: gegen den Gen-2-Trainer tritt nur das Gen-2-Mitglied an, "
      "das Gen-1-Mitglied sitzt aus (anders als bei generation_limit)",
      len(view["team"]) == 1, view["team"])
check("same_generation: das Kampflog nennt die strengere Einschraenkung",
      i18n.t("en", "battle.log.gym_difficulty_restricted_exact", generation=2) in view["log"])
trainer_battle.act(view["id"], "dana", {"action": "run"})

try:
    trainer_battle.start("gen3_gym", "dana")
    check("same_generation: Gen-1/Gen-2-Team wird gegen den Gen-3-Trainer abgelehnt "
          "(auch eine Generation aelter reicht hier nicht)", False)
except HTTPException as exc:
    check("same_generation: Gen-1/Gen-2-Team wird gegen den Gen-3-Trainer abgelehnt "
          "(auch eine Generation aelter reicht hier nicht)",
          exc.status_code == 409
          and exc.detail == i18n.t("en", "error.no_team_for_gym_difficulty_exact"))

ov = trainer_battle.overview("dana")
by_id = {t["id"]: t for region in ov["regions"] for t in region["trainers"]}
check("overview: Gen-3-Trainer ist fuer Dana (kein Gen-3-Mitglied) 'gen_restricted'",
      by_id["gen3_gym"]["gen_restricted"] is True)
check("overview: Gen-2-Trainer ist fuer Dana (Gen-2-Mitglied vorhanden) nicht 'gen_restricted'",
      by_id["gen2_gym"]["gen_restricted"] is False)

trainers._raw, trainers._trainers = _saved_raw, _saved_trainers

print()
if failures:
    print(f"{len(failures)} FAIL")
    sys.exit(1)
print("all ok")
