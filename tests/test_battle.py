"""Tests fuer Kampf-Engine, manuellen Kampf, getragene Items, Heilung und Team.

Aufruf:  python3 tests/test_battle.py
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-battle-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, battle_session, db, encounter_state, healing, held_items, moves, seed, spawn, trainer_battle  # noqa: E402
from app.api import battles as battles_api  # noqa: E402
from app.api import encounters as encounters_api  # noqa: E402
from app.api import team as team_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def expect_http(label: str, status: int, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
        check(label, False, "keine Exception")
    except HTTPException as exc:
        check(label, exc.status_code == status, f"{exc.status_code}: {exc.detail}")


random.seed(int(os.environ.get("TEST_SEED", "1234")))
db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")
IVS = {k: 15 for k in spawn.STAT_KEYS}
NOW = datetime.now().astimezone()


def species(name: str) -> dict:
    return db.query_one("SELECT * FROM species WHERE name = ?", (name,))


PIKA, KARP, CHAR, SQUIRT, BULBA = (
    species("pikachu"), species("magikarp"), species("charmander"),
    species("squirtle"), species("bulbasaur"),
)


def make_owned(user_id: str, sp: dict, level: int, slot: int | None,
               hp: int | None = None, item: str | None = None) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "current_hp, party_slot, caught_at, held_item) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, sp["id"], level, battle.exp_for_level(level, sp["growth_rate"]), 0,
         json.dumps(IVS), "hardy", hp, slot, NOW.isoformat(), item),
    )


def make_encounter(sp: dict, level: int, current_hp: int | None = None) -> int:
    encounter_id = spawn.create_encounter(sp, source_entity="test.trigger")
    db.execute("UPDATE encounter SET level = ?, ivs = ?, current_hp = ? WHERE id = ?",
               (level, json.dumps(IVS), current_hp, encounter_id))
    return encounter_id


def enc(encounter_id: int) -> dict:
    return db.query_one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))


def owned(owned_id: int) -> dict:
    return db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))


def act(battle_id: int, user: dict, **kwargs) -> dict:
    return battles_api.battle_action(battle_id, battles_api.BattleAction(**kwargs), user)


def c(sp: dict, level: int, item: str | None = None, wild: bool = False) -> battle.Combatant:
    return battle.Combatant(species=sp, level=level, ivs=dict(IVS), held_item=item, is_wild=wild)


# --------------------------------------------------------- Shedinja: 1 KP
# Shedinja (Art-ID 292) hat seit seiner Einfuehrung in Gen 3 in jeder
# Generation fest 1 KP, egal welches Level/IV/EV - eine Ausnahme von der
# KP-Formel, siehe battle.stat_hp.
SHEDINJA_FAKE = {**PIKA, "id": 292, "base_hp": 1}
check("stat_hp: Shedinja hat immer 1 KP, egal Level/IV/EV",
      battle.stat_hp(1, 31, 100, 252, species_id=292) == 1
      and battle.stat_hp(1, 0, 1, 0, species_id=292) == 1)
check("stat_hp: ohne Shedinja-ID bleibt es bei der normalen Formel",
      battle.stat_hp(1, 31, 100, 252) != 1)
check("Combatant: Shedinja-KP ignoriert Level/IV trotz Formel",
      c(SHEDINJA_FAKE, 100).max_hp == 1)

# ------------------------------------------------------------------ Attacken
all_moves = moves.all_moves()
check("Attacken-Katalog geladen", len(all_moves) >= 100, f"{len(all_moves)} Attacken")
check(
    "Jede Attacke hat gueltigen Typ/Kategorie/Staerke",
    # power=None ist erlaubt (reine Status-Attacke, siehe STATUS_MOVE_SCORE_POWER) -
    # aber dann muss "status", "stat_changes", "heal", "reset_stats" (Nebel/Haze)
    # ODER "trap" (Fester Blick/Spinnennetz, siehe roaming.py) gesetzt sein,
    # sonst waere sie komplett wirkungslos.
    all(m["type"] in battle.TYPE_CHART
        and m["category"] in ("physical", "special", "status")
        and m["name_de"]
        and (m["power"] > 0 if m["power"] is not None
             else bool(m.get("status")) or bool(m.get("stat_changes")) or bool(m.get("heal"))
             or bool(m.get("reset_stats")) or bool(m.get("trap")))
        for m in all_moves.values()),
)
check(
    "Jeder Typ hat eine Attacke ab Level 1-5",
    all(any(m["type"] == t and m["min_level"] <= 5 for m in all_moves.values())
        for t in battle.TYPE_CHART),
)
check(
    "Jeder Typ hat eine Status-Attacke (power=None) - sonst kommt der "
    "Statuswert-Slot in moveset_for() fuer diesen Typ nie zum Zug",
    all(any(m["type"] == t and m["power"] is None for m in all_moves.values())
        for t in battle.TYPE_CHART),
)

set5 = moves.moveset_for(PIKA, 5)
set50 = moves.moveset_for(PIKA, 50)
check("Moveset hat max. 4 Attacken", 1 <= len(set5) <= 4 and len(set50) == 4, f"{set5} / {set50}")
check("Moveset ist deterministisch", moves.moveset_for(PIKA, 5) == set5)
check("Moveset enthaelt Elektro- und Normal-Attacke",
      any(all_moves[k]["type"] == "electric" for k in set5)
      and any(all_moves[k]["type"] == "normal" for k in set5))
best5 = max(all_moves[k]["power"] for k in set5
            if all_moves[k]["type"] == "electric" and all_moves[k]["power"] is not None)
best50 = max(all_moves[k]["power"] for k in set50
             if all_moves[k]["type"] == "electric" and all_moves[k]["power"] is not None)
check("Hoeheres Level schaltet staerkere Attacken frei", best50 > best5, f"{best5} -> {best50}")
# Movesets come from the real learnset (data/learnsets.json), not from the type.
bulba30 = moves.moveset_for(BULBA, 30)
check("Bisasam Lv. 30 nutzt nur Attacken seiner echten Lernliste bis Lv. 30",
      all((moves.learn_level(BULBA, k) or 99) <= 30 for k in bulba30), f"{bulba30}")
CHARIZARD = {"id": 6, "type1": "fire", "type2": "flying", "base_atk": 84, "base_spa": 109}
dual = moves.moveset_for(CHARIZARD, 50)
check("Zwei Typen -> Attacken beider Typen (Glurak: Feuer + Flug)",
      {"fire", "flying"} <= {all_moves[k]["type"] for k in dual}, f"{dual}")
# Spezial-lastige Art bevorzugt spezielle Attacken.
check("Glurak (SpA > Ang) bekommt spezielle Feuer-Attacke auf Lv. 50",
      all_moves[dual[0]]["category"] == "special", dual[0])

# ---------------------------------------------------- Physisch vs. speziell
brute = dict(PIKA, base_atk=150, base_spa=10)
a, d = c(brute, 30), c(SQUIRT, 60)
phys = battle.expected_damage(a, d, "spark")        # physisch, 65
spec = battle.expected_damage(a, d, "thunder_shock")  # speziell, 40
check("Physisch nutzt Angriff, speziell Spezial-Angriff", phys > spec * 2.5, f"{phys:.1f} vs {spec:.1f}")
check("Wirkungslos macht 0 Schaden",
      battle.calc_damage(c(PIKA, 20), c(dict(KARP, type1="ground"), 20), all_moves["thunderbolt"])[0] == 0)

# ----------------------------------------------------------- Getragene Items
# Die Mega-Steine (siehe app/mega.py) sind gewoehnliche getragene Items und
# stellen den Grossteil des Katalogs - daher die weite Obergrenze.
check("Item-Katalog: 10-100 Items mit Effekt",
      10 <= len(held_items.catalog()) <= 100
      and all(i.get("effect") and i.get("name_de") for i in held_items.catalog().values()))

check("Jeder Typ hat einen Typ-Verstaerker",
      set(battle.TYPE_CHART) <=
      {i["type"] for i in held_items.catalog().values() if i.get("effect") == "type_boost"})

plain = battle.expected_damage(c(CHAR, 20), c(BULBA, 20), "ember")
boosted = battle.expected_damage(c(CHAR, 20, "charcoal"), c(BULBA, 20), "ember")
check("Holzkohle: Feuer x1,2", abs(boosted / plain - 1.2) < 0.01, f"{plain:.1f} -> {boosted:.1f}")
check("Holzkohle wirkt nicht auf Normal-Attacken",
      battle.expected_damage(c(CHAR, 20, "charcoal"), c(BULBA, 20), "scratch")
      == battle.expected_damage(c(CHAR, 20), c(BULBA, 20), "scratch"))

psychic_mon = dict(CHAR, type1="psychic", type2=None)
plain_psy = battle.expected_damage(c(psychic_mon, 20), c(BULBA, 20), "confusion")
boosted_psy = battle.expected_damage(c(psychic_mon, 20, "twisted_spoon"), c(BULBA, 20), "confusion")
check("Loeffelknick: Psycho x1,2", abs(boosted_psy / plain_psy - 1.2) < 0.01,
      f"{plain_psy:.1f} -> {boosted_psy:.1f}")

band = c(PIKA, 20, "choice_band")
check("Wahlband: Angriff x1,5", battle.effective_stat(band, "atk") == band.stat("atk") * 1.5)
b = battle.Battle(wild=c(SQUIRT, 60, wild=True), team=[band])
battle.use_move(b, band, b.wild, band.moves[0])
check("Wahlband sperrt auf die erste Attacke", battle.available_moves(band) == [band.moves[0]])
try:
    battle.run_turn(b, band.moves[1])
    check("Gesperrte Attacke wird abgelehnt", False)
except ValueError:
    check("Gesperrte Attacke wird abgelehnt", True)

orb = c(PIKA, 30, "life_orb")
b = battle.Battle(wild=c(SQUIRT, 60, wild=True), team=[orb])
battle.use_move(b, orb, b.wild, "tackle")
check("Leben-Orb kostet 1/10 KP", orb.current_hp == orb.max_hp - orb.max_hp // 10,
      f"{orb.current_hp}/{orb.max_hp}")

left = c(SQUIRT, 30, "leftovers")
left.current_hp = 10
b = battle.Battle(wild=c(KARP, 5, wild=True), team=[left])
battle.end_of_turn(b)
check("Überreste heilen am Rundenende", left.current_hp == 10 + max(1, int(left.max_hp / 16)))

sash = c(KARP, 5, "focus_sash", wild=True)
b = battle.Battle(wild=sash, team=[c(PIKA, 80)])
battle.use_move(b, b.active_mon, sash, "thunderbolt")
check("Fokusgurt: ueberlebt mit 1 KP und wird verbraucht",
      sash.current_hp == 1 and sash.held_item is None and sash.consumed_item == "focus_sash")

berry = c(SQUIRT, 30, "sitrus_berry", wild=True)
berry.current_hp = berry.max_hp // 2 + 1
b = battle.Battle(wild=berry, team=[c(PIKA, 20)])
random.seed(7)
battle.use_move(b, b.active_mon, berry, "tackle")
check("Tsitrubeere heilt unter halben KP und wird verbraucht",
      berry.held_item is None and any("Sitrus Berry" in line for line in b.log), b.log[-1])

held_items.catalog()["quick_claw"]["chance"] = 1.0
slow = c(KARP, 5, "quick_claw")
b = battle.Battle(wild=c(PIKA, 50, wild=True), team=[slow])
check("Flinkklaue: langsames Pokémon zuerst", battle.player_goes_first(b, "tackle", "tackle"))
held_items.catalog()["quick_claw"]["chance"] = 0.2
check("Prioritaet schlaegt Initiative",
      battle.player_goes_first(battle.Battle(wild=c(PIKA, 50, wild=True), team=[c(KARP, 5)]),
                               "quick_attack", "thunderbolt"))
check("Scope-Linse erhoeht Volltreffer-Stufe",
      battle._crit_stage(c(PIKA, 5, "scope_lens"), all_moves["tackle"]) == 1
      and battle._crit_stage(c(PIKA, 5, "scope_lens"), all_moves["razor_leaf"]) == 2)

bell = c(PIKA, 30, "shell_bell")
bell.current_hp = 5
b = battle.Battle(wild=c(SQUIRT, 60, wild=True), team=[bell])
battle.use_move(b, bell, b.wild, "thunderbolt")
check("Sing heals after hit", bell.current_hp > 5, f"{bell.current_hp}")

# Lucky Egg + EXP distribution via on_battle_won.
egg_id = make_owned("alice", PIKA, 10, None, item="lucky_egg")
plain_id = make_owned("alice", PIKA, 10, None)
team = [battle.combatant_from_owned(owned(egg_id)), battle.combatant_from_owned(owned(plain_id))]
b = battle.Battle(wild=c(KARP, 20, wild=True), team=team, participants={0, 1})
b.wild.current_hp = 0
won = battle.on_battle_won("alice", b)
check("Glücks-Ei: x1,5 EXP", won["exp"][egg_id] == int(won["exp"][plain_id] * 1.5), f"{won['exp']}")
check("on_battle_won liefert reward-Feld", "reward" in won)
db.execute("DELETE FROM owned_pokemon WHERE id IN (?,?)", (egg_id, plain_id))

# Exp. Share: full EXP for a bench member holding it, not just a fifth.
fighter, share_holder, bench = c(PIKA, 10), c(PIKA, 10, item="exp_share"), c(PIKA, 10)
shares = battle.exp_shares([fighter, share_holder, bench], c(KARP, 20, wild=True), {0})
check("Erfahrungsteiler: volle EP ohne Kampfteilnahme",
      shares[1] == shares[0], str(shares))
check("normales Bankmitglied bekommt nur ein Fuenftel",
      shares[2] == shares[0] // 5, str(shares))

shares = battle.exp_shares([fighter, share_holder, bench], c(KARP, 20, wild=True), {0}, share_all=True)
check("share_all (50. Level-100-Erfolg): alle bekommen volle EP",
      shares[0] == shares[1] == shares[2], str(shares))

# Wildes Pokémon levelt durch K.O. -> in diesem Kampf nicht mehr fangbar.
b = battle.Battle(wild=c(KARP, 5, wild=True), team=[c(PIKA, 60)])
battle.wild_gain_exp(b, b.active_mon)
check("Wildes Pokémon levelt durch besiegtes Team-Mitglied",
      b.wild.level > 5 and b.wild.leveled_in_battle, f"Lv. {b.wild.level}")
check("... und ist danach nicht mehr fangbar",
      battle.throw_ball(b.wild, "alice", "master_ball").caught is False)

# ------------------------------------------------------------ Manueller Kampf
db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES ('poke_ball','alice',5)")
db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES ('master_ball','alice',3)")
db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES ('master_ball','bob',3)")
pika_id = make_owned("alice", PIKA, 40, 1)
bob_id = make_owned("bob", CHAR, 20, 1)

e1 = make_encounter(KARP, 5)
view = battles_api.start_battle(e1, alice)
bid = view["id"]
check("Kampfstart beansprucht den Encounter",
      enc(e1)["status"] == "active" and enc(e1)["claimed_by"] == "alice")
check("Kampfansicht enthaelt Attacken", len(view["team"][0]["moves"]) >= 2)
check("Kampfansicht kennt keine Ballsperre mehr", "throw_locked" not in view)
check("Kampf erscheint nicht mehr in der Warteschlange",
      all(e["id"] != e1 for e in encounters_api.list_encounters("waiting", 100, alice)))
check("Erneuter Start setzt den eigenen Kampf fort", battles_api.start_battle(e1, alice)["id"] == bid)
expect_http("Anderer Nutzer kann nicht mitkaempfen", 409, battles_api.start_battle, e1, bob)
expect_http("Anderer Nutzer kann keinen Ball werfen", 409, encounters_api.throw, e1,
            encounters_api.BallThrow(ball="master_ball"), bob)
expect_http("Anderer Nutzer kann ihn nicht fliehen lassen", 409, encounters_api.flee, e1, bob)
db.set_setting("auto_battle_enabled", True)
expect_http("Anderer Nutzer kann keinen Autokampf starten", 409,
            encounters_api.run_auto_battle, e1, bob)
expect_http("Fremden Kampf ansehen verboten", 403, battles_api.get_battle, bid, bob)
expect_http("Fremden Kampf steuern verboten", 403, act, bid, bob, action="run")
expect_http("Karten-Wurf waehrend des eigenen Kampfs abgelehnt", 409, encounters_api.throw, e1,
            encounters_api.BallThrow(ball="poke_ball"), alice)
expect_http("Team-Aenderung waehrend des Kampfs gesperrt", 409, team_api.set_party,
            team_api.PartyOrder(order=[pika_id]), alice)
expect_http("Zweiter Kampf parallel abgelehnt", 409, battles_api.start_battle,
            make_encounter(KARP, 5), alice)

exp_before = owned(pika_id)["exp"]
for _ in range(10):
    view = act(bid, alice, action="move", move=view["team"][0]["moves"][0]["key"])
    if view["over"]:
        break
check("Kampf gewonnen", view["outcome"] == "won", view["outcome"] or "-")
check("Sieg: fuer Alice erledigt, fuer Bob weiter in der Warteschlange",
      encounter_state.progress(e1, "alice")["outcome"] == "defeated"
      and enc(e1)["status"] == "waiting" and not encounter_state.is_done(e1, "bob"))
check("Erledigter Encounter verschwindet aus Alices Warteschlange",
      all(e["id"] != e1 for e in encounters_api.list_encounters("waiting", 100, alice))
      and any(e["id"] == e1 for e in encounters_api.list_encounters("waiting", 100, bob)))
expect_http("Erledigter Encounter nimmt keinen Ball mehr", 409, encounters_api.throw, e1,
            encounters_api.BallThrow(ball="master_ball"), alice)
check("Sieg: EXP gutgeschrieben", owned(pika_id)["exp"] > exp_before)
check("Sieg: Ergebnis mit reward-Feld", "reward" in (view["result"] or {}))
check("Kampf-Log laeuft mit", len(view["log"]) > 3 and len(view["new_log"]) > 0)
expect_http("Beendeter Kampf nimmt keine Aktionen mehr", 409, act, bid, alice, action="run")
check("battle_log-Eintrag geschrieben",
      db.query_one("SELECT outcome FROM battle_log WHERE encounter_id = ?", (e1,))["outcome"] == "won")

# Flucht: Encounter kehrt mit KP zurueck in die Warteschlange.
db.execute("UPDATE owned_pokemon SET current_hp = NULL WHERE id = ?", (pika_id,))
# Level 30: a higher-level Squirtle knows Wave Crash/Hydro Pump (its real
# learnset), which can KO the Lv. 40 Pikachu in turn 1 - the flee scenario
# needs the battle to still be running after the first exchange.
e2 = make_encounter(SQUIRT, 30)
view = battles_api.start_battle(e2, alice)
view = act(view["id"], alice, action="move", move="quick_attack"
           if "quick_attack" in [m["key"] for m in view["team"][0]["moves"]]
           else view["team"][0]["moves"][-1]["key"])
if not view["over"]:
    hp_in_battle = view["wild"]["current_hp"]
    view = act(view["id"], alice, action="run")
    check("Flucht beendet den Kampf", view["outcome"] == "ran")
    check("Flucht: Encounter wieder 'waiting' und frei",
          enc(e2)["status"] == "waiting" and enc(e2)["claimed_by"] is None)
    check("Flucht: KP des Wilden bleiben im eigenen Fortschritt erhalten",
          encounter_state.progress(e2, "alice")["current_hp"] == hp_in_battle,
          f"{encounter_state.progress(e2, 'alice')['current_hp']}")
    check("Flucht: fuer andere startet der Encounter wieder voll",
          encounters_api._decorate(enc(e2), "bob")["current_hp"]
          == encounters_api._decorate(enc(e2), "bob")["max_hp"])
else:
    check("Flucht-Szenario (Kampf endete vorzeitig)", False, view["outcome"])

# Niederlage: das aktive Pokémon fault -> Kampf endet sofort, kein
# Pflichtwechsel wie in den Hauptspielen (obwohl weak2 noch kampffaehig
# waere) - das Wilde flieht statt im Kampf zu bleiben.
db.execute("UPDATE owned_pokemon SET party_slot = NULL WHERE user_id = 'alice'")
weak1 = make_owned("alice", BULBA, 5, 1)
weak2 = make_owned("alice", CHAR, 5, 2)
e3 = make_encounter(KARP, 100)
view = battles_api.start_battle(e3, alice)
bid3 = view["id"]
for _ in range(20):
    view = act(bid3, alice, action="move", move=view["team"][0]["moves"][0]["key"])
    if view["over"]:
        break
check("Niederlage sofort, kein Pflichtwechsel auf das zweite Team-Mitglied",
      view["outcome"] == "lost" and not view["needs_switch"] and view["active"] == weak1,
      str(view["outcome"]))
expect_http("Beendeter Kampf nimmt keine Aktionen mehr", 409, act, bid3, alice,
            action="switch", target=weak2)
check("Das Wilde flieht: fuer Alice ist der Encounter erledigt, nicht mehr in ihrer Warteschlange",
      encounter_state.progress(e3, "alice")["outcome"] == "fled"
      and all(e["id"] != e3 for e in encounters_api.list_encounters("waiting", 100, alice)))
check("Fuer Bob steht der geteilte Encounter weiterhin an",
      any(e["id"] == e3 for e in encounters_api.list_encounters("waiting", 100, bob)))

# Fang im Kampf: ein Ball wird auch nach bereits bekaempften Zuegen angenommen.
db.execute("UPDATE owned_pokemon SET party_slot = NULL WHERE user_id = 'alice'")
db.execute("UPDATE owned_pokemon SET party_slot = 1, current_hp = NULL WHERE id = ?", (pika_id,))
e3b = make_encounter(KARP, 30)
view = battles_api.start_battle(e3b, alice)
bid3b = view["id"]
# Quick Attack statt der staerksten Attacke (Donnerblitz) - sonst besiegt der
# Levelvorsprung das Wilde schon mit dem ersten Zug.
view = act(bid3b, alice, action="move", move="quick_attack")
check("Ein Angriff zaehlt als 'bekaempft'", view["fought"] is True and not view["over"])
dex_before = db.query_one(
    "SELECT caught_count FROM dex_entry WHERE species_id = ? AND user_id = 'alice'", (KARP["id"],)
)["caught_count"]
view = act(bid3b, alice, action="ball", ball="master_ball")
check("Ball im Kampf faengt", view["outcome"] == "caught" and view["result"]["owned_id"])
caught_row = owned(view["result"]["owned_id"])
check("Fang: gleicher Pfad wie /throw (Encounter 'caught', Pokémon angelegt)",
      encounter_state.progress(e3b, "alice")["outcome"] == "caught"
      and caught_row["caught_from"] == e3b
      and caught_row["user_id"] == "alice")
check("Fang: freier Team-Platz wird belegt", caught_row["party_slot"] == 2,
      f"Platz {caught_row['party_slot']}")
check("Fang: Pokédex nachgezogen", db.query_one(
    "SELECT caught_count FROM dex_entry WHERE species_id = ? AND user_id = 'alice'", (KARP["id"],)
)["caught_count"] == dex_before + 1)
check("Fang: Ball verbraucht", db.query_one(
    "SELECT quantity FROM inventory WHERE item_key='master_ball' AND user_id='alice'")["quantity"] == 2)

# Niederlage: einziges Team-Mitglied besiegt -> Wildes flieht ebenso.
db.execute("UPDATE owned_pokemon SET party_slot = NULL WHERE user_id = 'alice'")
db.execute("UPDATE owned_pokemon SET party_slot = 1 WHERE id = ?", (weak1,))
db.execute("UPDATE owned_pokemon SET current_hp = NULL WHERE id = ?", (weak1,))
e4 = make_encounter(CHAR, 100)
view = battles_api.start_battle(e4, alice)
for _ in range(20):
    view = act(view["id"], alice, action="move", move=view["team"][0]["moves"][0]["key"])
    if view["over"]:
        break
check("Niederlage erkannt", view["outcome"] == "lost", view["outcome"] or "-")
check("Niederlage: das Wilde flieht, Encounter fuer Alice erledigt (nicht wiederholbar)",
      encounter_state.progress(e4, "alice")["outcome"] == "fled"
      and encounter_state.progress(e4, "alice")["current_hp"] is not None)
check("Niederlage: Team-KP gespeichert", owned(weak1)["current_hp"] == 0)
e4b = make_encounter(KARP, 5)
expect_http("Kampfunfaehiges Team kann nicht kaempfen", 409, battles_api.start_battle, e4b, alice)

# ---------------------------------------------------------------- Heilung
check("Team heilen", team_api.heal(alice)["healed"] >= 1 and owned(weak1)["current_hp"] > 0)
expect_http("Heilen hat Abklingzeit", 409, team_api.heal, alice)
# Die Abklingzeit haengt am Nutzer (app_user.last_team_heal_at), nicht am
# Haushalt - Alice darf Bob also nicht blockieren.
check("Abklingzeit ist pro Nutzer: Bob kann trotzdem heilen",
      healing.heal_status("bob")["available"] and team_api.heal(bob)["healed"] >= 0)
check("Abklingzeit ist pro Nutzer: Alice bleibt gesperrt",
      not healing.heal_status("alice")["available"])
expect_http("Bob hat nach dem Heilen seine eigene Abklingzeit", 409, team_api.heal, bob)

db.set_setting("heal_regen_percent_per_hour", 20)
row = owned(weak1)
max_hp = healing.max_hp_of(row)
db.execute("UPDATE owned_pokemon SET current_hp = 0, hp_updated_at = ? WHERE id = ?",
           ((NOW - timedelta(hours=1)).isoformat(), weak1))
healing.apply_regen("alice")
check("Regeneration: ~20 % nach 1 h", owned(weak1)["current_hp"] == int(max_hp * 0.2),
      f"{owned(weak1)['current_hp']}/{max_hp}")
db.execute("UPDATE owned_pokemon SET current_hp = 0, hp_updated_at = ? WHERE id = ?",
           ((NOW - timedelta(hours=6)).isoformat(), weak1))
healing.apply_regen("alice")
check("Regeneration: voll nach 5+ h", owned(weak1)["current_hp"] == max_hp)

# ------------------------------------------------------------ Kampf-Timeout
e5 = make_encounter(KARP, 3)
view = battles_api.start_battle(e5, alice)
db.execute("UPDATE battle_session SET updated_at = ? WHERE id = ?",
           ((NOW - timedelta(hours=2)).isoformat(), view["id"]))
check("Verwaister Kampf wird freigegeben", battle_session.release_stale_battles() == 1
      and enc(e5)["status"] == "waiting")
check("... und als 'abandoned' markiert", db.query_one(
    "SELECT status FROM battle_session WHERE id = ?", (view["id"],))["status"] == "abandoned")

# ----------------------------------------------------------- Karten-Wurf
e6 = make_encounter(KARP, 3, current_hp=5)
orig_throw = battle.throw_ball
battle.throw_ball = lambda *a, **k: battle.CatchResult(False, False, 1, 0.1)
res = encounters_api.throw(e6, encounters_api.BallThrow(ball="poke_ball"), alice)
battle.throw_ball = orig_throw
check("Fehlwurf ausserhalb des Kampfs: Encounter bleibt in der Warteschlange",
      res["caught"] is False and enc(e6)["status"] == "waiting")
res = encounters_api.throw(e6, encounters_api.BallThrow(ball="master_ball"), alice)
check("Karten-Wurf nutzt finalize_catch", res["caught"] and res["owned_id"]
      and "reward" in res and encounter_state.progress(e6, "alice")["outcome"] == "caught")

# ------------------------------------------------------------- Autokampf
db.execute("UPDATE owned_pokemon SET party_slot = NULL WHERE user_id = 'alice'")
db.execute("UPDATE owned_pokemon SET party_slot = 1, current_hp = NULL WHERE id = ?", (pika_id,))
e7 = make_encounter(KARP, 5)
auto = encounters_api.run_auto_battle(e7, alice)
check("Autokampf: gleiche Antwortform", {"outcome", "log", "wild_hp"} <= set(auto), auto["outcome"])
check("Autokampf gewinnt mit starkem Team",
      auto["outcome"] == "won" and encounter_state.progress(e7, "alice")["outcome"] == "defeated")
check("Autokampf nutzt echte Attacken",
      any("uses" in line for line in auto["log"]), auto["log"][1] if len(auto["log"]) > 1 else "")

db.execute("UPDATE owned_pokemon SET party_slot = NULL WHERE user_id = 'alice'")
db.execute("UPDATE owned_pokemon SET party_slot = 1, current_hp = NULL WHERE id = ?", (weak1,))
e8 = make_encounter(CHAR, 100)
auto = encounters_api.run_auto_battle(e8, alice)
check("Verlorener Autokampf: Encounter zurueck in die Warteschlange",
      auto["outcome"] == "lost" and enc(e8)["status"] == "waiting", auto["outcome"])

# ----------------------------------------------------------------- Team
db.execute("UPDATE owned_pokemon SET party_slot = NULL WHERE user_id = 'alice'")
team = team_api.set_party(team_api.PartyOrder(order=[weak2, pika_id, weak1]), alice)
check("Team setzen/sortieren", [p["id"] for p in team["party"]] == [weak2, pika_id, weak1])
check("Team-Ansicht: Moves, IVs, EXP", all(p["moves"] and p["ivs"] and p["exp_next_level"]
                                           for p in team["party"]))
team = team_api.set_party(team_api.PartyOrder(order=[pika_id]), alice)
check("Aus dem Team entfernen -> Box", weak2 in [p["id"] for p in team["box"]])
expect_http("Fremde Pokémon ins Team verboten", 403, team_api.set_party,
            team_api.PartyOrder(order=[pika_id]), bob)
expect_http("Mehr als 6 im Team verboten", 400, team_api.set_party,
            team_api.PartyOrder(order=list(range(1, 8))), alice)
expect_http("Leeres Team verboten", 400, team_api.set_party, team_api.PartyOrder(order=[]), alice)

db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES ('leftovers','alice',1)")
db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES ('charcoal','alice',1)")


def qty(key: str, user: str = "alice") -> int:
    row = db.query_one("SELECT quantity FROM inventory WHERE item_key = ? AND user_id = ?",
                       (key, user))
    return row["quantity"] if row else 0


res = team_api.set_held_item(pika_id, team_api.HeldItemChange(item="leftovers"), alice)
check("Item anlegen: vom Inventar aufs Pokémon",
      res["held_item"] == "leftovers" and qty("leftovers") == 0)
res = team_api.set_held_item(pika_id, team_api.HeldItemChange(item="charcoal"), alice)
check("Item tauschen: altes Item zurueck ins Inventar",
      res["held_item"] == "charcoal" and qty("leftovers") == 1 and qty("charcoal") == 0)
res = team_api.set_held_item(pika_id, team_api.HeldItemChange(item=None), alice)
check("Item ablegen", res["held_item"] is None and qty("charcoal") == 1)
expect_http("Item ohne Inventar abgelehnt", 409, team_api.set_held_item, pika_id,
            team_api.HeldItemChange(item="life_orb"), alice)
expect_http("Unbekanntes Item abgelehnt", 400, team_api.set_held_item, pika_id,
            team_api.HeldItemChange(item="rare_candy"), alice)
expect_http("Fremdes Pokémon ausruesten verboten", 403, team_api.set_held_item, pika_id,
            team_api.HeldItemChange(item=None), bob)

# ------------------------------------------------------------- Migration
db.execute("UPDATE encounter SET status = 'active', claimed_by = 'bob' WHERE id = ?", (e8,))
db.init_db()
db.init_db()
cols = db._table_columns(db.connect(), "owned_pokemon")
check("Migration ist idempotent (held_item, hp_updated_at)", {"held_item", "hp_updated_at"} <= cols)
check("Verwaiste 'active'-Encounter werden freigegeben", enc(e8)["status"] == "waiting")
check("Neue Settings vorhanden", db.get_setting("heal_team_cooldown_minutes") == 60
      and db.get_setting("battle_timeout_minutes") == 30)

# --------------------------------------------------------------- Status
# Synthetische Arten fuer Typen, die im Fallback-Pokédex fehlen (Boden,
# Stahl, Eis) - direkt in die DB eingefuegt, nicht ueber PokeAPI/Fallback.
def make_species(species_id: int, name: str, type1: str, type2: str | None = None) -> dict:
    db.execute(
        """
        INSERT INTO species (id, name, name_de, type1, type2, generation, catch_rate,
            base_exp, growth_rate, base_hp, base_atk, base_def, base_spa, base_spd, base_spe)
        VALUES (?,?,?,?,?,1,45,64,'medium_fast',100,80,80,80,80,80)
        ON CONFLICT(id) DO NOTHING
        """,
        (species_id, name, name, type1, type2),
    )
    return db.query_one("SELECT * FROM species WHERE id = ?", (species_id,))


def mon(sp: dict, level: int = 30) -> battle.Combatant:
    return battle.Combatant(species=sp, level=level, ivs=dict(IVS))


ground_sp = make_species(90001, "test-ground", "ground")
steel_sp = make_species(90002, "test-steel", "steel")
ice_sp = make_species(90003, "test-ice", "ice")
electric_sp = make_species(90004, "test-electric", "electric")
fire_sp = make_species(90005, "test-fire", "fire")
poison_sp = make_species(90006, "test-poison", "poison")
normal_sp = make_species(90007, "test-normal", "normal")
psychic_sp = make_species(90008, "test-psychic", "psychic")
water_sp = make_species(90009, "test-water", "water")
ghost_sp = make_species(90010, "test-ghost", "ghost")
rock_sp = make_species(90011, "test-rock", "rock")
bug_sp = make_species(90012, "test-bug", "bug")


def with_random(**patched):
    """Ersetzt random.<name> zeitweise (z.B. random=lambda: 0.0 fuer 'trifft
    immer'/'Status-Chance greift immer'). Gibt eine Restore-Funktion zurueck."""
    saved = {name: getattr(random, name) for name in patched}
    for name, fn in patched.items():
        setattr(random, name, fn)

    def restore() -> None:
        for name, fn in saved.items():
            setattr(random, name, fn)

    return restore


# --- Typ-Immunitaet blockiert reine Status-Attacken (0x in TYPE_CHART)
thunder_wave = moves.get("thunder_wave")
gnd = mon(ground_sp)
restore = with_random(random=lambda: 0.0)
try:
    b = battle.Battle(wild=mon(electric_sp), team=[gnd])
    battle.use_move(b, b.wild, gnd, "thunder_wave")
    check("Donnerwelle wirkungslos gegen Boden (0x)", gnd.status is None)
finally:
    restore()

# --- Angeborene Status-Immunitaet (unabhaengig vom Typ der Attacke)
restore = with_random(random=lambda: 0.0)
try:
    elec = mon(electric_sp)
    b = battle.Battle(wild=mon(normal_sp), team=[elec])
    battle.use_move(b, b.wild, elec, "body_slam")  # normal_sp Paralyse-Chance 100% erzwungen
    check("Elektro-Typ kann nicht paralysiert werden", elec.status is None)

    fir = mon(fire_sp)
    b2 = battle.Battle(wild=mon(fire_sp), team=[fir])
    ok = battle.inflict_status(b2, fir, "burn")
    check("Feuer-Typ kann nicht verbrannt werden", not ok and fir.status is None)

    poi = mon(poison_sp)
    b3 = battle.Battle(wild=mon(poison_sp), team=[poi])
    ok = battle.inflict_status(b3, poi, "poison")
    check("Gift-Typ kann nicht vergiftet werden", not ok and poi.status is None)

    ste = mon(steel_sp)
    b4 = battle.Battle(wild=mon(steel_sp), team=[ste])
    ok = battle.inflict_status(b4, ste, "toxic")
    check("Stahl-Typ kann nicht (schwer) vergiftet werden", not ok and ste.status is None)

    ice_mon = mon(ice_sp)
    b5 = battle.Battle(wild=mon(ice_sp), team=[ice_mon])
    ok = battle.inflict_status(b5, ice_mon, "freeze")
    check("Eis-Typ kann nicht eingefroren werden", not ok and ice_mon.status is None)
finally:
    restore()

# --- Nur ein Status gleichzeitig
normal_mon = mon(normal_sp)
b = battle.Battle(wild=mon(fire_sp), team=[normal_mon])
assert battle.inflict_status(b, normal_mon, "paralysis")
check("Zweiter Status schlaegt fehl, solange der erste aktiv ist",
      not battle.inflict_status(b, normal_mon, "burn") and normal_mon.status == "paralysis")

# --- Vergiftung: 1/8 der maximalen KP pro Runde
target = mon(normal_sp)
target.status = "poison"
before = target.current_hp
b = battle.Battle(wild=mon(fire_sp), team=[target])
battle._tick_status(b, target)
check("Vergiftung: 1/8 Schaden pro Runde",
      before - target.current_hp == max(1, target.max_hp // 8))

# --- Kanenvergiftung (toxic): steigender Schaden 1/16, 2/16, 3/16, ...
target = mon(normal_sp)
target.status = "toxic"
b = battle.Battle(wild=mon(fire_sp), team=[target])
hp_before = target.current_hp
battle._tick_status(b, target)
tick1 = hp_before - target.current_hp
hp_before = target.current_hp
battle._tick_status(b, target)
tick2 = hp_before - target.current_hp
check("Kettenvergiftung: zweiter Tick schadet mehr als der erste", tick2 > tick1)
check("Kettenvergiftung: erster Tick = 1/16", tick1 == max(1, target.max_hp // 16))
check("Kettenvergiftung: zweiter Tick = 2/16", tick2 == max(1, target.max_hp * 2 // 16))

# --- Verbrennung: 1/16 Schaden pro Runde UND halbiert Physisch-Angriff
target = mon(normal_sp)
target.status = "burn"
before = target.current_hp
b = battle.Battle(wild=mon(fire_sp), team=[target])
battle._tick_status(b, target)
check("Verbrennung: 1/16 Schaden pro Runde",
      before - target.current_hp == max(1, target.max_hp // 16))
healthy = mon(normal_sp)
burned = mon(normal_sp)
burned.status = "burn"
check("Verbrennung halbiert den effektiven Angriffswert",
      battle.effective_stat(burned, "atk") == battle.effective_stat(healthy, "atk") / 2)

# --- Paralyse: Tempo halbiert, ~25% Chance auf volle Blockade
paralyzed = mon(normal_sp)
paralyzed.status = "paralysis"
healthy = mon(normal_sp)
check("Paralyse halbiert das effektive Tempo",
      battle.effective_stat(paralyzed, "spe") == battle.effective_stat(healthy, "spe") / 2)
restore = with_random(random=lambda: 0.0)  # < 0.25 -> blockiert
try:
    b = battle.Battle(wild=mon(fire_sp), team=[paralyzed])
    check("Paralyse blockiert bei niedrigem Wurf", battle._status_prevents_action(b, paralyzed))
finally:
    restore()
restore = with_random(random=lambda: 0.99)  # >= 0.25 -> handelt normal
try:
    b = battle.Battle(wild=mon(fire_sp), team=[paralyzed])
    check("Paralyse blockiert nicht bei hohem Wurf", not battle._status_prevents_action(b, paralyzed))
finally:
    restore()

# --- Schlaf: blockiert fuer status_counter Runden, wacht danach auf
sleeper = mon(normal_sp)
sleeper.status, sleeper.status_counter = "sleep", 2
b = battle.Battle(wild=mon(fire_sp), team=[sleeper])
check("Schlaf blockiert (Runde 1)", battle._status_prevents_action(b, sleeper) and sleeper.status_counter == 1)
check("Schlaf blockiert (Runde 2)", battle._status_prevents_action(b, sleeper) and sleeper.status_counter == 0)
check("Schlaf: aufgewacht (Runde 3)", not battle._status_prevents_action(b, sleeper) and sleeper.status is None)

# --- Frost: blockiert, taut mit Chance auf, oder sofort durch Feuer-Attacke
frozen = mon(normal_sp)
frozen.status = "freeze"
restore = with_random(random=lambda: 0.99)  # >= 0.2 -> bleibt eingefroren
try:
    b = battle.Battle(wild=mon(fire_sp), team=[frozen])
    check("Frost blockiert, wenn Auftau-Wurf hoch ist", battle._status_prevents_action(b, frozen))
finally:
    restore()
restore = with_random(random=lambda: 0.0)  # < 0.2 -> taut auf
try:
    b = battle.Battle(wild=mon(fire_sp), team=[frozen])
    check("Frost: taut bei niedrigem Wurf auf",
          not battle._status_prevents_action(b, frozen) and frozen.status is None)
finally:
    restore()

frozen2 = mon(normal_sp)
frozen2.status = "freeze"
restore = with_random(random=lambda: 0.0)
try:
    attacker = mon(fire_sp)
    b = battle.Battle(wild=attacker, team=[frozen2])
    # fire_fang hat keinen Sekundaereffekt - isoliert das Auftauen sauber von
    # "koennte danach direkt wieder verbrennen".
    check("fire_fang hat keinen Status-Sekundaereffekt (isoliert den Test)",
          moves.get("fire_fang").get("status") is None)
    battle.use_move(b, attacker, frozen2, "fire_fang")
    check("Feuer-Attacke taut ein eingefrorenes Ziel sofort auf", frozen2.status is None)
finally:
    restore()

# --- Sekundaerer Status auf Schadens-Attacken (Chance erzwungen via random=0.0)
restore = with_random(random=lambda: 0.0)
try:
    attacker = mon(fire_sp)
    target = mon(normal_sp)
    b = battle.Battle(wild=attacker, team=[target])
    battle.use_move(b, attacker, target, "ember")
    check("Ember kann verbrennen (Sekundaereffekt)", target.status == "burn")
finally:
    restore()

# --- Toxin/Donnerwelle/Schlafpuder/Irrlicht sind jetzt echte Status-Attacken
for key, expect_status in [
    ("toxic", "toxic"), ("thunder_wave", "paralysis"),
    ("sleep_powder", "sleep"), ("will_o_wisp", "burn"),
]:
    check(f"{key}: power=None, status={expect_status}",
          moves.get(key)["power"] is None and moves.get(key)["status"] == expect_status)

# --- Movesets: hoechstens EINE Status-Attacke, Schaden nicht verdraengt
poison_species = species("bulbasaur")  # grass/poison, Level hoch genug fuer toxic (20)
ms = moves.moveset_for(poison_species, level=30)
status_in_moveset = [k for k in ms if moves.get(k)["power"] is None]
check("Moveset: hoechstens eine Status-Attacke", len(status_in_moveset) <= 1)
check("Moveset: mindestens eine Schadens-Attacke bleibt uebrig",
      any(moves.get(k)["power"] is not None for k in ms))

# --- Persistenz: Status ueberlebt Serialisierung/Deserialisierung im
# manuellen Kampf (battle_session state-Blob, siehe _to_state/_load)
carol = db.upsert_user("carol_status", "Carol")
db.execute(
    "INSERT INTO inventory (item_key, user_id, quantity) VALUES ('poke_ball', ?, 5)",
    (carol["id"],),
)
make_owned(carol["id"], CHAR, level=20, slot=1)  # Feuer: darf paralysiert werden
e_status = make_encounter(PIKA, level=15)  # Elektro: darf vergiftet werden

session = battles_api.start_battle(e_status, carol)
sid = session["id"]
session_row = db.query_one("SELECT * FROM battle_session WHERE id = ?", (sid,))
b, state, encounter = battle_session._load(session_row)
check("Status-Test-Vorbedingung: Team-Pokémon startet ohne Status", b.active_mon.status is None)
assert battle.inflict_status(b, b.active_mon, "paralysis")
assert battle.inflict_status(b, b.wild, "toxic")
b.wild.status_counter = 3
battle_session._save(session_row, b, state, "ongoing")

session_row2 = db.query_one("SELECT * FROM battle_session WHERE id = ?", (sid,))
b2, _, _ = battle_session._load(session_row2)
check("Team-Status uebersteht Speichern/Laden (_save/_load)", b2.active_mon.status == "paralysis")
check("Wild-Status inkl. Zaehler uebersteht Speichern/Laden",
      b2.wild.status == "toxic" and b2.wild.status_counter == 3)
view = battles_api.get_battle(sid, carol)
check("Status erscheint in der Frontend-Ansicht (_describe)",
      view["wild"]["status"] == "toxic"
      and next(m["status"] for m in view["team"] if m["active"]) == "paralysis")

# ------------------------------------------------------ Statuswert-Aenderungen
check("Stufen-Multiplikator: 0 ist neutral", battle.stat_stage_multiplier(0) == 1.0)
check("Stufen-Multiplikator: +2 = x2", battle.stat_stage_multiplier(2) == 2.0)
check("Stufen-Multiplikator: +6 = x4", battle.stat_stage_multiplier(6) == 4.0)
check("Stufen-Multiplikator: -2 = x0,5", battle.stat_stage_multiplier(-2) == 0.5)
check("Stufen-Multiplikator: -6 = x0,25", battle.stat_stage_multiplier(-6) == 0.25)

# --- Selbstbuff (Schwerttanz: +2 Angriff auf sich selbst)
swords_user = mon(normal_sp)
b = battle.Battle(wild=mon(fire_sp), team=[swords_user])
battle.use_move(b, swords_user, b.wild, "swords_dance")
check("Schwerttanz erhoeht den eigenen Angriff um 2 Stufen", swords_user.stat_stages["atk"] == 2)
check("Schwerttanz veraendert den Gegner nicht", b.wild.stat_stages["atk"] == 0)
check("Effektiver Angriff verdoppelt sich bei +2",
      battle.effective_stat(swords_user, "atk") == swords_user.stat("atk") * 2.0)

# --- Debuff auf den Gegner (Knurren: -1 Angriff beim Ziel)
attacker = mon(normal_sp)
target = mon(normal_sp)
b = battle.Battle(wild=attacker, team=[target])
battle.use_move(b, attacker, target, "growl")
check("Knurren senkt den Angriff des Ziels um 1 Stufe", target.stat_stages["atk"] == -1)
check("Knurren veraendert den Anwender nicht", attacker.stat_stages["atk"] == 0)

# --- Sp.Angriff-Debuff auf den Gegner ueber eine reine Status-Attacke (Vertrauen)
confide_attacker = mon(normal_sp)
confide_target = mon(normal_sp)
b = battle.Battle(wild=confide_attacker, team=[confide_target])
battle.use_move(b, confide_attacker, confide_target, "confide")
check("Vertrauen senkt den Sp.Angriff des Ziels um 1 Stufe", confide_target.stat_stages["spa"] == -1)

# --- Sp.Verteidigung-Debuff auf den Gegner ueber eine reine Status-Attacke (Metallsound)
restore = with_random(random=lambda: 0.0)
try:
    metal_attacker = mon(normal_sp)
    metal_target = mon(normal_sp)
    b = battle.Battle(wild=metal_attacker, team=[metal_target])
    battle.use_move(b, metal_attacker, metal_target, "metal_sound")
    check("Metallsound senkt die Sp.Verteidigung des Ziels um 2 Stufen",
          metal_target.stat_stages["spd"] == -2)
finally:
    restore()

# --- Sekundaerer Sp.Angriff-Debuff auf einer Schadens-Attacke (Standpauke, 100% Chance)
restore = with_random(random=lambda: 0.0)
try:
    snarl_attacker = mon(normal_sp)
    snarl_target = mon(normal_sp)
    b = battle.Battle(wild=snarl_attacker, team=[snarl_target])
    battle.use_move(b, snarl_attacker, snarl_target, "snarl")
    check("Standpauke senkt bei Treffer garantiert den Sp.Angriff des Ziels",
          snarl_target.stat_stages["spa"] == -1)
finally:
    restore()

# --- Mehrere Statuswerte auf einmal (Stahltanz-artig: Ruhe Bewahren)
calm = mon(psychic_sp)
b = battle.Battle(wild=mon(fire_sp), team=[calm])
battle.use_move(b, calm, b.wild, "calm_mind")
check("Ruhe Bewahren erhoeht Sp.Angriff UND Sp.Verteidigung um je 1",
      calm.stat_stages["spa"] == 1 and calm.stat_stages["spd"] == 1)

# --- Stufen sind bei +6/-6 gedeckelt
capped = mon(normal_sp)
capped.stat_stages["atk"] = 6
b = battle.Battle(wild=mon(fire_sp), team=[capped])
ok = battle.apply_stat_change(b, capped, "atk", 2)
check("Bereits bei +6: weitere Erhoehung schlaegt fehl", not ok and capped.stat_stages["atk"] == 6)
check("Log meldet 'kann nicht weiter steigen'",
      "higher" in b.log[-1] or "steigen" in b.log[-1])

# --- Typ-Immunitaet blockiert Statuswert-Attacken gegen ein Ziel (Donnerwelle-artig)
restore = with_random(random=lambda: 0.0)
try:
    ground_target = mon(ground_sp)
    b = battle.Battle(wild=mon(normal_sp), team=[ground_target])
    battle.use_move(b, b.wild, ground_target, "growl")  # normal-Attacke, KEIN 0x - Kontrolle
    check("Kontrolle: Knurren wirkt normal gegen Boden", ground_target.stat_stages["atk"] == -1)
finally:
    restore()

# --- Sekundaerer Statuswert-Effekt auf einer Schadens-Attacke (Chance erzwungen)
restore = with_random(random=lambda: 0.0)
try:
    steel_attacker = mon(steel_sp)
    steel_target = mon(normal_sp)
    b = battle.Battle(wild=steel_attacker, team=[steel_target])
    battle.use_move(b, steel_attacker, steel_target, "iron_tail")
    check("Eisenschweif senkt bei Treffer die Verteidigung des Ziels (Sekundaereffekt)",
          steel_target.stat_stages["def"] == -1)
    check("Niedrigere effektive Verteidigung erhoeht den erwarteten Folgeschaden",
          battle.effective_stat(steel_target, "def") < steel_target.stat("def"))
finally:
    restore()

# --- Sekundaereffekt bleibt aus, wenn die Chance nicht trifft (Treffer aber
# kein Statuswert-Effekt: 0.5 schlaegt die 10%-Chance von metal_claw fehl,
# trifft aber die 95%-Genauigkeit)
restore = with_random(random=lambda: 0.5)
try:
    steel_attacker = mon(steel_sp)
    steel_target = mon(normal_sp)
    b = battle.Battle(wild=steel_attacker, team=[steel_target])
    battle.use_move(b, steel_attacker, steel_target, "metal_claw")
    check("Treffer kam an (Kontrolle)", steel_target.current_hp < steel_target.max_hp)
    check("Kein Sekundaereffekt ohne die noetige Chance", steel_attacker.stat_stages["atk"] == 0)
finally:
    restore()

# --- Statuswert-Stufen werden beim Wechsel zurueckgesetzt
switcher = mon(normal_sp)
bench_mon = mon(fire_sp)
b = battle.Battle(wild=mon(water_sp), team=[switcher, bench_mon])
battle.use_move(b, switcher, b.wild, "swords_dance")
check("Vorbedingung: Anwender hat +2 Angriff", switcher.stat_stages["atk"] == 2)
battle.switch_to(b, 1)
check("Wechsel setzt die Statuswert-Stufen des Verlassenden zurueck", switcher.stat_stages["atk"] == 0)

# --- KI beruecksichtigt Statuswert-Attacken (0 Nutzwert, wenn wirkungslos/am Limit)
# growl SENKT den Angriff des Ziels - am unteren Limit (-6) bringt es nichts mehr.
maxed = mon(normal_sp)
maxed.stat_stages["atk"] = battle.STAT_STAGE_MIN
check("Erwarteter Nutzwert ist 0, wenn die Stufe schon am Limit ist",
      battle.expected_damage(mon(normal_sp), maxed, "growl") == 0.0)
fresh_target = mon(normal_sp)
check("Erwarteter Nutzwert > 0, wenn die Attacke noch etwas bewirken kann",
      battle.expected_damage(mon(normal_sp), fresh_target, "growl") > 0.0)

# --- Regression: KI stapelt Statuswert-Attacken nicht die ganze Kampfdauer,
# sondern wertet sie ab, je naeher die Stufe an ihr Limit kommt - sonst haelt
# die KI eine ganze Runde lang nur Statuswerte, statt anzugreifen.
fresh_dance_user = mon(normal_sp)
first_use_value = battle.expected_damage(fresh_dance_user, mon(normal_sp), "swords_dance")
boosted_once = mon(normal_sp)
boosted_once.stat_stages["atk"] = 2
second_use_value = battle.expected_damage(boosted_once, mon(normal_sp), "swords_dance")
boosted_twice = mon(normal_sp)
boosted_twice.stat_stages["atk"] = 4
third_use_value = battle.expected_damage(boosted_twice, mon(normal_sp), "swords_dance")
check("Nutzwert von Schwerttanz sinkt mit jeder bereits erreichten Stufe",
      first_use_value > second_use_value > third_use_value > 0.0)
maxed_dancer = mon(normal_sp)
maxed_dancer.stat_stages["atk"] = battle.STAT_STAGE_MAX
check("Nutzwert ist 0, sobald die Stufe das Limit erreicht hat",
      battle.expected_damage(maxed_dancer, mon(normal_sp), "swords_dance") == 0.0)

# --- Persistenz: Statuswert-Stufen ueberstehen Speichern/Laden (Arena-/PvP-Sitzungszustand)
stage_mon = battle.Combatant(species=CHAR, level=20, ivs=dict(IVS))
stage_mon.stat_stages["atk"] = 2
stage_mon.stat_stages["spe"] = -1
state_blob = trainer_battle.mon_state(stage_mon)
check("mon_state schreibt nur von 0 abweichende Stufen",
      state_blob["stat_stages"] == {"atk": 2, "spe": -1})
restored_mon = battle.Combatant(species=CHAR, level=20, ivs=dict(IVS))
trainer_battle.apply_mon_state(restored_mon, {"current_hp": restored_mon.max_hp, **state_blob})
check("apply_mon_state stellt die Statuswert-Stufen wieder her",
      restored_mon.stat_stages == {"atk": 2, "def": 0, "spa": 0, "spd": 0, "spe": -1})

# --------------------------------------------------------------- Selbstheilung
# --- Erholung (Recover): heilt 50% der maximalen KP, aber nicht ueber das Maximum
healer = mon(normal_sp)
heal_amount = max(1, int(healer.max_hp * 0.5))
healer.current_hp = healer.max_hp - heal_amount - 8  # bleibt unter dem Maximum
expected_after = healer.current_hp + heal_amount
b = battle.Battle(wild=mon(fire_sp), team=[healer])
battle.use_move(b, healer, b.wild, "recover")
check("Erholung heilt exakt 50% der maximalen KP", healer.current_hp == expected_after,
      f"{healer.current_hp} != {expected_after}")
check("Erholung meldet die Heilung im Log", "recovers" in b.log[-1] and str(heal_amount) in b.log[-1])

# --- Erholung schlaegt fehl, wenn bereits volle KP vorhanden sind (kein Item verbraucht o.ae.)
full_healer = mon(normal_sp)
b = battle.Battle(wild=mon(fire_sp), team=[full_healer])
battle.use_move(b, full_healer, b.wild, "recover")
check("Erholung bei vollen KP aendert nichts", full_healer.current_hp == full_healer.max_hp)
check("Log meldet 'But it failed!' bei wirkungsloser Heilung", "failed" in b.log[-1])

# --- KI-Nutzwert: 0 bei voller KP, sonst der erwartete (gedeckelte) Heil-Betrag
check("Erwarteter Nutzwert von Erholung ist 0 bei voller KP",
      battle.expected_damage(mon(normal_sp), mon(fire_sp), "recover") == 0.0)
slightly_hurt = mon(normal_sp)
slightly_hurt.current_hp = slightly_hurt.max_hp - 10
check("Erwarteter Nutzwert ist die fehlende KP, wenn das unter dem Heil-Betrag liegt",
      battle.expected_damage(slightly_hurt, mon(fire_sp), "recover") == 10.0)
badly_hurt = mon(normal_sp)
badly_hurt.current_hp = 1
check("Erwarteter Nutzwert ist auf den Heil-Betrag (50% max. KP) gedeckelt",
      battle.expected_damage(badly_hurt, mon(fire_sp), "recover") == heal_amount)

# ---------------------------------------------------------------------- Flinch
# --- Eine Attacke mit Flinch-Chance (Biss) laesst das Ziel bei Treffer diese
# Runde aussetzen, wenn der Anwender zuerst dran ist (Chance erzwungen).
restore = with_random(random=lambda: 0.0)
try:
    biter = battle.Combatant(species=normal_sp, level=30, ivs=dict(IVS), moves=["bite"])
    target = mon(normal_sp)
    b = battle.Battle(wild=target, team=[biter])
    battle.run_turn(b, "bite")
    check("Biss trifft und richtet Schaden an", target.current_hp < target.max_hp)
    check("Ziel zuckt zusammen und handelt diese Runde nicht mehr", biter.current_hp == biter.max_hp)
    check("Log meldet das Zusammenzucken", any("flinch" in entry.lower() for entry in b.log))
    check("Flinch wird am Rundenende zurueckgesetzt", not target.flinched and not biter.flinched)
finally:
    restore()

# --- Treffer, aber die Flinch-Chance selbst schlaegt fehl: Ziel bleibt frei
restore = with_random(random=lambda: 0.99)
try:
    biter = mon(normal_sp)
    target = mon(normal_sp)
    b = battle.Battle(wild=target, team=[biter])
    battle.use_move(b, biter, target, "bite")
    check("Trifft trotzdem (Kontrolle)", target.current_hp < target.max_hp)
    check("Ohne die noetige Chance bleibt das Ziel nicht zusammengezuckt", not target.flinched)
finally:
    restore()

# --- Wechsel setzt einen (theoretischen) Flinch-Status des Verlassenden zurueck
switcher2 = mon(normal_sp)
switcher2.flinched = True
bench2 = mon(fire_sp)
b = battle.Battle(wild=mon(water_sp), team=[switcher2, bench2])
battle.switch_to(b, 1)
check("Wechsel setzt Flinch des Verlassenden zurueck", not switcher2.flinched)

# ------------------------------------------------- Neue Status-Attacken
# Wasser/Kaefer/Gestein/Boden/Eis/Geist hatten zuvor keine einzige
# Status-Attacke (power=None) - siehe "Jeder Typ hat eine Status-Attacke" oben.

# --- Rueckzug (Wasser): +1 Verteidigung auf sich selbst
withdrawer = mon(water_sp)
b = battle.Battle(wild=mon(fire_sp), team=[withdrawer])
battle.use_move(b, withdrawer, b.wild, "withdraw")
check("Rueckzug erhoeht die eigene Verteidigung um 1 Stufe", withdrawer.stat_stages["def"] == 1)

# --- Fadenschuss (Kaefer): -2 Tempo beim Ziel
spinner = mon(bug_sp)
webbed = mon(normal_sp)
b = battle.Battle(wild=webbed, team=[spinner])
# 95% Genauigkeit: Trefferwurf fixieren, sonst haengt der Test vom Seed ab.
_real_random = battle.random.random
battle.random.random = lambda: 0.0
try:
    battle.use_move(b, spinner, webbed, "string_shot")
finally:
    battle.random.random = _real_random
check("Fadenschuss senkt das Tempo des Ziels um 2 Stufen", webbed.stat_stages["spe"] == -2)

# --- Politur (Gestein): +2 Tempo auf sich selbst
polisher = mon(rock_sp)
b = battle.Battle(wild=mon(normal_sp), team=[polisher])
battle.use_move(b, polisher, b.wild, "rock_polish")
check("Politur erhoeht das eigene Tempo um 2 Stufen", polisher.stat_stages["spe"] == 2)

# --- Aufschuettung (Boden): heilt 50% der maximalen KP wie Erholung/Rastplatz
digger = mon(ground_sp)
dig_heal_amount = max(1, int(digger.max_hp * 0.5))
digger.current_hp = 1
battle.use_move(battle.Battle(wild=mon(water_sp), team=[digger]), digger, mon(water_sp), "shore_up")
check("Aufschuettung heilt 50% der maximalen KP", digger.current_hp == 1 + dig_heal_amount)

# --- Vernebelung/Haze (Eis): setzt alle Statuswert-Stufen BEIDER Seiten zurueck
hazer = mon(ice_sp)
foe = mon(normal_sp)
hazer.stat_stages["atk"] = 2
foe.stat_stages["def"] = -1
b = battle.Battle(wild=foe, team=[hazer])
battle.use_move(b, hazer, foe, "haze")
check("Nebel setzt die eigenen Statuswert-Stufen zurueck", hazer.stat_stages["atk"] == 0)
check("Nebel setzt auch die Statuswert-Stufen des Ziels zurueck", foe.stat_stages["def"] == 0)

# --- Fluch (Geist): kostet dem Anwender die Haelfte seiner maximalen KP und
# belegt das Ziel mit einem eigenen Status ("curse"), der wie Gift pro Runde
# tickt (siehe _tick_status) - hier direkt am Katalogeintrag geprueft, nicht
# ueber real anfeindliche Level (Fluch ist auf test-ghost ab Lv. 30 ohnehin
# nicht zwangslaeufig im Standard-Moveset).
cursed_user = mon(ghost_sp)
cursed_target = mon(water_sp)
b = battle.Battle(wild=cursed_target, team=[cursed_user])
battle.use_move(b, cursed_user, cursed_target, "curse")
check("Fluch kostet dem Anwender die Haelfte seiner maximalen KP",
      cursed_user.current_hp == cursed_user.max_hp - cursed_user.max_hp // 2)
check("Fluch belegt das Ziel mit dem Status 'curse'", cursed_target.status == "curse")
before_curse_tick = cursed_target.current_hp
battle._tick_status(b, cursed_target)  # noqa: SLF001
check("Fluch richtet am Rundenende 1/4 der maximalen KP Schaden an",
      before_curse_tick - cursed_target.current_hp == max(1, cursed_target.max_hp // 4))

# --- Fluch (Geist) gegen Normal-Typ: wirkungslos wie jede Geist-Attacke
# (siehe TYPE_CHART), der Anwender verliert dabei auch keine KP.
immune_user = mon(ghost_sp)
immune_target = mon(normal_sp)
b = battle.Battle(wild=immune_target, team=[immune_user])
battle.use_move(b, immune_user, immune_target, "curse")
check("Fluch gegen Normal-Typ bleibt wirkungslos", immune_target.status is None)
check("Bei Wirkungslosigkeit verliert der Anwender keine KP", immune_user.current_hp == immune_user.max_hp)

# --- Regression (Top-4 Lorelei 500er): Vernebelung (Haze) hat weder Status,
# Heilung noch Statuswerte-Aenderung; die KI-Bewertung darf nicht mit KeyError
# abbrechen und soll Haze nur bei Vorteil fuer den Anwender waehlen.
haze_user = mon(normal_sp)
haze_foe = mon(normal_sp)
try:
    neutral_value = battle.expected_damage(haze_user, haze_foe, "haze")
    haze_ok = True
except KeyError:
    neutral_value, haze_ok = None, False
check("KI-Bewertung von Haze wirft keinen KeyError", haze_ok)
check("Haze ohne Statuswert-Aenderungen ist wertlos", neutral_value == 0.0)
haze_foe.stat_stages["atk"] = 2
check("Haze gegen gesteigerten Gegner hat Wert", battle.expected_damage(haze_user, haze_foe, "haze") > 0)
haze_foe.stat_stages["atk"] = 0
haze_user.stat_stages["atk"] = 2
check("Haze bei eigenen Boosts ist wertlos", battle.expected_damage(haze_user, haze_foe, "haze") == 0.0)

# ------------------------------------- Erweiterter Attackenkatalog + Stufen-UI
# Datenintegritaet aller Attacken mit Stufen-Aenderung: gueltiger Wert/Ziel,
# und jede Selbst-/Gegner-Aenderung muss die Engine auch verarbeiten koennen.
all_moves = moves.all_moves()
check(
    "Alle stat_changes: Statuswert, Stufen (-6..6, !=0) und Ziel sind gueltig",
    all(c["stat"] in battle.STAT_STAGE_KEYS and c["stages"] != 0 and -6 <= c["stages"] <= 6
        and c["target"] in ("self", "target")
        for m in all_moves.values() for c in m.get("stat_changes", [])),
)
check(
    "Chancen (status_chance/stat_chance) liegen zwischen 1 und 100",
    all(1 <= m[k] <= 100 for m in all_moves.values() for k in ("status_chance", "stat_chance") if k in m),
)
check(
    "Kein Move mit Statuswert-Aenderung UND Heilung (die Engine wertet nur eines aus)",
    not any(m.get("heal") and m.get("stat_changes") for m in all_moves.values()),
)
new_moves = ["shell_smash", "overheat", "spore", "soft_boiled", "icy_wind", "drain_punch", "quiver_dance", "scald"]
check("Neue Attacken sind im Katalog", all(k in all_moves for k in new_moves))

# --- Panzerbruch: +2 Atk/SpA/Tmp, aber -1 Def/SpD - alles auf sich selbst
smasher = mon(normal_sp)
b = battle.Battle(wild=mon(fire_sp), team=[smasher])
battle.use_move(b, smasher, b.wild, "shell_smash")
check("Panzerbruch: +2 Angriff/Sp.Angriff/Initiative, -1 Verteidigung/Sp.Verteidigung",
      smasher.stat_stages == {"atk": 2, "def": -1, "spa": 2, "spd": -1, "spe": 2}, str(smasher.stat_stages))
check("Panzerbruch schreibt fuer jede der 5 Aenderungen eine Log-Zeile",
      sum("Shell Smash" not in line and ("rose" in line or "fell" in line) for line in b.log) == 5, str(b.log))

# --- Hitzekoller: Schaden, danach -2 Sp.Angriff beim Anwender (100 %)
heater, heat_target = mon(fire_sp), mon(normal_sp)
b = battle.Battle(wild=heat_target, team=[heater])
restore = with_random(random=lambda: 0.0)
try:
    battle.use_move(b, heater, heat_target, "overheat")
finally:
    restore()
check("Hitzekoller trifft", heat_target.current_hp < heat_target.max_hp)
check("Hitzekoller senkt den EIGENEN Sp.Angriff um 2 Stufen", heater.stat_stages["spa"] == -2)
check("Hitzekoller veraendert das Ziel nicht", not any(heat_target.stat_stages.values()))

# --- Eissturm: Schaden + -1 Initiative beim Ziel (100 %)
wind_user, wind_target = mon(ice_sp), mon(normal_sp)
b = battle.Battle(wild=wind_target, team=[wind_user])
restore = with_random(random=lambda: 0.0)
try:
    battle.use_move(b, wind_user, wind_target, "icy_wind")
finally:
    restore()
check("Eissturm senkt die Initiative des Ziels", wind_target.stat_stages["spe"] == -1)

# --- Pilzsporen: Schlaf (100 % Genauigkeit)
spore_user, spore_target = mon(bug_sp), mon(normal_sp)
b = battle.Battle(wild=spore_target, team=[spore_user])
restore = with_random(random=lambda: 0.0)
try:
    battle.use_move(b, spore_user, spore_target, "spore")
finally:
    restore()
check("Pilzsporen versetzen das Ziel in Schlaf", spore_target.status == "sleep")

# --- Weichei: heilt die Haelfte der Max-KP
healer = mon(normal_sp)
healer.current_hp = 1
b = battle.Battle(wild=mon(fire_sp), team=[healer])
battle.use_move(b, healer, b.wild, "soft_boiled")
check("Weichei heilt die Haelfte der Max-KP",
      healer.current_hp == 1 + max(1, int(healer.max_hp * 0.5)), str(healer.current_hp))

# --- Saugfaust: Drain (Anwender bekommt KP zurueck)
drainer, drain_target = mon(normal_sp), mon(normal_sp)
drainer.current_hp = 1
b = battle.Battle(wild=drain_target, team=[drainer])
restore = with_random(random=lambda: 0.99)  # kein Crit
try:
    battle.use_move(b, drainer, drain_target, "drain_punch")
finally:
    restore()
check("Saugfaust heilt den Anwender", drainer.current_hp > 1, str(drainer.current_hp))

# --- Stufen-Anzeige: die Kampf-Ansicht liefert stat_stages fuer beide Seiten
shown_a, shown_b = mon(normal_sp), mon(normal_sp)
battle.apply_stat_change(b, shown_a, "atk", 2)
battle.apply_stat_change(b, shown_b, "spe", -1)
check("describe() liefert nur die veraenderten Stufen",
      battle.describe(shown_a)["stat_stages"] == {"atk": 2}
      and battle.describe(shown_b)["stat_stages"] == {"spe": -1}
      and battle.describe(mon(normal_sp))["stat_stages"] == {})

# --- Frontend zeigt Stufen (Pills), Effekte auf den Attacken-Buttons + i18n
html = (ROOT / "pokemon_encounter" / "www" / "index.html").read_text(encoding="utf-8")
check("Frontend: Stufen-Pills fuer Gegner UND eigenes Pokemon",
      "statStagePills(w.stat_stages)" in html and "statStagePills(me.stat_stages)" in html)
check("Frontend: Attacken-Buttons zeigen Statuswert-Aenderungen/Heilung", "moveEffectsLine(m)" in html)
i18n_dir = ROOT / "pokemon_encounter" / "app" / "data" / "i18n"
for lang in ("en", "de"):
    catalog = json.loads((i18n_dir / f"{lang}.json").read_text(encoding="utf-8"))
    check(f"i18n {lang}: Stufen-Tooltip und Heilungs-Text vorhanden",
          "battle.stat_stage_title" in catalog and "move.heals" in catalog)

# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
