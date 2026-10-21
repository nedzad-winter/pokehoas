"""Tests fuer das Muenzamulett (Amulet Coin, `held_items.json`): verdoppelt
das Preisgeld eines gewonnenen Kampfes (wild, Arena/Liga, PvP), wenn es ein
beliebiges Teammitglied traegt - wie beim Erfahrungsteiler ohne Kampfteilnahme.

Aufruf:  python3 tests/test_amulet_coin.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-amulet-coin-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, db, economy, held_items, pvp, seed, spawn, trainer_battle, trainers  # noqa: E402
from app.economy import catalog_by_key  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


# ---------------------------------------------------------------- Katalog
item = held_items.get("amulet_coin")
check("Muenzamulett ist im Item-Katalog", item is not None)
check("Effekt ist amulet_coin", item and item["effect"] == "amulet_coin", item)
check("Verdoppelt (multiplier 2.0)", item and item["multiplier"] == 2.0, item)
check("Deutscher Name ist Muenzamulett", item and item["name_de"] == "Münzamulett", item)

shop_entry = catalog_by_key().get("amulet_coin")
check("Muenzamulett ist im Shop kaufbar", shop_entry is not None and shop_entry.get("category") == "held",
      shop_entry)


# ------------------------------------------------------------ any_holds()
holder = SimpleNamespace(held_item="amulet_coin")
bench = SimpleNamespace(held_item=None)
other = SimpleNamespace(held_item="leftovers")
check("any_holds erkennt Traeger im Team", held_items.any_holds([bench, holder], "amulet_coin"))
check("any_holds ist False ohne Traeger", not held_items.any_holds([bench, other], "amulet_coin"))
check("any_holds ist False bei leerem Team", not held_items.any_holds([], "amulet_coin"))
check("any_holds liefert bool (kein Stapeln bei mehreren Traegern)",
      held_items.any_holds([holder, holder, holder], "amulet_coin") is True)
check("Muenzamulett kostet 150000", shop_entry and shop_entry.get("price") == 150000, shop_entry)


# --------------------------------------------------------- economy.award_battle_win
db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
now = datetime.now().astimezone()

pika = db.query_one("SELECT * FROM species WHERE name = 'pikachu'")
karp = db.query_one("SELECT * FROM species WHERE name = 'magikarp'")
check("Fallback-Pokédex enthaelt Pikachu und Karpador", pika is not None and karp is not None)

encounter_id = spawn.create_encounter(karp, source_entity="test.trigger")
db.execute("UPDATE encounter SET level = 20 WHERE id = ?", (encounter_id,))
row = db.query_one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))

plain = economy.award_battle_win("alice", {**row, "level": 20})
doubled = economy.award_battle_win("alice", {**row, "level": 20}, multiplier=2)
check("award_battle_win verdoppelt bei multiplier=2", doubled == plain * 2 and plain > 0,
      (plain, doubled))


IVS = {k: 31 for k in ("hp", "atk", "def", "spa", "spd", "spe")}


def make_owned(user_id: str, sp: dict, level: int, item: str | None, slot: int = 1) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "current_hp, party_slot, caught_at, held_item) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, sp["id"], level, battle.exp_for_level(level, sp["growth_rate"]), 0,
         json.dumps(IVS), "hardy", None, slot, now.isoformat(), item),
    )


def clear_team(user_id: str) -> None:
    db.execute("UPDATE owned_pokemon SET party_slot = NULL WHERE user_id = ?", (user_id,))


def fight(win_fn, view: dict, session_id: int, user_id: str, limit: int = 40) -> dict:
    for _ in range(limit):
        if view["over"]:
            break
        member = next(m for m in view["team"] if m["active"])
        view = win_fn(session_id, user_id, {"action": "move", "move": member["moves"][0]["key"]})
    return view


# ------------------------------------------------------- Wild battle (on_battle_won)
egg_id = make_owned("alice", pika, 60, "amulet_coin")
plain_id = make_owned("alice", pika, 60, None)
team_with = [battle.combatant_from_owned(db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (egg_id,)))]
team_without = [battle.combatant_from_owned(db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (plain_id,)))]

enc2 = spawn.create_encounter(karp, source_entity="test.trigger")
db.execute("UPDATE encounter SET level = 20 WHERE id = ?", (enc2,))
b_without = battle.Battle(wild=battle.Combatant(species=karp, level=20, ivs=dict(IVS), is_wild=True),
                          team=team_without, participants={0})
b_without.wild.current_hp = 0
result_without = battle.on_battle_won("alice", b_without, encounter_id=enc2)

enc3 = spawn.create_encounter(karp, source_entity="test.trigger")
db.execute("UPDATE encounter SET level = 20 WHERE id = ?", (enc3,))
b_with = battle.Battle(wild=battle.Combatant(species=karp, level=20, ivs=dict(IVS), is_wild=True),
                       team=team_with, participants={0})
b_with.wild.current_hp = 0
result_with = battle.on_battle_won("alice", b_with, encounter_id=enc3)

check("Wildkampf: Muenzamulett verdoppelt das Preisgeld",
      result_with["reward"]["money_earned"] == result_without["reward"]["money_earned"] * 2
      and result_without["reward"]["money_earned"] > 0,
      (result_without["reward"], result_with["reward"]))
db.execute("DELETE FROM owned_pokemon WHERE id IN (?,?)", (egg_id, plain_id))


# ---------------------------------------------------- Gym/League (trainer_battle)
_saved_raw, _saved_trainers = trainers._raw, trainers._trainers
trainers._raw = {
    "regions": [{"key": "kanto", "label": "Kanto"}],
    "trainers": [{
        "id": "test_amulet", "kind": "gym_leader", "region": "kanto",
        "name": "Testo", "team": [["magikarp", 3]],
    }],
}
trainers._trainers = None

clear_team("alice")
strong_plain = make_owned("alice", pika, 40, None)
money_before = economy.balance("alice")
view = trainer_battle.start("test_amulet", "alice")
view = fight(trainer_battle.act, view, view["id"], "alice")
check("Testkampf (ohne Item) wird gewonnen", view["outcome"] == "won", view.get("outcome"))
earned_plain = view["result"]["reward"]["money_earned"]
check("Preisgeld ohne Item wird gutgeschrieben", earned_plain > 0, earned_plain)

clear_team("alice")
strong_with = make_owned("alice", pika, 40, "amulet_coin")
view2 = trainer_battle.start("test_amulet", "alice")
view2 = fight(trainer_battle.act, view2, view2["id"], "alice")
check("Testkampf (mit Muenzamulett) wird gewonnen", view2["outcome"] == "won", view2.get("outcome"))
earned_with = view2["result"]["reward"]["money_earned"]
check("Arenakampf: Muenzamulett verdoppelt das Preisgeld",
      earned_with == earned_plain * 2, (earned_plain, earned_with))

trainers._raw, trainers._trainers = _saved_raw, _saved_trainers
db.execute("DELETE FROM owned_pokemon WHERE id IN (?,?)", (strong_plain, strong_with))


# --------------------------------------------------------------------- PvP
bob = db.upsert_user("bob", "Bob")
clear_team("alice")
clear_team("bob")
bob_karp = make_owned("bob", karp, 4, None)

strong_plain2 = make_owned("alice", pika, 30, None)
view3 = pvp.start_vs_user("alice", "bob")
view3 = fight(pvp.act, view3, view3["id"], "alice")
check("PvP (ohne Item) wird gewonnen", view3["outcome"] == "won", view3.get("outcome"))
pvp_earned_plain = view3["result"]["reward"]["money_earned"]
check("PvP-Preisgeld ohne Item wird gutgeschrieben", pvp_earned_plain > 0, pvp_earned_plain)

clear_team("alice")
strong_with2 = make_owned("alice", pika, 30, "amulet_coin")
view4 = pvp.start_vs_user("alice", "bob")
view4 = fight(pvp.act, view4, view4["id"], "alice")
check("PvP (mit Muenzamulett) wird gewonnen", view4["outcome"] == "won", view4.get("outcome"))
pvp_earned_with = view4["result"]["reward"]["money_earned"]
check("PvP: Muenzamulett verdoppelt das Preisgeld",
      pvp_earned_with == pvp_earned_plain * 2, (pvp_earned_plain, pvp_earned_with))


print()
if failures:
    print(f"{len(failures)} FAIL")
    sys.exit(1)
print("all ok")
