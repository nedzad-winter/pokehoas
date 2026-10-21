"""Tests fuer Fleisspunkte (EVs, app/evs.py) - Formel, Katalog der EV-Items,
Regeln (Vitamin/Feder/Beere), Anwendung im Beutel, Wirkung im Kampf sowie die
Grenzen als Einstellung.

Aufruf:  python3 tests/test_evs.py
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

TMP = tempfile.mkdtemp(prefix="pokemon-evs-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, db, economy, evs, healing, i18n, pvp, seed, spawn  # noqa: E402
from app.api import config_routes  # noqa: E402
from app.api import shop as shop_api  # noqa: E402
from app.api import team as team_api  # noqa: E402

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
economy.credit("alice", 1_000_000)


def species(name: str) -> dict:
    return db.query_one("SELECT * FROM species WHERE name = ?", (name,))


PIKA, KARP = species("pikachu"), species("magikarp")


def make_owned(user_id: str, sp: dict, level: int, slot: int | None,
               hp: int | None = None) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "current_hp, party_slot, caught_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, sp["id"], level, battle.exp_for_level(level, sp["growth_rate"]), 0,
         json.dumps(IVS), "hardy", hp, slot, NOW.isoformat()),
    )


def owned(owned_id: int) -> dict:
    return db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))


def set_evs(owned_id: int, values: dict[str, int]) -> None:
    db.execute("UPDATE owned_pokemon SET evs = ? WHERE id = ?",
               (json.dumps({k: int(values.get(k, 0)) for k in evs.STAT_KEYS}), owned_id))


def give(key: str, qty: int, user: str = "alice") -> None:
    db.execute(
        "INSERT INTO inventory (item_key, user_id, quantity) VALUES (?,?,?) "
        "ON CONFLICT(item_key, user_id) DO UPDATE SET quantity = quantity + excluded.quantity",
        (key, user, qty),
    )


def qty(key: str, user: str = "alice") -> int:
    row = db.query_one("SELECT quantity FROM inventory WHERE item_key = ? AND user_id = ?",
                       (key, user))
    return row["quantity"] if row else 0


def use(owned_id: int, key: str, user: dict | None = None, quantity: int = 1) -> dict:
    return shop_api.use_item(owned_id, shop_api.UseItemIn(item_key=key, quantity=quantity), user or alice)


def bag_item(key: str, user: dict | None = None) -> dict:
    items = shop_api.bag(user or alice)["items"]
    return next((it for it in items if it["key"] == key), {})


PER_STAT, TOTAL_MAX = evs.limits()
check("Standardgrenzen: 252 pro Wert, 510 insgesamt",
      (PER_STAT, TOTAL_MAX) == (252, 510), (PER_STAT, TOTAL_MAX))


# ------------------------------------------------------------------ Formel
check("4 EVs sind auf Level 100 einen Punkt wert",
      battle.stat_other(100, 31, 100, 4) == battle.stat_other(100, 31, 100, 0) + 1)
check("3 EVs sind noch nichts wert (abgerundet)",
      battle.stat_other(100, 31, 100, 3) == battle.stat_other(100, 31, 100, 0))
check("252 EVs sind auf Level 100 63 Punkte wert",
      battle.stat_other(100, 31, 100, 252) == battle.stat_other(100, 31, 100, 0) + 63)
check("EV-Bonus gilt auch fuer KP",
      battle.stat_hp(100, 31, 100, 252) == battle.stat_hp(100, 31, 100, 0) + 63)
check("Auf Level 50 zaehlt der EV-Bonus etwa halb (Abrundung)",
      31 <= battle.stat_other(100, 31, 50, 252) - battle.stat_other(100, 31, 50, 0) <= 32,
      battle.stat_other(100, 31, 50, 252) - battle.stat_other(100, 31, 50, 0))
check("Ohne EVs rechnet die Formel wie vorher",
      battle.stat_other(100, 31, 100) == battle.stat_other(100, 31, 100, 0))
check("evs.points rundet auf ganze Punkte ab",
      (evs.points(0), evs.points(3), evs.points(10), evs.points(252)) == (0, 0, 2, 63))


# ----------------------------------------------------------------- Katalog
VITAMINS = {"hp": "hp_up", "atk": "protein", "def": "iron",
            "spa": "calcium", "spd": "zinc", "spe": "carbos"}
WINGS = {"hp": "health_wing", "atk": "muscle_wing", "def": "resist_wing",
         "spa": "genius_wing", "spd": "clever_wing", "spe": "swift_wing"}
BERRIES = {"hp": "pomeg_berry", "atk": "kelpsy_berry", "def": "qualot_berry",
           "spa": "hondew_berry", "spd": "grepa_berry", "spe": "tamato_berry"}
cat = evs.catalog()
check("18 EV-Items im Katalog (6 Vitamine, 6 Federn, 6 Beeren)", len(cat) == 18, sorted(cat))
check("Jeder Statuswert hat Vitamin, Feder und Beere",
      all(key in cat for key in (*VITAMINS.values(), *WINGS.values(), *BERRIES.values())))
check("Vitamine geben +10 EVs auf ihren Wert",
      all(cat[key]["ev_amount"] == 10 and cat[key]["ev_stat"] == stat
          for stat, key in VITAMINS.items()))
check("Federn geben +1 EV auf ihren Wert",
      all(cat[key]["ev_amount"] == 1 and cat[key]["ev_stat"] == stat
          for stat, key in WINGS.items()))
check("Beeren nehmen 10 EVs von ihrem Wert",
      all(cat[key]["ev_amount"] == -10 and cat[key]["ev_stat"] == stat
          for stat, key in BERRIES.items()))
check("EV-Items haben deutsche Labels und Beschreibungen",
      all(item.get("label_de") and item.get("description_de") for item in cat.values()))
check("EV-Items sind kaeuflich mit positivem Preis",
      all(economy.is_buyable(item) and item["price"] > 0 for item in cat.values()))
check("Federn sind billiger als Vitamine",
      cat["health_wing"]["price"] < cat["hp_up"]["price"])
check("Jedes EV-Item hat ein Sprite", all(item.get("sprite_url") for item in cat.values()))
check("is_ev_item erkennt nur EV-Items",
      evs.is_ev_item("hp_up") and evs.is_ev_item("tamato_berry")
      and not evs.is_ev_item("potion") and not evs.is_ev_item(None))

shop_keys = {it["key"]: it for it in shop_api.shop(alice)["items"]}
check("Shop listet EV-Items unter 'vitamin'",
      all(shop_keys.get(key, {}).get("category") == "vitamin" for key in cat))


# ------------------------------------------------------- Regeln (rein)
cc = evs.compute_change
ZERO = {k: 0 for k in evs.STAT_KEYS}
check("Vitamin auf 0 EVs: +10", cc(ZERO, cat["protein"]) == ({**ZERO, "atk": 10}, 10))
check("Feder auf 0 EVs: +1", cc(ZERO, cat["muscle_wing"]) == ({**ZERO, "atk": 1}, 1))
check("Vitamin knapp unter der Grenze gibt nur den Rest",
      cc({**ZERO, "atk": PER_STAT - 3}, cat["protein"]) == ({**ZERO, "atk": PER_STAT}, 3))
check("Vitamin auf ausgereiztem Wert: abgelehnt",
      cc({**ZERO, "atk": PER_STAT}, cat["protein"]) == "evs.error.stat_full")
check("Vitamin mit fast erschoepftem Budget gibt nur den Rest",
      cc({**ZERO, "hp": PER_STAT, "def": TOTAL_MAX - PER_STAT - 4}, cat["protein"])
      == ({"hp": PER_STAT, "atk": 4, "def": TOTAL_MAX - PER_STAT - 4,
           "spa": 0, "spd": 0, "spe": 0}, 4))
check("Vitamin bei erschoepftem Budget: abgelehnt",
      cc({**ZERO, "hp": PER_STAT, "def": PER_STAT, "spa": TOTAL_MAX - 2 * PER_STAT},
         cat["protein"]) == "evs.error.total_full")
check("Beere nimmt 10 EVs weg",
      cc({**ZERO, "atk": 30}, cat["kelpsy_berry"]) == ({**ZERO, "atk": 20}, -10))
check("Beere nimmt hoechstens den vorhandenen Rest",
      cc({**ZERO, "atk": 4}, cat["kelpsy_berry"]) == (ZERO, -4))
check("Beere auf leerem Wert: abgelehnt",
      cc(ZERO, cat["kelpsy_berry"]) == "evs.error.stat_empty")
check("compute_change aendert die uebergebenen EVs nicht",
      (lambda before: (cc(before, cat["protein"]), before == {**ZERO, "atk": 10})[1])(
          {**ZERO, "atk": 10}))
check("evs.total summiert alle sechs Werte",
      evs.total({"hp": 4, "atk": 6, "def": 0, "spa": 0, "spd": 0, "spe": 500}) == 510)


# ---------------------------------------------------------------- Speicher
check("owned_pokemon hat die Spalte evs",
      "evs" in {row["name"] for row in db.query("PRAGMA table_info(owned_pokemon)")})
check("as_evs(None) ist ueberall 0", evs.as_evs(None) == ZERO)
check("as_evs ergaenzt fehlende Werte", evs.as_evs('{"atk": 12}') == {**ZERO, "atk": 12})
check("as_evs nimmt auch ein Dict", evs.as_evs({"spe": 8}) == {**ZERO, "spe": 8})
check("as_evs haelt Werte >= 0", evs.as_evs({"spe": -8}) == ZERO)

no_evs = make_owned("alice", PIKA, 100, None)
check("Altbestand ohne EVs hat NULL in der Spalte", owned(no_evs)["evs"] is None)
base_atk = battle.combatant_from_owned(owned(no_evs)).stat("atk")
db.init_db()  # Migration ist idempotent
check("init_db laesst sich wiederholen", owned(no_evs)["evs"] is None)
check("Combatant ohne EVs rechnet wie vorher",
      battle.combatant_from_owned(owned(no_evs)).stat("atk") == base_atk)


# -------------------------------------------------------------- Anwendung
mon = make_owned("alice", PIKA, 100, 1)
max_before = battle.combatant_from_owned(owned(mon)).max_hp
db.execute("UPDATE owned_pokemon SET current_hp = ? WHERE id = ?", (max_before, mon))
give("hp_up", 2)
result = use(mon, "hp_up")
check("Protein (HP Up) gibt 10 KP-EVs",
      result["gained"] == 10 and evs.as_evs(owned(mon)["evs"])["hp"] == 10, result)
check("Protein wird aus dem Beutel verbraucht", qty("hp_up") == 1)
check("10 KP-EVs heben die max. KP um 2 (Level 100)",
      battle.combatant_from_owned(owned(mon)).max_hp == max_before + 2)
check("Der KP-Zuwachs kommt oben drauf", owned(mon)["current_hp"] == max_before + 2)
check("Antwort enthaelt eine fertige Meldung", bool(result["message"]), result["message"])
check("Antwort meldet das Budget",
      result["total"] == 10 and result["max_total"] == TOTAL_MAX, result)

give("protein", 1)
atk_before = battle.combatant_from_owned(owned(mon)).stat("atk")
use(mon, "protein")
check("Eisen (Protein) hebt den Angriff um 2 Punkte",
      battle.combatant_from_owned(owned(mon)).stat("atk") == atk_before + 2)
check("Nur der trainierte Wert steigt",
      battle.combatant_from_owned(owned(mon)).stat("def")
      == battle.combatant_from_owned(owned(no_evs)).stat("def"))

give("muscle_wing", 1)
use(mon, "muscle_wing")
check("Muskelfeder gibt genau 1 EV", evs.as_evs(owned(mon)["evs"])["atk"] == 11)

give("kelpsy_berry", 1)
lowered = use(mon, "kelpsy_berry")
check("Setangbeere nimmt 10 Angriffs-EVs weg",
      lowered["gained"] == -10 and evs.as_evs(owned(mon)["evs"])["atk"] == 1, lowered)

# ------------------------------------------------- Mehrere auf einmal (quantity)
set_evs(mon, {"atk": 0})
give("protein", 5)
bulk = use(mon, "protein", quantity=3)
check("quantity=3 gibt 3x die EVs des Vitamins",
      bulk["gained"] == 30 and bulk["used"] == 3 and evs.as_evs(owned(mon)["evs"])["atk"] == 30, bulk)
check("quantity=3 verbraucht genau 3 Vitamine", qty("protein") == 2)
check("Meldung bei quantity > 1 nennt die Anzahl", "3" in bulk["message"], bulk["message"])

over_stock = use(mon, "protein", quantity=10)
check("quantity > Bestand wird auf den Bestand gedeckelt",
      over_stock["used"] == 2 and over_stock["gained"] == 20
      and evs.as_evs(owned(mon)["evs"])["atk"] == 50, over_stock)
check("Gedeckelte Menge leert den Bestand genau", qty("protein") == 0)

set_evs(mon, {"atk": PER_STAT - 15})
give("protein", 5)
near_cap = use(mon, "protein", quantity=5)
check("quantity > was noch Wirkung zeigt stoppt an der Grenze (letztes Vitamin gibt nur den Rest)",
      near_cap["used"] == 2 and near_cap["gained"] == 15
      and evs.as_evs(owned(mon)["evs"])["atk"] == PER_STAT, near_cap)
check("Ungenutzte Vitamine bleiben im Beutel", qty("protein") == 3)

set_evs(mon, {"atk": PER_STAT})
give("protein", 2)
before_qty = qty("protein")
expect_http("quantity bei bereits ausgereiztem Wert: 400 (nichts verbraucht)", 400,
            use, mon, "protein", None, 2)
check("Abgelehnte Bulk-Anwendung verbraucht nichts", qty("protein") == before_qty)
set_evs(mon, {"atk": 20})

give("kelpsy_berry", 3)
bulk_berry = use(mon, "kelpsy_berry", quantity=2)
check("quantity gilt auch fuer Beeren (2x -10 Angriffs-EVs)",
      bulk_berry["gained"] == -20 and bulk_berry["used"] == 2
      and evs.as_evs(owned(mon)["evs"])["atk"] == 0, bulk_berry)
check("Beere stoppt, sobald der Wert bei 0 ist (nicht alle 3 verbraucht)", qty("kelpsy_berry") == 1)

# Wirkungslose Anwendungen: Item bleibt im Beutel.
set_evs(mon, {"atk": PER_STAT})
give("protein", 1)
before_qty = qty("protein")
expect_http("Vitamin auf ausgereiztem Wert: 400", 400, use, mon, "protein")
check("Abgelehntes Vitamin bleibt im Beutel", qty("protein") == before_qty)
set_evs(mon, {"atk": 0})
give("kelpsy_berry", 1)
before_qty = qty("kelpsy_berry")
expect_http("Beere auf leerem Wert: 400", 400, use, mon, "kelpsy_berry")
check("Abgelehnte Beere bleibt im Beutel", qty("kelpsy_berry") == before_qty)

# Budget erschoepft.
set_evs(mon, {"hp": PER_STAT, "atk": PER_STAT, "def": TOTAL_MAX - 2 * PER_STAT})
give("zinc", 1)
detail = expect_http("Vitamin bei erschoepftem Budget: 400", 400, use, mon, "zinc")
check("Fehlermeldung nennt das Budget", str(TOTAL_MAX) in detail, detail)
check("Erschoepftes Pokémon steht nicht mehr zur Auswahl",
      mon not in [t["owned_id"] for t in (bag_item("zinc").get("targets") or [])])
set_evs(mon, {"atk": 20})

# Fremdes Pokémon / laufender Kampf.
bob_mon = make_owned("bob", KARP, 20, 1)
give("protein", 1)
expect_http("Fremdes Pokémon: 404", 404, use, bob_mon, "protein")
encounter_id = db.execute(
    "INSERT INTO encounter (species_id, level, shiny, ivs, nature, status, created_at, expires_at) "
    "VALUES (?,?,?,?,?,?,?,?)",
    (KARP["id"], 10, 0, json.dumps(IVS), "hardy", "active", NOW.isoformat(), NOW.isoformat()),
)
session_id = db.execute(
    "INSERT INTO battle_session (encounter_id, user_id, status, state, created_at, updated_at) "
    "VALUES (?,?,?,?,?,?)",
    (encounter_id, "alice", "ongoing", "{}", NOW.isoformat(), NOW.isoformat()),
)
expect_http("Waehrend eines Kampfs: 409", 409, use, mon, "protein")
db.execute("DELETE FROM battle_session WHERE id = ?", (session_id,))
check("Nach dem Kampf wieder erlaubt", use(mon, "protein")["gained"] == 10)


# ------------------------------------------------------------------ Beutel
give("carbos", 3)
entry = bag_item("carbos")
check("Beutel listet EV-Items als Kategorie 'vitamin'", entry.get("category") == "vitamin", entry)
targets = entry.get("targets") or []
check("Beutel-Ziele nennen Wert, EV-Stand und Grenze",
      bool(targets) and all(
          t["stat"] == "spe" and "ev" in t and t["max_per_stat"] == PER_STAT for t in targets),
      targets[:1])
check("Teammitglieder stehen vor Box-Pokémon",
      [t["in_party"] for t in targets] == sorted((t["in_party"] for t in targets), reverse=True),
      [t["in_party"] for t in targets])
set_evs(no_evs, {"spe": PER_STAT})
check("Ausgereizte Pokémon stehen nicht mehr zur Auswahl",
      no_evs not in [t["owned_id"] for t in (bag_item("carbos").get("targets") or [])])
give("tamato_berry", 1)
check("Die Beere bietet genau das ausgereizte Pokémon an",
      no_evs in [t["owned_id"] for t in (bag_item("tamato_berry").get("targets") or [])])
set_evs(no_evs, {})


# ------------------------------------------------------------- Team-Ansicht
detail_view = team_api.get_pokemon(mon, alice)
block = detail_view["evs"]
check("Pokémon-Detail liefert die EVs",
      block["values"]["atk"] == evs.as_evs(owned(mon)["evs"])["atk"], block)
check("Pokémon-Detail liefert Budget und Grenzen",
      block["max_per_stat"] == PER_STAT and block["max_total"] == TOTAL_MAX
      and block["remaining"] == TOTAL_MAX - block["total"], block)
check("Pokémon-Detail liefert den Punktewert je Wert",
      block["points"]["atk"] == evs.points(block["values"]["atk"]), block["points"])
check("Angezeigte Werte enthalten den EV-Bonus",
      detail_view["stats"]["atk"] == battle.combatant_from_owned(owned(mon)).stat("atk"))


# ---------------------------------------------------------------- Im Kampf
trained = make_owned("alice", PIKA, 100, 2)
set_evs(trained, {"atk": PER_STAT})
check("Trainiertes Pokémon schlaegt untrainiertes um 63 Punkte Angriff",
      battle.combatant_from_owned(owned(trained)).stat("atk") == base_atk + 63)
check("effective_stat rechnet mit den EVs",
      battle.effective_stat(battle.combatant_from_owned(owned(trained)), "atk")
      == base_atk + 63)
set_evs(trained, {"hp": PER_STAT})
hp_trained = battle.combatant_from_owned(owned(trained)).max_hp
check("KP-EVs heben auch die max. KP im Kampf",
      hp_trained == battle.combatant_from_owned(owned(no_evs)).max_hp + 63)
check("healing.max_hp_of kennt die EVs", healing.max_hp_of(owned(trained)) == hp_trained)
db.execute("UPDATE owned_pokemon SET current_hp = 1 WHERE id = ?", (trained,))
db.execute("UPDATE app_user SET last_team_heal_at = NULL WHERE id = 'alice'")
healing.heal_team("alice")
check("Team heilen fuellt bis zum EV-Maximum", owned(trained)["current_hp"] == hp_trained)

snapshot = {m["species_id"]: m for m in pvp.snapshot_for_user("alice")}
check("PvP-Abbild enthaelt die EVs",
      snapshot[PIKA["id"]]["evs"]["hp"] in (PER_STAT, evs.as_evs(owned(mon)["evs"])["hp"]),
      snapshot.get(PIKA["id"], {}).get("evs"))
copy = pvp._combatant({"species_id": PIKA["id"], "level": 100, "ivs": IVS,
                        "evs": {"atk": PER_STAT}, "moves": ["tackle"]}, "Alice")
check("PvP-Kopie kaempft mit den EVs", copy.stat("atk") == base_atk + 63)
check("Team-Code-Import kaempft ohne EVs (keine EVs im Code)",
      pvp._combatant({"species_id": PIKA["id"], "level": 100, "ivs": IVS,
                      "moves": ["tackle"]}, "Code").stat("atk") == base_atk)


# ----------------------------------------------------------- Einstellungen
expect_http("PATCH: Grenze pro Wert > Gesamtgrenze wird abgelehnt", 400,
            config_routes.patch_settings, {"ev_max_per_stat": 600, "ev_max_total": 510})
expect_http("PATCH: negative Grenze wird abgelehnt", 400,
            config_routes.patch_settings, {"ev_max_total": -1})
expect_http("PATCH: Kommazahl wird abgelehnt", 400,
            config_routes.patch_settings, {"ev_max_per_stat": 100.5})
config_routes.patch_settings({"ev_max_per_stat": 100, "ev_max_total": 300})
check("Gesenkte Grenze gilt sofort", evs.limits() == (100, 300), evs.limits())
check("Vitamin respektiert die gesenkte Grenze pro Wert",
      cc({**ZERO, "atk": 95}, cat["protein"]) == ({**ZERO, "atk": 100}, 5))
check("Bereits trainierte EVs bleiben ueber der gesenkten Grenze erhalten",
      evs.as_evs(owned(trained)["evs"])["hp"] == PER_STAT)
config_routes.patch_settings({"ev_max_per_stat": PER_STAT, "ev_max_total": TOTAL_MAX})
check("Grenzen wieder auf Standard", evs.limits() == (PER_STAT, TOTAL_MAX))


# -------------------------------------------------------------------- i18n
for key in ["item_category.vitamin", "evs.title", "evs.hint", "evs.total",
            "evs.bag.none_can_use", "evs.bar_title", "evs.result.raised",
            "evs.result.lowered", "evs.error.stat_full", "evs.error.total_full",
            "evs.error.stat_empty", "evs.error.in_battle", "guide.evs.title",
            "guide.evs.p1", "settings.ev_max_per_stat", "settings.ev_max_total"]:
    en_text, de_text = i18n.catalog("en").get(key), i18n.catalog("de").get(key)
    check(f"i18n en+de: {key}", bool(en_text) and bool(de_text) and en_text != de_text)


# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
