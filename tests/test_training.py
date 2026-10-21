"""Training (`app/training.py`): unlock at 101 level-100 Pokemon, a sparring
fight of one chosen Pokemon (team or box, started from its summary) against
six level-100 Pokemon its moves hit
super effectively (>= x2) that only know Splash, and only EXP earned (no
money, no EVs).

Aufruf:  python3 tests/test_training.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-training-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from fastapi import HTTPException  # noqa: E402

from app import battle, db, moves, seed, spawn, trainer_battle, training  # noqa: E402
from app.api import league  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")
now = datetime.now().astimezone().isoformat()
IVS = json.dumps({k: 31 for k in spawn.STAT_KEYS})

pika = db.query_one("SELECT * FROM species WHERE name = 'pikachu'")
bulba = db.query_one("SELECT * FROM species WHERE name = 'bulbasaur'")


def add_owned(user_id: str, species: dict, level: int, slot: int | None) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "caught_at, party_slot, current_hp) VALUES (?,?,?,?,0,?,?,?,?,?)",
        (user_id, species["id"], level, battle.exp_for_level(level), IVS, "hardy", now, slot,
         battle.stat_hp(species["base_hp"], 31, level)),
    )


def mark_level100(user_id: str, count: int) -> None:
    for n in range(count):
        db.execute("INSERT OR IGNORE INTO pokemon_level100 (user_id, owned_id, reached_at) "
                   "VALUES (?,?,?)", (user_id, 100000 + n, now))


# ------------------------------------------------------------ Freischaltung
check("Neuer Spieler ist gesperrt", not training.unlocked(alice["id"]))
mark_level100(alice["id"], 100)
check("100 Level-100-Pokemon reichen noch nicht", not training.unlocked(alice["id"]))
mark_level100(alice["id"], 101)
check("101 Level-100-Pokemon schalten frei", training.unlocked(alice["id"]))
check("Overview spiegelt Zaehler und Schwelle",
      training.overview(alice["id"]) == {"unlocked": True, "count": 101, "required": 101, "level": 100})
check("Arena-Uebersicht hat keinen Training-Block mehr", "training" not in trainer_battle.overview(alice["id"]))
check("GET /api/training liefert den Status",
      league.training_status(current_user=alice) == training.overview(alice["id"]))

# Bob trainiert ein BOX-Pokemon; im Team steht ein anderes.
bob_box_id = add_owned(bob["id"], pika, 25, None)
bob_team_id = add_owned(bob["id"], bulba, 100, 1)
try:
    trainer_battle.start(training.TRAINER_ID, bob["id"], owned_id=bob_box_id)
    check("Gesperrter Spieler kann nicht trainieren", False)
except HTTPException as exc:
    check("Gesperrter Spieler kann nicht trainieren (409)", exc.status_code == 409, exc.detail)


# ------------------------------------------------------- Sparring-Team
def mults(types: list[str], row: dict) -> list[float]:
    defender = [t for t in (row["type1"], row["type2"]) if t]
    return [battle.type_effectiveness(t, defender) for t in types]


ALL_SPECIES = db.query("SELECT * FROM species")

roster = training.roster(pika["id"], ["thunderbolt", "thunder_wave", "growl"])
check("Sechs Gegner", len(roster) == 6, len(roster))
check("Alle Gegner Level 100", all(level == 100 for _, level in roster))
check("Nur Elektro-Attacken: alle Gegner schwach gegen Elektro (>= x2)",
      all(mults(["electric"], row)[0] >= 2 for row, _ in roster),
      [(row["type1"], row["type2"]) for row, _ in roster])
check("Status-Attacken zaehlen nicht mit",
      training.attack_types(pika["id"], ["thunderbolt", "thunder_wave", "growl"]) == ["electric"])
check("Ohne Schadens-Attacke zaehlen die eigenen Typen",
      training.attack_types(pika["id"], ["growl"]) == ["electric"])
pool = {row["id"] for row in ALL_SPECIES if mults(["electric"], row)[0] >= 2}
seen_ids: set[int] = set()
for _ in range(60):
    seen_ids |= {row["id"] for row, _ in training.roster(pika["id"], ["thunderbolt"])}
check("Ziehung nur aus dem Schwaechen-Pool, nicht immer dieselben", seen_ids <= pool and len(seen_ids) > 6,
      len(seen_ids))
check("x4-Kombination (Wasser/Flug) zaehlt als Schwaeche",
      training._multipliers(["electric"], {"type1": "water", "type2": "flying"}) == [4])
check("Kein Gegner von Elektro-Attacken ist Boden (immun)",
      all("ground" not in (row["type1"], row["type2"]) for row, _ in roster))

# Zapdos-Fall: Elektro-Pokemon, aber die Attacke entscheidet - nicht der Typ.
zap_types = ["electric", "flying"]
zap_roster = training.roster(pika["id"], ["thunder", "drill_peck"])
both = [row for row in ALL_SPECIES if all(m >= 2 for m in mults(zap_types, row))]
if both:
    check("Donner + Bohrschnabel: Gegner von BEIDEN sehr effektiv getroffen",
          all(all(m >= 2 for m in mults(zap_types, row)) for row, _ in zap_roster),
          [(row["type1"], row["type2"]) for row, _ in zap_roster])
else:
    check("Donner + Bohrschnabel: Gegner von einer sehr effektiv, gegen keine immun",
          all(max(mults(zap_types, row)) >= 2 and min(mults(zap_types, row)) > 0
              for row, _ in zap_roster),
          [(row["type1"], row["type2"]) for row, _ in zap_roster])
check("Zapdos mit Donner bekommt keine Elektro-Gegner",
      all("electric" not in (row["type1"], row["type2"])
          for row, _ in training.roster(pika["id"], ["thunder"])))

# Modus "type": erster Typ des Pokemon, x4-Schwaechen zuerst (Attacken egal).
# Die Fallback-Daten haben keine x4-Arten, deshalb drei Testarten dazu.
for sid, name, t1, t2 in [(9001, "testgyarados", "water", "flying"),
                          (9002, "testrhyhorn", "ground", "rock"),
                          (9003, "testparasect", "grass", "bug")]:
    db.execute("INSERT INTO species (id, name, type1, type2) VALUES (?,?,?,?)", (sid, name, t1, t2))
fire_mon = db.query_one("SELECT * FROM species WHERE type1 = 'fire' LIMIT 1")
type_roster = training.roster(pika["id"], ["tackle"], mode=training.MODE_TYPE)
check("Typ-Modus (Elektro): sechs Gegner, alle x4-schwach (Wasser/Flug)",
      len(type_roster) == 6 and all(mults(["electric"], row)[0] >= 4 for row, _ in type_roster),
      [(row["type1"], row["type2"]) for row, _ in type_roster])
check("Typ-Modus ignoriert die Attacken (Pikachu mit Erdbeben)",
      all(mults(["electric"], row)[0] >= 4 for row, _ in
          training.roster(pika["id"], ["earthquake"], mode=training.MODE_TYPE)))
check("Typ-Modus nutzt den ERSTEN Typ (Bisasam = Pflanze: Boden/Gestein)",
      all(mults(["grass"], row)[0] >= 4 for row, _ in
          training.roster(bulba["id"], ["tackle"], mode=training.MODE_TYPE)))
check("Typ-Modus (Feuer): Pflanze/Kaefer",
      all(mults(["fire"], row)[0] >= 4 for row, _ in
          training.roster(fire_mon["id"], ["tackle"], mode=training.MODE_TYPE)))
check("Typ-Modus ohne x4-Gegner (Wasser) faellt auf x2 zurueck",
      all(mults(["water"], row)[0] >= 2 for row, _ in
          training.roster(db.query_one("SELECT id FROM species WHERE type1 = 'water' AND id < 9000")["id"],
                          ["tackle"], mode=training.MODE_TYPE)))
check("roster() ohne mode nutzt die Attacken (Gras-Pokemon mit Donner -> Wasser/Flug)",
      all(mults(["electric"], row)[0] >= 2 for row, _ in training.roster(bulba["id"], ["thunder"])))

# Nur Normal-Attacken: nichts ist sehr effektiv - neutrale Gegner.
normal_roster = training.roster(pika["id"], ["tackle"])
check("Nur Normal-Attacken: sechs Gegner, keiner resistent oder immun",
      len(normal_roster) == 6 and all(mults(["normal"], row)[0] >= 1 for row, _ in normal_roster))
check("Splash ist nicht in der Attacken-Liste der Arten", "splash" not in moves.all_moves())
check("Splash ist ueber moves.get abrufbar", moves.get("splash")["splash"] is True)
check("Keine Art lernt Splash",
      all("splash" not in moves.moveset_for(dict(row), 100)
          for row in db.query("SELECT * FROM species")))

bob_before = dict(db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (bob_box_id,)))
money_before = db.query_one("SELECT money FROM app_user WHERE id = ?", (bob["id"],))["money"]
mark_level100(bob["id"], training.UNLOCK_COUNT)


def _route_error() -> int | None:
    try:
        league.start_trainer_battle(training.TRAINER_ID, current_user=bob)
    except HTTPException as exc:
        return exc.status_code
    return None


def start_error(owned_id: int | None, user_id: str = bob["id"]) -> tuple[int, str] | None:
    try:
        trainer_battle.start(training.TRAINER_ID, user_id, owned_id=owned_id)
    except HTTPException as exc:
        return exc.status_code, exc.detail
    return None


check("Ohne Pokemon kein Training (404)", (start_error(None) or (0,))[0] == 404, start_error(None))
check("Alter Arena-Endpunkt (/trainers/training/battle) startet kein Training",
      _route_error() == 404)
check("Fremdes Pokemon geht nicht (404)", (start_error(bob_box_id, alice["id"]) or (0,))[0] == 404)
fainted_id = add_owned(bob["id"], pika, 30, None)
db.execute("UPDATE owned_pokemon SET current_hp = 0 WHERE id = ?", (fainted_id,))
err = start_error(fainted_id)
check("Kampfunfaehiges Pokemon kann nicht trainieren (409, eigener Text)",
      err is not None and err[0] == 409 and "Pikachu" in err[1] and "heal" in err[1], err)
db.execute("DELETE FROM owned_pokemon WHERE id = ?", (fainted_id,))
check("Nach den Fehlversuchen laeuft kein Kampf", trainer_battle.ongoing_for_user(bob["id"]) is None)

# ----------------------------------------------------------------- Kampf
view = league.start_training(bob_box_id, current_user=bob)
check("Kampf startet als Trainerkampf", view["kind"] == "trainer" and view["trainer"]["kind"] == "training")
check("Nur das gewaehlte Box-Pokemon kaempft",
      [m["owned_id"] for m in view["team"]] == [bob_box_id], [m.get("owned_id") for m in view["team"]])
check("Gegner-Team hat sechs Pokemon", len(view["opponents"]) == 6, len(view["opponents"]))
opponents = trainer_battle._load(db.query_one(
    "SELECT * FROM trainer_battle_session WHERE user_id = ? AND status = 'ongoing'", (bob["id"],)))[1]
check("Gegner sind Level 100", all(o.level == 100 for o in opponents))
check("Gegner kennen nur Splash", all(o.moves == ["splash"] for o in opponents),
      [o.moves for o in opponents])
bob_moves = battle.combatant_from_owned(bob_before).moves
bob_types = training.attack_types(pika["id"], bob_moves)
check("Gegner werden von einer Attacke des Box-Pokemon sehr effektiv getroffen",
      all(max(mults(bob_types, o.species)) >= 2 for o in opponents), (bob_moves, bob_types))
check("Gegner haben keine Faehigkeit", all(o.ability is None for o in opponents),
      [o.ability for o in opponents])
check("Training zaehlt nicht fuer den Pokedex",
      db.query_one("SELECT COUNT(*) AS n FROM dex_entry WHERE user_id = ?", (bob["id"],))["n"] == 0)
check("Titel kommt aus den i18n-Texten", view["trainer"]["title"] == "Training", view["trainer"]["title"])
check("Kein Preisgeld angezeigt", view["trainer"]["prize"] == 0)

# Ein Splash-Zug des Gegners: Log-Eintrag, kein Schaden.
view = trainer_battle.act(view["id"], bob["id"], {"action": "move", "move": view["team"][0]["moves"][0]["key"]})
check("Splash hat eine Log-Zeile", any("splashes around" in line for line in view["new_log"]),
      view["new_log"][-3:])

for _ in range(400):
    if view["over"]:
        break
    view = trainer_battle.act(view["id"], bob["id"],
                              {"action": "move", "move": view["team"][0]["moves"][0]["key"]})
check("Kampf ist entschieden", view["over"] and view["outcome"] == "won", view["outcome"])

result = view["result"]
check("EXP fuer das Box-Pokemon", result["exp"].get(bob_box_id, 0) > 0, result["exp"])
check("Keine EVs, kein Preisgeld", result["evs"] == {} and result["reward"] is None, result)
check("Keine Erfolge (Level 100 nicht erreicht)", result["achievements"] == [])
check("Kein Preisgeld-Log", not any("prize" in line.lower() for line in view["log"]))
bob_after = dict(db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (bob_box_id,)))
check("Box-Pokemon bleibt in der Box", bob_after["party_slot"] is None)
check("Box-Pokemon hat EXP und Level gewonnen",
      bob_after["exp"] > bob_before["exp"] and bob_after["level"] > bob_before["level"],
      (bob_before["level"], bob_after["level"]))
check("Keine EVs gespeichert", (bob_after.get("evs") or "{}") == (bob_before.get("evs") or "{}"),
      bob_after.get("evs"))
check("Teammitglied bekommt nichts",
      db.query_one("SELECT exp FROM owned_pokemon WHERE id = ?", (bob_team_id,))["exp"]
      == battle.exp_for_level(100))
check("Kein Geld gutgeschrieben",
      db.query_one("SELECT money FROM app_user WHERE id = ?", (bob["id"],))["money"] == money_before)
check("Kein Kampf in der Historie",
      db.query_one("SELECT COUNT(*) AS n FROM battle_log WHERE user_id = ?", (bob["id"],))["n"] == 0)

# Typ-Modus ueber die Route (Revanche startet ein neues Training; der Kampf
# davor ist beendet): Pikachu (Elektro) trifft Wasser/Flug mit x4.
type_view = league.start_training(bob_box_id, current_user=bob)
type_opps = trainer_battle._load(db.query_one(
    "SELECT * FROM trainer_battle_session WHERE user_id = ? AND status = 'ongoing'", (bob["id"],)))[1]
check("Typ-Modus ist der Standard", training.DEFAULT_MODE == training.MODE_TYPE)
check("Route ohne mode (Standard): Gegner x4-schwach gegen den ersten Typ (Elektro)",
      all(mults(["electric"], o.species)[0] >= 4 for o in type_opps),
      [(o.species["type1"], o.species["type2"]) for o in type_opps])
db.execute("UPDATE trainer_battle_session SET status = 'finished' WHERE id = ?", (type_view["id"],))

# Revanche: Training startet erneut, kein Gegner-Team-Konflikt.
again = trainer_battle.start(training.TRAINER_ID, bob["id"], owned_id=bob_box_id)
check("Revanche startet einen neuen Kampf", again["over"] is False and again["id"] != view["id"])


# --------------------------------------------------- Gym-Kampf unveraendert
check("Gym-Trainer ohne Katalog bleibt unbekannt",
      trainer_battle._trainer_for("does_not_exist") is None)

print()
if failures:
    print(f"{len(failures)} FAIL: {', '.join(failures)}")
    sys.exit(1)
print("all training checks passed")
