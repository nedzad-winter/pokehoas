"""Tests fuer EV-Ertrag besiegter Gegner (PokeAPI `effort`, app/evs.py
`grant_battle_evs`): Seeding, Migration, Vergabe im Wild-/Auto-/Trainerkampf,
Grenzen, Einstellung.

Aufruf:  python3 tests/test_ev_yield.py
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-evyield-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, db, evs, i18n, seed, spawn, trainer_battle, trainers  # noqa: E402
from app import moves as moves_mod  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


random.seed(int(os.environ.get("TEST_SEED", "1234")))
db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
NOW = datetime.now().astimezone()
IVS = {k: 15 for k in spawn.STAT_KEYS}
db.set_setting("heal_regen_percent_per_hour", 0)  # deterministische KP


def species(name: str) -> dict:
    return db.query_one("SELECT * FROM species WHERE name = ?", (name,))


PIKA, KARP = species("pikachu"), species("magikarp")


def make_owned(sp: dict, level: int, slot: int | None, evs_value: dict | None = None,
               hp: int | None = None) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "current_hp, party_slot, caught_at, evs) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("alice", sp["id"], level, battle.exp_for_level(level, sp["growth_rate"]), 0,
         json.dumps(IVS), "hardy", hp, slot, NOW.isoformat(),
         json.dumps(evs_value) if evs_value is not None else None),
    )


def stored_evs(owned_id: int) -> dict[str, int]:
    return evs.as_evs(db.query_one("SELECT evs FROM owned_pokemon WHERE id = ?",
                                   (owned_id,))["evs"])


def team_of(*owned_ids: int) -> list[battle.Combatant]:
    return [battle.combatant_from_owned(
        db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (i,))) for i in owned_ids]


def with_yield(sp: dict, **yields: int) -> dict:
    return {**sp, "ev_yield": json.dumps({k: yields.get(k, 0) for k in evs.STAT_KEYS})}


# ------------------------------------------------- PokeAPI-Dokument -> Ertrag
doc = {"stats": [
    {"base_stat": 35, "effort": 0, "stat": {"name": "hp"}},
    {"base_stat": 55, "effort": 0, "stat": {"name": "attack"}},
    {"base_stat": 40, "effort": 0, "stat": {"name": "defense"}},
    {"base_stat": 50, "effort": 1, "stat": {"name": "special-attack"}},
    {"base_stat": 50, "effort": 0, "stat": {"name": "special-defense"}},
    {"base_stat": 90, "effort": 2, "stat": {"name": "speed"}},
]}
check("PokeAPI-Dokument: effort pro Statuswert",
      seed.ev_yield_from_pokemon_doc(doc)
      == {"hp": 0, "atk": 0, "def": 0, "spa": 1, "spd": 0, "spe": 2},
      seed.ev_yield_from_pokemon_doc(doc))
check("Dokument ohne stats -> leerer Ertrag", seed.ev_yield_from_pokemon_doc({}) == {})

# ------------------------------------------------------- Seeding / Migration
check("Offline-Fallback liefert Ertrag (Pikachu: 2 Initiative)",
      evs.yield_of(PIKA) == {"hp": 0, "atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 2},
      PIKA["ev_yield"])
check("Offline-Fallback liefert Ertrag (Karpador: 1 Initiative)",
      evs.yield_of(KARP)["spe"] == 1)
check("Unbekannter Ertrag (NULL) gibt nichts",
      evs.yield_of({"ev_yield": None}) == {k: 0 for k in evs.STAT_KEYS})

# Ein Datensatz ohne Ertrag (z. B. aelterer Cache) ueberschreibt einen
# gespeicherten Ertrag nicht.
seed._upsert({**{k: KARP[k] for k in ("id", "name", "name_en", "type1", "type2", "generation",
                                       "catch_rate", "base_exp", "growth_rate", "base_hp",
                                       "base_atk", "base_def", "base_spa", "base_spd",
                                       "base_spe")}})
check("Upsert ohne ev_yield behaelt den gespeicherten Ertrag",
      evs.yield_of(species("magikarp"))["spe"] == 1)
db.execute("UPDATE species SET ev_yield = NULL WHERE name = 'magikarp'")
check("Spezies ohne Ertrag landen im Hintergrund-Nachtrag",
      KARP["id"] in seed._species_missing_name_en())
db.execute("UPDATE species SET ev_yield = ? WHERE name = 'magikarp'", (KARP["ev_yield"],))
check("Vollstaendige Spezies nicht im Nachtrag",
      KARP["id"] not in seed._species_missing_name_en())

# Eine komplette Spezies-Zeile aus der DB (ev_yield als JSON-Text) laesst sich
# wieder upserten (so machen es andere Tests/Module).
seed._upsert({**species("magikarp")})
check("Upsert einer DB-Zeile behaelt den Ertrag", evs.yield_of(species("magikarp"))["spe"] == 1)

# Migration: alte DB ohne die Spalte bekommt sie, zweiter Lauf tut nichts.
legacy = sqlite3.connect(":memory:")
legacy.row_factory = sqlite3.Row
legacy.execute("CREATE TABLE species (id INTEGER PRIMARY KEY, name TEXT)")
legacy.execute("INSERT INTO species VALUES (1, 'bulbasaur')")
db._migrate_ev_yield(legacy)
db._migrate_ev_yield(legacy)
check("Migration fuegt species.ev_yield idempotent hinzu",
      "ev_yield" in db._table_columns(legacy, "species"))
check("Bestehende Zeilen bleiben ohne Ertrag (NULL)",
      legacy.execute("SELECT ev_yield FROM species").fetchone()[0] is None)

# ------------------------------------------------------ grant_battle_evs
mon = make_owned(PIKA, 20, 1)
team = team_of(mon)
log: list[str] = []
gained = evs.grant_battle_evs(log, team, [(with_yield(KARP, spe=1, atk=2), {0})], "en")
check("Teilnehmer erhaelt den Ertrag", gained == {mon: {"atk": 2, "spe": 1}}, gained)
check("EVs werden in der DB gespeichert",
      stored_evs(mon) == {"hp": 0, "atk": 2, "def": 0, "spa": 0, "spd": 0, "spe": 1},
      stored_evs(mon))
check("Combatant traegt die neuen EVs", team[0].evs["atk"] == 2 and team[0].evs["spe"] == 1)
check("Kampflog nennt die EVs", len(log) == 1 and "+2" in log[0] and "+1" in log[0], log)

# Mehrere Gegner addieren sich.
evs.grant_battle_evs([], team, [(with_yield(KARP, spe=1), {0}),
                                (with_yield(KARP, spe=1), {0})], "en")
check("Mehrere besiegte Gegner addieren sich", stored_evs(mon)["spe"] == 3, stored_evs(mon))

# Nicht-Teilnehmer bekommen nichts. Wer übergeben wird, bekommt den Ertrag,
# auch wenn er später ohnmächtig wurde - der Aufrufer wählt am KO aus.
bench = make_owned(PIKA, 20, 2)
fainted = make_owned(PIKA, 20, 3, hp=0)
team3 = team_of(mon, bench, fainted)
evs.grant_battle_evs([], team3, [(with_yield(KARP, spa=1), {0, 2})], "en")
check("Teilnehmer am Leben: +1", stored_evs(mon)["spa"] == 1)
check("Nicht-Teilnehmer: nichts", stored_evs(bench)["spa"] == 0)
check("Später ohnmächtiger Teilnehmer behält den Ertrag", stored_evs(fainted)["spa"] == 1)
check("Ungueltiger Teamindex wird ignoriert",
      evs.grant_battle_evs([], team3, [(with_yield(KARP, spa=1), {7})], "en") == {})

# Grenze pro Wert.
capped = make_owned(PIKA, 20, None, {"spe": 251})
team_c = team_of(capped)
evs.grant_battle_evs([], team_c, [(with_yield(KARP, spe=3), {0})], "en")
check("Grenze pro Wert (252) wird nicht ueberschritten", stored_evs(capped)["spe"] == 252)
log_full: list[str] = []
check("Voller Wert: nichts mehr, kein Logeintrag",
      evs.grant_battle_evs(log_full, team_c, [(with_yield(KARP, spe=3), {0})], "en") == {}
      and log_full == [])

# Gesamtgrenze.
near_total = make_owned(PIKA, 20, None, {"atk": 252, "def": 250, "hp": 5})
team_t = team_of(near_total)
evs.grant_battle_evs([], team_t, [(with_yield(KARP, hp=1, spe=3), {0})], "en")
now_evs = stored_evs(near_total)
check("Gesamtgrenze (510) wird nicht ueberschritten", evs.total(now_evs) == 510, now_evs)
check("Der Rest geht an die Werte in Reihenfolge", now_evs["hp"] == 6 and now_evs["spe"] == 2,
      now_evs)

# KP-EV erhoeht max. KP und aktuelle KP wie ein Level-Up.
hp_mon = make_owned(PIKA, 100, None, {"hp": 3}, hp=50)
team_h = team_of(hp_mon)
max_before = team_h[0].max_hp
evs.grant_battle_evs([], team_h, [(with_yield(KARP, hp=1), {0})], "en")
check("4. KP-EV erhoeht die max. KP um 1", team_h[0].max_hp == max_before + 1,
      (max_before, team_h[0].max_hp))
check("aktuelle KP steigen mit", team_h[0].current_hp == 51, team_h[0].current_hp)
check("Neu geladenes Pokémon hat dieselben max. KP",
      battle.combatant_from_owned(db.query_one(
          "SELECT * FROM owned_pokemon WHERE id = ?", (hp_mon,))).max_hp == max_before + 1)

# Spezies ohne Ertrag / Einstellung aus.
check("Gegner ohne Ertrag: nichts",
      evs.grant_battle_evs([], team_of(mon), [({**KARP, "ev_yield": None}, {0})], "en") == {})
db.set_setting("ev_battle_gain_enabled", False)
before = stored_evs(mon)
check("Einstellung aus: keine EVs",
      evs.grant_battle_evs([], team_of(mon), [(with_yield(KARP, atk=1), {0})], "en") == {}
      and stored_evs(mon) == before)
db.set_setting("ev_battle_gain_enabled", True)
check("Einstellung hat einen Standardwert (an)",
      db.DEFAULT_SETTINGS["ev_battle_gain_enabled"] is True)

# Grenzen als Einstellung gelten auch hier.
db.set_setting("ev_max_per_stat", 2)
lim = make_owned(PIKA, 20, None)
evs.grant_battle_evs([], team_of(lim), [(with_yield(KARP, atk=5), {0})], "en")
check("ev_max_per_stat gilt auch fuer Kampf-EVs", stored_evs(lim)["atk"] == 2)
db.set_setting("ev_max_per_stat", db.DEFAULT_SETTINGS["ev_max_per_stat"])

# --------------------------------------------- Wildkampf: Sieg vs. Fang
for row in db.query("SELECT id FROM owned_pokemon"):
    db.execute("UPDATE owned_pokemon SET party_slot = NULL WHERE id = ?", (row["id"],))
fighter = make_owned(PIKA, 30, 1)
weak = battle.Combatant(species=KARP, level=3, ivs=dict(IVS), is_wild=True)
b = battle.Battle(wild=weak, team=team_of(fighter), active=0, lang="en")
b.participants = {0}
weak.current_hp = 0
result = battle.on_battle_won("alice", b)
check("Sieg: on_battle_won meldet die EVs", result["evs"] == {fighter: {"spe": 1}}, result)
check("Sieg: EV in der DB", stored_evs(fighter)["spe"] == 1)
check("Sieg: Kampflog nennt die EVs",
      any("EV" in line and "Spe" in line for line in b.log), b.log[-3:])

fighter2 = make_owned(PIKA, 30, None)
b2 = battle.Battle(wild=battle.Combatant(species=KARP, level=3, ivs=dict(IVS), is_wild=True),
                   team=team_of(fighter2), active=0, lang="en")
b2.participants = {0}
battle.award_battle_exp(b2)  # Fang teilt sich diesen Pfad mit dem Sieg
check("Fang (nichts besiegt) gibt keine EVs", stored_evs(fighter2)["spe"] == 0)

# ------------------------------------------------------------- Autokampf
db.execute("UPDATE owned_pokemon SET party_slot = NULL")
auto_mon = make_owned(PIKA, 40, 1)
enc_id = spawn.create_encounter(KARP, source_entity="binary_sensor.flur_bewegung")
auto = battle.auto_battle(db.query_one("SELECT * FROM encounter WHERE id = ?", (enc_id,)),
                          "alice")
check("Autokampf gewonnen", auto["outcome"] == "won", auto["outcome"])
check("Autokampf vergibt EVs", stored_evs(auto_mon)["spe"] == 1, stored_evs(auto_mon))

# --------------------------------------------------------- Arena/Liga-Kampf
# The team is capped at the trainer's highest level - with a level-3 team the
# level-40 Pikachu would fight at level 3 and lose now and then to Bubble.
# At level 40 its Electric STAB knocks out each Magikarp in one hit.
_saved_raw, _saved_trainers = trainers._raw, trainers._trainers
trainers._raw = {
    "regions": [{"key": "kanto", "label": "Kanto"}],
    "trainers": [{"id": "test_ev", "kind": "gym_leader", "region": "kanto", "name": "Testo",
                  "team": [["magikarp", 40], ["magikarp", 40]]}],
}
trainers._trainers = None
db.execute("UPDATE owned_pokemon SET party_slot = NULL")
gym_mon = make_owned(PIKA, 40, 1)
view = trainer_battle.start("test_ev", "alice")
for _ in range(40):
    if view["over"]:
        break
    active_member = next(m for m in view["team"] if m["active"])
    strongest = max((m["key"] for m in active_member["moves"]),
                    key=lambda key: moves_mod.get(key)["power"] or 0)
    view = trainer_battle.act(view["id"], "alice", {"action": "move", "move": strongest})
check("Arenakampf gewonnen", view["outcome"] == "won", view.get("outcome"))
check("Arenakampf: EVs fuer beide besiegten Gegner", stored_evs(gym_mon)["spe"] == 2,
      stored_evs(gym_mon))
check("Arenakampf meldet die EVs im Ergebnis", view["result"]["evs"] == {gym_mon: {"spe": 2}},
      view["result"].get("evs"))
trainers._raw, trainers._trainers = _saved_raw, _saved_trainers

# ------------------------- Arenakampf: je Gegner-KO, auch bei verlorenem Kampf
# A besiegt Gegner 1 und wird danach ohnmaechtig; B besiegt Gegner 2, dann ist
# das Team komplett besiegt. Beide haben einen Ertrag verdient.
trainers._raw = {
    "regions": [{"key": "kanto", "label": "Kanto"}],
    "trainers": [{"id": "test_wipe", "kind": "gym_leader", "region": "kanto", "name": "Wipo",
                  "team": [["magikarp", 3], ["magikarp", 3], ["magikarp", 3]]}],
}
trainers._trainers = None
wipe_a, wipe_b = make_owned(PIKA, 40, None), make_owned(PIKA, 40, None)
wipe_team = team_of(wipe_a, wipe_b)


def wipe_foe() -> battle.Combatant:
    foe = battle.Combatant(species=KARP, level=3, ivs=dict(IVS), is_wild=True)
    foe.trainer = "Wipo"
    return foe


wipe = battle.Battle(wild=wipe_foe(), team=wipe_team, active=0, lang="en",
                     bench=[wipe_foe(), wipe_foe()])
wipe.wild.current_hp = 0
battle._check_faints(wipe)                 # Gegner 1 faellt, A war aktiv
wipe.team[0].current_hp = 0
battle._check_faints(wipe)                 # A ist ohnmaechtig, B muss raus
check("KO-Teilnehmer wird am KO festgehalten",
      wipe.knockouts[0]["ev_participants"] == [0], wipe.knockouts)
wipe.active = 1
wipe.participants.add(1)
wipe.needs_switch = False
wipe.wild.current_hp = 0
battle._check_faints(wipe)                 # Gegner 2 faellt durch B
wipe.team[1].current_hp = 0
battle._check_faints(wipe)                 # B ohnmaechtig, Team komplett besiegt
check("Team-Wipe ist eine verlorene Schlacht", wipe.outcome == "lost", wipe.outcome)
trainer_battle._finish(wipe, {}, trainers.get("test_wipe"), "alice")
check("Verloren: beide KO-Teilnehmer bekommen je einen EV-Ertrag",
      stored_evs(wipe_a)["spe"] == 1 and stored_evs(wipe_b)["spe"] == 1,
      (stored_evs(wipe_a), stored_evs(wipe_b)))
trainers._raw, trainers._trainers = _saved_raw, _saved_trainers

# ---------------------------------------------------------------- i18n
for key in ["battle.log.evs_gained", "evs.gain_part", "evs.hint", "guide.evs.p2",
            "settings.ev_battle_gain_enabled"]:
    en_text, de_text = i18n.catalog("en").get(key), i18n.catalog("de").get(key)
    check(f"i18n en+de: {key}", bool(en_text) and bool(de_text)
          and (en_text != de_text or key == "evs.gain_part"))
check("Guide sagt nicht mehr 'nie aus Kaempfen'",
      "never from battles" not in i18n.catalog("en")["guide.evs.p2"])

print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
