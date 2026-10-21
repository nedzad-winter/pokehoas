"""Tests fuer Heil-Items (Trank, Beleber, Status-Heiler) - Shop, Beutel,
Anwendung ausserhalb des Kampfs und als Kampf-Aktion (wild + Trainer).

Aufruf:  python3 tests/test_medicine.py
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-medicine-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, db, economy, i18n, medicine, seed, spawn, trainer_battle, trainers  # noqa: E402
from app.api import battles as battles_api  # noqa: E402
from app.api import shop as shop_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


def expect_http(label: str, status: int, fn, *args, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
        check(label, False, "keine Exception")
    except HTTPException as exc:
        check(label, exc.status_code == status, f"{exc.status_code}: {exc.detail}")
        return str(exc.detail)
    return ""


random.seed(int(os.environ.get("TEST_SEED", "1234")))
db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")
db.set_setting("heal_regen_percent_per_hour", 0)  # deterministische KP
IVS = {k: 15 for k in spawn.STAT_KEYS}
NOW = datetime.now().astimezone()


def species(name: str) -> dict:
    return db.query_one("SELECT * FROM species WHERE name = ?", (name,))


PIKA, KARP, CHAR = species("pikachu"), species("magikarp"), species("charmander")


def make_owned(user_id: str, sp: dict, level: int, slot: int | None, hp: int | None = None) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "current_hp, party_slot, caught_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, sp["id"], level, battle.exp_for_level(level, sp["growth_rate"]), 0,
         json.dumps(IVS), "hardy", hp, slot, NOW.isoformat()),
    )


def owned(owned_id: int) -> dict:
    return db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))


def max_hp(owned_id: int) -> int:
    return battle.combatant_from_owned(owned(owned_id)).max_hp


def give(key: str, qty: int, user: str = "alice") -> None:
    db.execute(
        "INSERT INTO inventory (item_key, user_id, quantity) VALUES (?,?,?) "
        "ON CONFLICT(item_key, user_id) DO UPDATE SET quantity = quantity + excluded.quantity",
        (key, user, qty),
    )


def qty(key: str, user: str = "alice") -> int:
    row = db.query_one("SELECT quantity FROM inventory WHERE item_key = ? AND user_id = ?", (key, user))
    return row["quantity"] if row else 0


def use(owned_id: int, key: str, user: dict = alice) -> dict:
    return shop_api.use_item(owned_id, shop_api.UseItemIn(item_key=key), user)


def act(battle_id: int, user: dict = alice, **kwargs) -> dict:
    return battles_api.battle_action(battle_id, battles_api.BattleAction(**kwargs), user)


# ----------------------------------------------------------------- Katalog
EXPECTED = ["potion", "super_potion", "hyper_potion", "max_potion", "full_restore", "revive",
            "max_revive", "antidote", "paralyze_heal", "awakening", "burn_heal", "ice_heal",
            "full_heal"]
cat = medicine.catalog()
check("Alle Heil-Items im Katalog", all(k in cat for k in EXPECTED), sorted(cat))
check("Heil-Items haben deutsche Labels und Beschreibungen",
      all(cat[k].get("label_de") and cat[k].get("description_de") for k in EXPECTED))
check("Heil-Items sind kaeuflich mit positivem Preis",
      all(economy.is_buyable(cat[k]) and cat[k]["price"] > 0 for k in EXPECTED))
check("Preise steigen mit der Wirkung (Trank < Super < Hyper < Top < Top-Genesung)",
      [cat[k]["price"] for k in EXPECTED[:5]] == sorted(cat[k]["price"] for k in EXPECTED[:5]))
check("Beleber billiger als Top-Beleber", cat["revive"]["price"] < cat["max_revive"]["price"])
check("Heil-Items sind keine Haltegegenstaende",
      not any(k in __import__("app.held_items", fromlist=["x"]).catalog() for k in EXPECTED))

shop = shop_api.shop(alice)
shop_keys = {it["key"]: it for it in shop["items"]}
check("Shop listet Heil-Items unter 'medicine'",
      all(shop_keys.get(k, {}).get("category") == "medicine" for k in EXPECTED))

# ------------------------------------------------------- Regeln (rein)
ce = medicine.compute_effect
check("Trank: +20 KP", ce(cat["potion"], 10, 50, None).new_hp == 30)
check("Trank: gedeckelt bei max. KP", ce(cat["potion"], 45, 50, None).new_hp == 50)
check("Trank bei vollen KP: wirkungslos", ce(cat["potion"], 50, 50, None) == "medicine.error.full_hp")
check("Trank auf Besiegtes: abgelehnt", ce(cat["potion"], 0, 50, None) == "medicine.error.fainted")
check("Top-Trank: volle KP", ce(cat["max_potion"], 1, 200, None).new_hp == 200)
check("Beleber: 50 % der max. KP", ce(cat["revive"], 0, 51, "burn").new_hp == 25)
check("Beleber: Status weg", ce(cat["revive"], 0, 51, "burn").new_status is None)
check("Top-Beleber: volle KP", ce(cat["max_revive"], 0, 51, None).new_hp == 51)
check("Beleber auf Fittes: abgelehnt", ce(cat["revive"], 10, 50, None) == "medicine.error.not_fainted")
check("Beleber bei 1 max. KP: mind. 1 KP", ce(cat["revive"], 0, 1, None).new_hp == 1)
check("Gegengift heilt Gift", ce(cat["antidote"], 10, 50, "poison").cured == "poison")
check("Gegengift heilt schwere Vergiftung", ce(cat["antidote"], 10, 50, "toxic").new_status is None)
check("Gegengift ohne Gift: abgelehnt", ce(cat["antidote"], 10, 50, "burn") == "medicine.error.no_status")
check("Gegengift heilt keine KP", ce(cat["antidote"], 10, 50, "poison").new_hp == 10)
check("Aufwecker weckt", ce(cat["awakening"], 10, 50, "sleep").new_status is None)
check("Hyperheiler heilt jeden Status",
      all(ce(cat["full_heal"], 10, 50, s).new_status is None for s in medicine.ALL_STATUSES))
check("Top-Genesung: volle KP + Status", (lambda e: e.new_hp == 50 and e.new_status is None)(
      ce(cat["full_restore"], 5, 50, "paralysis")))
check("Top-Genesung bei vollen KP, aber Status: heilt Status",
      ce(cat["full_restore"], 50, 50, "freeze").cured == "freeze")
check("Top-Genesung bei vollen KP ohne Status: abgelehnt",
      ce(cat["full_restore"], 50, 50, None) == "medicine.error.full_hp")
check("Status-Heiler auf Besiegtes: abgelehnt",
      ce(cat["full_heal"], 0, 50, "burn") == "medicine.error.fainted")

# ------------------------------------------------- Kauf + ausserhalb Kampf
money_before = economy.balance("alice")
economy.credit("alice", 100000)
bought = economy.purchase("alice", "potion", 3)
check("Trank kaufen bucht ab", bought["owned"] == 3 and bought["total"] == 3 * shop_keys["potion"]["price"])

pika = make_owned("alice", PIKA, 30, 1)
char = make_owned("alice", CHAR, 20, 2)
pika_max = max_hp(pika)

d = expect_http("Trank bei vollen KP (NULL) abgelehnt", 400, use, pika, "potion")
check("... Meldung nennt volle KP", "full HP" in d, d)
check("... Trank nicht verbraucht", qty("potion") == 3)

db.execute("UPDATE owned_pokemon SET current_hp = 5 WHERE id = ?", (pika,))
r = use(pika, "potion")
check("Trank heilt 20 KP", owned(pika)["current_hp"] == 25 and r["healed"] == 20, r)
check("Trank verbraucht", qty("potion") == 2 and r["remaining"] == 2)
check("Antwort enthaelt Meldung", "recovered 20 HP" in r["message"], r["message"])

db.execute("UPDATE owned_pokemon SET current_hp = ? WHERE id = ?", (pika_max - 3, pika))
r = use(pika, "potion")
check("Trank deckelt bei max. KP", owned(pika)["current_hp"] == pika_max and r["healed"] == 3)

db.execute("UPDATE owned_pokemon SET current_hp = 0 WHERE id = ?", (pika,))
d = expect_http("Trank auf Besiegtes abgelehnt", 400, use, pika, "potion")
check("... Meldung verweist auf Beleber", "Revive" in d, d)
expect_http("Beleber nicht im Beutel", 409, use, pika, "revive")

give("revive", 2)
give("max_revive", 1)
expect_http("Beleber auf Fittes abgelehnt", 400, use, char, "revive")
check("... Beleber nicht verbraucht", qty("revive") == 2)
r = use(pika, "revive")
check("Beleber: halbe KP", owned(pika)["current_hp"] == pika_max // 2 and r["revived"], r)
db.execute("UPDATE owned_pokemon SET current_hp = 0 WHERE id = ?", (pika,))
use(pika, "max_revive")
check("Top-Beleber: volle KP", owned(pika)["current_hp"] == pika_max)

give("antidote", 1)
d = expect_http("Gegengift ausserhalb des Kampfs wirkungslos", 400, use, pika, "antidote")
check("... Meldung erklaert: Status nur im Kampf", "only exist during a battle" in d, d)

give("full_restore", 1)
expect_http("Top-Genesung bei vollen KP abgelehnt", 400, use, pika, "full_restore")
db.execute("UPDATE owned_pokemon SET current_hp = 1 WHERE id = ?", (pika,))
use(pika, "full_restore")
check("Top-Genesung ausserhalb: volle KP", owned(pika)["current_hp"] == pika_max)

bob_mon = make_owned("bob", KARP, 10, 1, hp=1)
expect_http("Fremdes Pokémon: 404", 404, use, bob_mon, "potion")
expect_http("Unbekanntes Item: 400", 400, use, pika, "not_an_item")

db.set_user_language("alice", "de")
db.execute("UPDATE owned_pokemon SET current_hp = NULL WHERE id = ?", (pika,))
d = expect_http("Deutsche Fehlermeldung", 400, use, pika, "potion")
check("... ist deutsch", "volle KP" in d, d)
db.set_user_language("alice", "en")

# Beutel-Ansicht: passende Ziele
db.execute("UPDATE owned_pokemon SET current_hp = 3 WHERE id = ?", (char,))
db.execute("UPDATE owned_pokemon SET current_hp = 0 WHERE id = ?", (pika,))
bag = {it["key"]: it for it in shop_api.bag(alice)["items"]}
check("Beutel: Trank-Ziele nur angeschlagene, nicht besiegte",
      [t["owned_id"] for t in bag["potion"]["targets"]] == [char], bag["potion"]["targets"])
check("Beutel: Beleber-Ziele nur besiegte",
      [t["owned_id"] for t in bag["revive"]["targets"]] == [pika], bag["revive"]["targets"])
check("Beutel: Gegengift als 'nur im Kampf' markiert",
      bag["antidote"]["battle_only"] and bag["antidote"]["targets"] == [])
check("Beutel: Trank nicht 'nur im Kampf'", bag["potion"]["battle_only"] is False)

# ------------------------------------------------------------ Wilder Kampf
db.execute("UPDATE owned_pokemon SET current_hp = NULL WHERE id = ?", (pika,))
db.execute("UPDATE owned_pokemon SET current_hp = 5 WHERE id = ?", (char,))
enc_id = spawn.create_encounter(KARP, source_entity="test.trigger")
db.execute("UPDATE encounter SET level = 2, ivs = ? WHERE id = ?", (json.dumps(IVS), enc_id))
view = battles_api.start_battle(enc_id, alice)
bid = view["id"]
check("Kampfansicht listet Heil-Items",
      {m["key"] for m in view["medicine"]} >= {"potion", "antidote", "revive"}, view["medicine"])

expect_http("Ausserhalb-Anwendung waehrend Kampf gesperrt", 409, use, char, "potion")
potions = qty("potion")
turn = view["turn"]
d = expect_http("Trank im Kampf bei vollen KP abgelehnt", 409, act, bid, action="item",
                item="potion", target=pika)
check("... nicht verbraucht, keine Runde", qty("potion") == potions
      and battles_api.get_battle(bid, alice)["turn"] == turn)
expect_http("Item auf Nicht-Team-Mitglied abgelehnt", 409, act, bid, action="item",
            item="potion", target=bob_mon)
expect_http("Kein Heil-Item: abgelehnt", 409, act, bid, action="item", item="poke_ball")

view = act(bid, action="item", item="potion", target=char)
check("Trank im Kampf auf Bank-Pokémon: +20 KP",
      next(m for m in view["team"] if m["owned_id"] == char)["current_hp"] == 25)
check("... kostet die Runde", view["turn"] == turn + 1, view["turn"])
check("... Log meldet Einsatz", any("You used Potion" in line for line in view["new_log"]), view["new_log"])
check("... Trank verbraucht", qty("potion") == potions - 1)
check("... KP in owned_pokemon gespeichert", owned(char)["current_hp"] == 25)

# Status (lebt nur im Kampfzustand) + Status-Heiler
session = db.query_one("SELECT * FROM battle_session WHERE id = ?", (bid,))
state = json.loads(session["state"])
state["statuses"] = {str(pika): ["poison", 0]}
db.execute("UPDATE battle_session SET state = ? WHERE id = ?", (json.dumps(state), bid))
expect_http("Aufwecker gegen Gift abgelehnt", 409, act, bid, action="item",
            item="awakening", target=pika)
give("awakening", 1)
expect_http("Aufwecker gegen Gift abgelehnt (im Beutel)", 409, act, bid, action="item",
            item="awakening", target=pika)
check("... Aufwecker nicht verbraucht", qty("awakening") == 1)
view = act(bid, action="item", item="antidote")  # ohne target: aktives Pokémon
active = next(m for m in view["team"] if m["active"])
check("Gegengift im Kampf heilt Gift", active["owned_id"] == pika and active["status"] is None,
      active["status"])
check("... Log meldet Heilung", any("cured of Poison" in line for line in view["new_log"]), view["new_log"])
check("... Gegengift verbraucht", qty("antidote") == 0)

# Beleber im Kampf auf besiegtes Bank-Pokémon
db.execute("UPDATE owned_pokemon SET current_hp = 0 WHERE id = ?", (char,))
view = act(bid, action="item", item="revive", target=char)
char_view = next(m for m in view["team"] if m["owned_id"] == char)
check("Beleber im Kampf: Bank-Pokémon mit halben KP zurueck",
      char_view["current_hp"] == char_view["max_hp"] // 2, char_view)
view = act(bid, action="run")
check("Flucht nach Items moeglich", view["outcome"] == "ran")
check("Beleber-KP bleiben nach Kampf", owned(char)["current_hp"] == char_view["max_hp"] // 2)

# Pflichtwechsel sperrt Items
db.execute("UPDATE owned_pokemon SET current_hp = NULL WHERE id IN (?, ?)", (pika, char))
view = battles_api.start_battle(enc_id, alice)
bid2 = view["id"]
session = db.query_one("SELECT * FROM battle_session WHERE id = ?", (bid2,))
state = json.loads(session["state"])
state["needs_switch"] = True
db.execute("UPDATE battle_session SET state = ? WHERE id = ?", (json.dumps(state), bid2))
db.execute("UPDATE owned_pokemon SET current_hp = 0 WHERE id = ?", (pika,))
give("revive", 1)
expect_http("Beleber bei Pflichtwechsel abgelehnt", 409, act, bid2, action="item",
            item="revive", target=pika)
check("... Beleber nicht verbraucht", qty("revive") == 1)
act(bid2, action="switch", target=char)
act(bid2, action="run")

# ------------------------------------------------------------ Trainerkampf
_saved_raw, _saved_trainers = trainers._raw, trainers._trainers
trainers._raw = {
    "regions": [{"key": "kanto", "label": "Kanto"}],
    "trainers": [{"id": "test_med", "kind": "gym_leader", "region": "kanto",
                  "name": "Testo", "team": [["magikarp", 3]]}],
}
trainers._trainers = None
db.execute("UPDATE owned_pokemon SET current_hp = 10 WHERE id = ?", (pika,))
give("potion", 2)
view = trainer_battle.start("test_med", "alice")
check("Trainerkampf-Ansicht listet Heil-Items", any(m["key"] == "potion" for m in view["medicine"]))
before = next(m for m in view["team"] if m["active"])
turn = view["turn"]
potions = qty("potion")
view = trainer_battle.act(view["id"], "alice", {"action": "item", "item": "potion", "target": pika})
after = next(m for m in view["team"] if m["active"])
check("Trank im Trainerkampf heilt (vor Gegnerzug)",
      any("recovered" in line for line in view["new_log"]), view["new_log"])
check("... kostet die Runde", view["turn"] == turn + 1)
check("... Trank verbraucht", qty("potion") == potions - 1)
check("... KP gestiegen oder Gegner hat getroffen", after["current_hp"] >= before["current_hp"]
      or any("used" in line.lower() for line in view["new_log"]))
try:
    trainer_battle.act(view["id"], "alice", {"action": "item", "item": "revive", "target": pika})
    check("Beleber auf Fittes im Trainerkampf abgelehnt", False)
except HTTPException as exc:
    check("Beleber auf Fittes im Trainerkampf abgelehnt", exc.status_code == 409, exc.detail)
trainer_battle.act(view["id"], "alice", {"action": "run"})
trainers._raw, trainers._trainers = _saved_raw, _saved_trainers

# -------------------------------------------------------------- i18n
for key in ["item_category.medicine", "medicine.error.full_hp", "medicine.log.used",
            "medicine.result.revived", "medicine.battle.costs_turn"]:
    en_text, de_text = i18n.catalog("en").get(key), i18n.catalog("de").get(key)
    check(f"i18n en+de: {key}", bool(en_text) and bool(de_text) and en_text != de_text)

# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
