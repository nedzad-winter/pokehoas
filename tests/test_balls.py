"""Tests fuer Pokébaelle: Multiplikatoren der Spezialbaelle, Fang-Kontext in
Warteschlange und manuellem Kampf, Heilball, Shop und die Rogueball-Migration.

Aufruf:  python3 tests/test_balls.py
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-balls-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, battle_session, db, economy, seed, spawn  # noqa: E402
from app.api import battles as battles_api  # noqa: E402
from app.api import encounters as encounters_api  # noqa: E402
from app.api import shop as shop_api  # noqa: E402

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
IVS = {k: 15 for k in spawn.STAT_KEYS}
NOW = datetime.now().astimezone()


def species(name: str) -> dict:
    return db.query_one("SELECT * FROM species WHERE name = ?", (name,))


PIKA, SQUIRT, CHAR = species("pikachu"), species("squirtle"), species("charmander")
# Kein Kaefer / kein schnelles Pokémon im Fallback-Datensatz -> abgewandelte Kopien.
BUG = dict(PIKA, type1="bug", type2=None)
FAST = dict(PIKA, base_spe=110)


def wild(sp: dict, level: int = 10) -> battle.Combatant:
    return battle.Combatant(species=sp, level=level, ivs=dict(IVS), is_wild=True)


def mult(ball: str, w: battle.Combatant, **ctx) -> float:
    return battle.ball_multiplier(ball, w, battle.CatchContext(**ctx))


def make_owned(sp: dict, level: int, slot: int | None) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "current_hp, party_slot, caught_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("alice", sp["id"], level, battle.exp_for_level(level, sp["growth_rate"]), 0,
         json.dumps(IVS), "hardy", None, slot, NOW.isoformat()),
    )


def make_encounter(sp: dict, level: int, current_hp: int | None = None,
                   biome: str | None = None) -> int:
    encounter_id = spawn.create_encounter(sp, source_entity="test.trigger", biome=biome)
    db.execute("UPDATE encounter SET level = ?, ivs = ?, current_hp = ? WHERE id = ?",
               (level, json.dumps(IVS), current_hp, encounter_id))
    return encounter_id


def give(item: str, quantity: int) -> None:
    db.execute(
        "INSERT INTO inventory (item_key, user_id, quantity) VALUES (?,?,?) "
        "ON CONFLICT(item_key, user_id) DO UPDATE SET quantity = excluded.quantity",
        (item, "alice", quantity),
    )


def qty(item: str, user_id: str = "alice") -> int:
    row = db.query_one("SELECT quantity FROM inventory WHERE item_key = ? AND user_id = ?",
                       (item, user_id))
    return row["quantity"] if row else 0


# ---------------------------------------------------------------- Katalog
check("Rogueball ist entfernt", "rogue_ball" not in battle.BALLS)
check("Standardbaelle unveraendert",
      [battle.BALLS[k]["bonus"] for k in ("poke_ball", "great_ball", "ultra_ball")] == [1, 1.5, 2])
shop_keys = {item["key"] for item in economy.catalog() if item["category"] == "ball"}
check("Jeder Ball hat einen Shop-Eintrag", set(battle.BALLS) == shop_keys,
      set(battle.BALLS) ^ shop_keys)
check("Shop-Baelle haben Beschreibungen in en+de",
      all(item.get("description") and item.get("description_de") and item.get("label_de")
          for item in economy.catalog() if item["category"] == "ball"))
sold = {item["key"] for item in shop_api.shop(alice)["items"]}
check("Shop verkauft die neuen Baelle",
      {"net_ball", "dusk_ball", "quick_ball", "timer_ball", "nest_ball", "repeat_ball",
       "dive_ball", "level_ball", "fast_ball", "heal_ball"} <= sold)
check("Shop verkauft keinen Rogueball", "rogue_ball" not in sold)

# ------------------------------------------------------ Multiplikatoren
check("Standardball ohne Kontext", mult("great_ball", wild(PIKA)) == 1.5)

check("Netzball: Wasser x3,5", mult("net_ball", wild(SQUIRT)) == 3.5)
check("Netzball: Kaefer x3,5", mult("net_ball", wild(BUG)) == 3.5)
check("Netzball: sonst x1", mult("net_ball", wild(CHAR)) == 1.0)

check("Tauchball: See x3,5", mult("dive_ball", wild(PIKA), biome="lake") == 3.5)
check("Tauchball: Strand x3,5", mult("dive_ball", wild(PIKA), biome="beach") == 3.5)
check("Tauchball: sonst x1", mult("dive_ball", wild(PIKA), biome="forest") == 1.0
      and mult("dive_ball", wild(PIKA)) == 1.0)

check("Nestball: Lv. 1 -> x4", mult("nest_ball", wild(PIKA, 1)) == 4.0)
check("Nestball: Lv. 21 -> x2", abs(mult("nest_ball", wild(PIKA, 21)) - 2.0) < 1e-9)
check("Nestball: Lv. 29 -> x1,2", abs(mult("nest_ball", wild(PIKA, 29)) - 1.2) < 1e-9)
check("Nestball: ab Lv. 30 x1", mult("nest_ball", wild(PIKA, 30)) == 1.0
      and mult("nest_ball", wild(PIKA, 50)) == 1.0)

check("Wiederball: schon gefangen x3,5", mult("repeat_ball", wild(PIKA), species_caught=True) == 3.5)
check("Wiederball: neu x1", mult("repeat_ball", wild(PIKA)) == 1.0)

check("Timerball: Runde 0 x1", mult("timer_ball", wild(PIKA), turn=0) == 1.0)
check("Timerball: waechst mit Runden",
      1.0 < mult("timer_ball", wild(PIKA), turn=1) < mult("timer_ball", wild(PIKA), turn=5) < 4.0,
      [round(mult("timer_ball", wild(PIKA), turn=t), 2) for t in (1, 5)])
check("Timerball: max x4", mult("timer_ball", wild(PIKA), turn=10) == 4.0
      and mult("timer_ball", wild(PIKA), turn=99) == 4.0)

check("Flottball: erste Runde x5", mult("quick_ball", wild(PIKA), first_turn=True) == 5.0)
check("Flottball: spaeter x1", mult("quick_ball", wild(PIKA), first_turn=False) == 1.0)

night = NOW.replace(hour=22, minute=0)
early = NOW.replace(hour=3, minute=30)
noon = NOW.replace(hour=12, minute=0)
evening = NOW.replace(hour=19, minute=59)
check("Finsterball: 22 Uhr x3", mult("dusk_ball", wild(PIKA), now=night) == 3.0)
check("Finsterball: 3:30 Uhr x3", mult("dusk_ball", wild(PIKA), now=early) == 3.0)
check("Finsterball: Mittag x1", mult("dusk_ball", wild(PIKA), now=noon) == 1.0)
check("Finsterball: 19:59 x1", mult("dusk_ball", wild(PIKA), now=evening) == 1.0)

check("Levelball: >= 4-fach x8", mult("level_ball", wild(PIKA, 10), player_level=40) == 8.0)
check("Levelball: > 2-fach x4", mult("level_ball", wild(PIKA, 10), player_level=21) == 4.0)
check("Levelball: genau 2-fach x2", mult("level_ball", wild(PIKA, 10), player_level=20) == 2.0)
check("Levelball: hoeher x2", mult("level_ball", wild(PIKA, 10), player_level=11) == 2.0)
check("Levelball: gleich/niedriger x1", mult("level_ball", wild(PIKA, 10), player_level=10) == 1.0
      and mult("level_ball", wild(PIKA, 10), player_level=5) == 1.0)
check("Levelball: ohne Team x1", mult("level_ball", wild(PIKA, 10)) == 1.0)

check("Turboball: Initiative >= 100 x4", mult("fast_ball", wild(FAST)) == 4.0)
check("Turboball: langsam x1", mult("fast_ball", wild(PIKA)) == 1.0)

check("Heilball: x1", mult("heal_ball", wild(PIKA)) == 1.0)
check("Unbekannter Ball -> wie Pokéball", mult("rogue_ball", wild(PIKA)) == 1.0)

weak = wild(SQUIRT, 20)
weak.current_hp = max(1, weak.max_hp // 4)
check("Verstaerkter Ball erhoeht die Fangchance",
      battle.catch_probability(weak, "net_ball") > battle.catch_probability(weak, "ultra_ball")
      > battle.catch_probability(weak, "poke_ball"))
check("Unverstaerkter Spezialball = Pokéball",
      battle.catch_probability(wild(CHAR), "net_ball") == battle.catch_probability(wild(CHAR), "poke_ball"))

opts = {o["key"]: o for o in battle.ball_options(wild(SQUIRT), battle.CatchContext(), {"net_ball": 2})}
check("ball_options: Netzball gegen Wasser markiert",
      opts["net_ball"]["boosted"] and opts["net_ball"]["multiplier"] == 3.5
      and opts["net_ball"]["count"] == 2)
check("ball_options: Standardbaelle nie 'boosted'",
      not opts["ultra_ball"]["boosted"] and not opts["master_ball"]["boosted"])
check("ball_options: Bedingung nicht erfuellt -> nicht markiert", not opts["dusk_ball"]["boosted"]
      or battle.is_night())

# ------------------------------------------------------ Fang-Kontext
lead = make_owned(CHAR, 45, 1)
ctx = battle.catch_context(wild(PIKA, 10), "alice")
check("catch_context: Levelball nimmt Team-Platz 1", ctx.player_level == 45)
check("catch_context: Art noch nicht gefangen", ctx.species_caught is False)
db.execute(
    "INSERT INTO dex_entry (species_id, user_id, seen_count, caught_count) VALUES (?,?,1,1)",
    (PIKA["id"], "alice"),
)
check("catch_context: Art schon gefangen -> Wiederball",
      battle.catch_context(wild(PIKA, 10), "alice").species_caught is True)

fresh = make_encounter(SQUIRT, 10, biome="lake")
dec = encounters_api._decorate(dict(db.query_one("SELECT * FROM encounter WHERE id = ?", (fresh,))), "alice")
boosts = dec["ball_boosts"]
check("Warteschlange: Flottball bei unberuehrtem Encounter", boosts.get("quick_ball") == 5.0, boosts)
check("Warteschlange: Netzball/Tauchball/Levelball markiert",
      boosts.get("net_ball") == 3.5 and boosts.get("dive_ball") == 3.5
      and boosts.get("level_ball") == 8.0, boosts)
check("Warteschlange: Timerball ausserhalb eines Kampfs x1", "timer_ball" not in boosts)
touched = make_encounter(SQUIRT, 10, current_hp=5)
dec = encounters_api._decorate(dict(db.query_one("SELECT * FROM encounter WHERE id = ?", (touched,))), "alice")
check("Warteschlange: kein Flottball nach Kampf/Fehlwurf", "quick_ball" not in dec["ball_boosts"])

# ------------------------------------------------------ Manueller Kampf
give("quick_ball", 3)
give("timer_ball", 3)
e_battle = make_encounter(PIKA, 10)
view = battles_api.start_battle(e_battle, alice)
by_key = {b["key"]: b for b in view["balls"]}
check("Kampf: Ballauswahl liefert Multiplikator + Markierung",
      all("multiplier" in b and "boosted" in b for b in view["balls"]))
check("Kampf: Flottball in Runde 0 verstaerkt", by_key["quick_ball"]["boosted"]
      and by_key["quick_ball"]["multiplier"] == 5.0)
check("Kampf: Wiederball verstaerkt (Pikachu schon gefangen)", by_key["repeat_ball"]["boosted"])
check("Kampf: Levelball gegen aktives Pokémon", by_key["level_ball"]["multiplier"] == 8.0)

session_row = db.query_one("SELECT * FROM battle_session WHERE id = ?", (view["id"],))
b, state, encounter = battle_session._load(session_row)
b.turn = 6
battle_session._save(session_row, b, state, "ongoing")
view = battles_api.get_battle(view["id"], alice)
by_key = {b["key"]: b for b in view["balls"]}
check("Kampf: Timerball nach 6 Runden verstaerkt",
      by_key["timer_ball"]["boosted"] and 2.5 < by_key["timer_ball"]["multiplier"] < 3.0,
      by_key["timer_ball"]["multiplier"])
check("Kampf: Flottball nach Runde 0 nicht mehr verstaerkt", not by_key["quick_ball"]["boosted"])

# Der Kontext kommt beim Wurf wirklich an.
seen: dict = {}
orig_throw = battle.throw_ball


def spy(w, user_id, ball_key="poke_ball", status=None, context=None):
    seen["ctx"] = context
    return battle.CatchResult(False, False, 1, 0.1)


battle.throw_ball = spy
try:
    battles_api.battle_action(view["id"], battles_api.BattleAction(action="ball", ball="timer_ball"), alice)
finally:
    battle.throw_ball = orig_throw
check("Kampf: Wurf bekommt Rundenzahl im Kontext",
      seen.get("ctx") is not None and seen["ctx"].turn == 6 and not seen["ctx"].first_turn)

# ---------------------------------------------------------------- Heilball
give("heal_ball", 1)
e_heal = make_encounter(PIKA, 10, current_hp=3)
row = db.query_one("SELECT * FROM encounter WHERE id = ?", (e_heal,))
w = battle.combatant_from_encounter(row)
res = encounters_api.finalize_catch(row, w, "alice", "heal_ball")
got = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (res["owned_id"],))
check("Heilball: Pokémon kommt mit vollen KP an", got["current_hp"] == w.max_hp and w.max_hp > 3,
      f"{got['current_hp']}/{w.max_hp}")
e_plain = make_encounter(PIKA, 10, current_hp=3)
row = db.query_one("SELECT * FROM encounter WHERE id = ?", (e_plain,))
res = encounters_api.finalize_catch(row, battle.combatant_from_encounter(row), "alice", "poke_ball")
check("Andere Baelle heilen nicht",
      db.query_one("SELECT current_hp FROM owned_pokemon WHERE id = ?", (res["owned_id"],))["current_hp"] == 3)

# ------------------------------------------------- Rogueball-Migration
bob = db.upsert_user("bob", "Bob")
db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES ('rogue_ball','bob',3)")
db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES ('ultra_ball','bob',1)")
db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES ('rogue_ball','alice',2)")
db.set_setting("starter_inventory", {"poke_ball": 10, "rogue_ball": 1})
db.init_db()  # Migration laeuft bei jedem Start
check("Migration: Rogueball -> 2 Hyperbaelle je Stueck (addiert)", qty("ultra_ball", "bob") == 7,
      qty("ultra_ball", "bob"))
check("Migration: auch ohne vorhandene Hyperbaelle", qty("ultra_ball", "alice") == 4)
check("Migration: keine Rogueball-Zeilen mehr",
      db.query_one("SELECT COUNT(*) AS n FROM inventory WHERE item_key = 'rogue_ball'")["n"] == 0)
check("Migration: starter_inventory umgestellt",
      db.get_setting("starter_inventory") == {"poke_ball": 10, "ultra_ball": 2},
      db.get_setting("starter_inventory"))
db.init_db()
check("Migration ist idempotent", qty("ultra_ball", "bob") == 7
      and db.get_setting("starter_inventory") == {"poke_ball": 10, "ultra_ball": 2})

# Unbekannte Item-Keys im Inventar (z. B. vor der Migration) crashen nichts.
db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES ('rogue_ball','bob',1)")
bag = shop_api.bag(bob)
check("Beutel verkraftet unbekannte Keys",
      any(it["key"] == "rogue_ball" for it in bag["items"]))
check("item_label faellt auf den Key zurueck", economy.item_label("rogue_ball") == "rogue_ball")

# --------------------------------------- Meisterball-Rueckfrage (index.html)
# Der Meisterball ist nicht kaufbar und faengt immer - ein Fehlklick ist daher
# nicht reparierbar. Das Frontend fragt in beiden Wurf-Pfaden (Warteschlange und
# manueller Kampf) doppelt nach. Ohne JS-Testlauf wenigstens hier festnageln,
# damit die Rueckfrage bei einem Umbau nicht still verschwindet.
html = (ROOT / "pokemon_encounter" / "www" / "index.html").read_text(encoding="utf-8")
check("Frontend hat einen Meisterball-Waechter", "function confirmMasterBall(" in html)
check("Der Waechter fragt zweimal nach",
      html.count("common.master_ball_confirm'") == 1
      and html.count("common.master_ball_confirm_again'") == 1)
check("Wurf in der Warteschlange geht durch den Waechter",
      "if (!confirmMasterBall(ball, card.dataset.name" in html)
check("Wurf im manuellen Kampf geht durch den Waechter",
      "if (!confirmMasterBall(ball, w.name" in html)
check("Der Waechter laesst andere Baelle durch",
      "if (ball !== MASTER_BALL) return true;" in html)


# ------------------------------------- Ballverbrauch und Bestand nach Fehlwurf
# Ein Wurf verbraucht den Ball auch bei einem Fehlwurf. consume_ball prueft und
# zieht in einem Statement ab (parallele Wuerfe koennen den letzten Ball nicht
# doppelt ausgeben). Die Warteschlange zeigt nach jedem Wurf den neuen Bestand.
give_ball = "INSERT INTO inventory (item_key, user_id, quantity) VALUES (?,?,?) " \
            "ON CONFLICT(item_key, user_id) DO UPDATE SET quantity = excluded.quantity"
db.execute(give_ball, ("master_ball", "alice", 1))
check("consume_ball: letzter Ball wird abgezogen", encounters_api.consume_ball("master_ball", "alice") is True
      and qty("master_ball") == 0)
check("consume_ball: ohne Bestand kein Abzug unter 0",
      encounters_api.consume_ball("master_ball", "alice") is False and qty("master_ball") == 0)
db.execute(give_ball, ("poke_ball", "alice", 2))
check("consume_ball: zweiter Ball wird ebenfalls abgezogen",
      encounters_api.consume_ball("poke_ball", "alice") and encounters_api.consume_ball("poke_ball", "alice")
      and qty("poke_ball") == 0)
check("Frontend: Ballauswahl wird nach jedem Wurf neu aufgebaut",
      "async function refreshBallPickers()" in html and "await refreshBallPickers();" in html)
check("Frontend: Ballauswahl baut Optionen aus dem Bestand",
      "function queueBallOptions(inv, boosts)" in html and "queueBallOptions(inv, enc.ball_boosts" in html)


# ---------------------------------- Option: ersten Fang einer Art automatisch sperren
CHIKO, CYNDA, TOTO = species("chikorita"), species("cyndaquil"), species("totodile")


def is_locked(owned_id: int) -> bool:
    return bool(db.query_one("SELECT locked FROM owned_pokemon WHERE id = ?", (owned_id,))["locked"])


def catch(sp: dict, user_id: str) -> int:
    encounter_id = make_encounter(sp, 10)
    row = db.query_one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))
    res = encounters_api.finalize_catch(row, battle.combatant_from_encounter(row), user_id, "poke_ball")
    return res["owned_id"]


check("auto_lock_first_catch: Standard ist aus", db.user_auto_lock_first_catch("alice") is False)
check("auto_lock_first_catch: erster Fang bleibt unversperrt, solange die Option aus ist",
      not is_locked(catch(CHIKO, "alice")))

db.set_user_auto_lock_first_catch("alice", True)
check("auto_lock_first_catch: Option laesst sich einschalten",
      db.user_auto_lock_first_catch("alice") is True)
first_owned = catch(CYNDA, "alice")
check("auto_lock_first_catch: erster Fang einer Art wird gesperrt", is_locked(first_owned))
second_owned = catch(CYNDA, "alice")
check("auto_lock_first_catch: zweiter Fang derselben Art bleibt unversperrt",
      not is_locked(second_owned))

check("auto_lock_first_catch: ist je Spieler getrennt (bob hat die Option nicht an)",
      db.user_auto_lock_first_catch("bob") is False
      and not is_locked(catch(TOTO, "bob")))


# ------------------------------------------- Option: Shiny-Faenge automatisch sperren
carol = db.upsert_user("carol", "Carol")


def catch_shiny(sp: dict, user_id: str, shiny: bool) -> int:
    encounter_id = make_encounter(sp, 10)
    db.execute("UPDATE encounter SET shiny = ? WHERE id = ?", (int(shiny), encounter_id))
    row = db.query_one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))
    res = encounters_api.finalize_catch(row, battle.combatant_from_encounter(row), user_id, "poke_ball")
    return res["owned_id"]


check("auto_lock_shiny: Standard ist an", db.user_auto_lock_shiny("carol") is True)
check("auto_lock_shiny: normaler (nicht-shiny) Fang bleibt unversperrt",
      not is_locked(catch_shiny(CHIKO, "carol", False)))
check("auto_lock_shiny: Shiny-Fang wird automatisch gesperrt",
      is_locked(catch_shiny(CYNDA, "carol", True)))

db.set_user_auto_lock_shiny("carol", False)
check("auto_lock_shiny: Option laesst sich ausschalten", db.user_auto_lock_shiny("carol") is False)
check("auto_lock_shiny: bei ausgeschalteter Option bleibt ein Shiny-Fang unversperrt",
      not is_locked(catch_shiny(TOTO, "carol", True)))

check("auto_lock_shiny: ist je Spieler getrennt (bob hat weiter den Standard an, waehrend carol aus hat)",
      db.user_auto_lock_shiny("bob") is True
      and is_locked(catch_shiny(PIKA, "bob", True))
      and db.user_auto_lock_shiny("carol") is False)


# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
