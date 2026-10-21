"""Tests fuer Geld, Shop, Entwicklungs-Items und IV-Zuwachs.

Aufruf:  python3 tests/test_economy.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-economy-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, db, economy, evolution, held_items, seed, spawn  # noqa: E402
from app.api import encounters as encounters_api  # noqa: E402
from app.api import shop as shop_api  # noqa: E402
from app.api import users as users_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def expect_http(label: str, status: int, fn, *args) -> None:
    try:
        fn(*args)
        check(label, False, "keine Exception")
    except HTTPException as exc:
        check(label, exc.status_code == status, f"{exc.status_code}: {exc.detail}")


NOW = datetime.now(timezone.utc).isoformat()
IVS = {k: 10 for k in spawn.STAT_KEYS}

# ------------------------------------------------------------ Migration
# Alte DB ohne `money`-Spalte: Spieler mit Starter bekommen einmalig Startgeld.
old = sqlite3.connect(":memory:")
old.row_factory = db._dict_factory
old.executescript(
    """
    CREATE TABLE setting (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE app_user (id TEXT PRIMARY KEY, name TEXT NOT NULL, display_name TEXT,
        starter_species_id INTEGER, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);
    INSERT INTO app_user VALUES ('veteran', 'V', 'V', 4, 'x', 'x');
    INSERT INTO app_user VALUES ('legacy', 'legacy', 'L', NULL, 'x', 'x');
    """
)
db._migrate_economy(old)
db._migrate_economy(old)  # idempotent
money = {r["id"]: r["money"] for r in old.execute("SELECT id, money FROM app_user")}
check("Migration: money-Spalte angelegt", "money" in db._table_columns(old, "app_user"))
check("Migration: Bestandsspieler bekommt Startgeld einmalig",
      money == {"veteran": db.DEFAULT_SETTINGS["starting_money"], "legacy": 0}, str(money))

# ---------------------------------------------------------------- Setup
db.init_db()
db.init_db()  # zweiter Start darf nichts kaputt machen
seed.seed_from_fallback()
check("Neue Settings gesetzt", db.get_setting("money_per_level") == 25)
db.set_setting("iv_repeat_catch_bonus", 2)  # Alt-DB mit abgeloester Einstellung
db.init_db()
check("Abgeloestes iv_repeat_catch_bonus wird entfernt",
      "iv_repeat_catch_bonus" not in db.all_settings()
      and db.get_setting("iv_repeat_catch_min_gain") == 1
      and db.get_setting("iv_repeat_catch_max_gain") == 2)
check("money-Spalte auf frischer DB", "money" in db._table_columns(db.connect(), "app_user"))

# Ein paar Arten, die der Fallback-Pokédex nicht hat: Raichu (Ziel des
# Donnersteins), Pichu (Baby von Pikachu) und ein Legendaeres.
for rec in (
    {"id": 26, "name": "raichu", "name_de": "Raichu", "type1": "electric", "catch_rate": 75,
     "base_exp": 243, "growth_rate": "medium_fast", "base_hp": 60, "base_atk": 90,
     "base_def": 55, "base_spa": 90, "base_spd": 80, "base_spe": 110, "evolves_from": 25},
    {"id": 172, "name": "pichu", "name_de": "Pichu", "type1": "electric", "catch_rate": 190,
     "base_exp": 41, "growth_rate": "medium_fast", "base_hp": 20, "base_atk": 40,
     "base_def": 15, "base_spa": 35, "base_spd": 35, "base_spe": 60},
    {"id": 150, "name": "mewtwo", "type1": "psychic", "catch_rate": 3, "base_exp": 340,
     "growth_rate": "slow", "base_hp": 106, "base_atk": 110, "base_def": 90, "base_spa": 154,
     "base_spd": 90, "base_spe": 130, "is_legendary": 1},
):
    rec.setdefault("generation", seed.generation_for(rec["id"]))
    seed._upsert(rec)
db.execute("UPDATE species SET evolves_from = 172 WHERE id = 25")

pika = db.query_one("SELECT * FROM species WHERE id = 25")
karp = db.query_one("SELECT * FROM species WHERE id = 129")
mewtwo = db.query_one("SELECT * FROM species WHERE id = 150")

# ------------------------------------------------------------ Belohnung
cfg = economy.economy_settings()
r_low = economy.reward_amount(karp, 5, cfg=cfg)
r_high = economy.reward_amount(karp, 30, cfg=cfg)
check("Belohnung steigt mit Level", r_high > r_low, f"{r_low} -> {r_high}")
check("Seltenere Art bringt mehr",
      economy.reward_amount(pika, 15, cfg=cfg) > economy.reward_amount(karp, 15, cfg=cfg))
check("Legendaer bringt deutlich mehr",
      economy.reward_amount(mewtwo, 15, cfg=cfg) > 2 * economy.reward_amount(pika, 15, cfg=cfg))
check("Shiny bringt mehr", economy.reward_amount(karp, 15, shiny=True, cfg=cfg)
      > economy.reward_amount(karp, 15, cfg=cfg))
check("Kampfsieg bringt weniger als Fang (Default)",
      economy.reward_amount(pika, 15, kind="battle", cfg=cfg)
      < economy.reward_amount(pika, 15, kind="catch", cfg=cfg))
check("Belohnung auf 10 gerundet", r_low % 10 == 0)

# Balancing-Ziel (Stand 2026-09-27): Entwicklungs-Items sind ein Geldsenke fuers
# Endgame, Fixpreis 100000 - kein frueher Kauf mehr im Vorbeigehen. Level-Verteilung
# wie spawn.roll_level (2..30, Neigung nach unten) -> Mittel ~13.
mean_level = 2 + 28 / 2.4
avg = economy.expected_reward(round(mean_level))
stone = economy.unit_price(economy.catalog_by_key()["fire_stone"])
check("Entwicklungs-Item kostet 100k", stone == 100_000, str(stone))
ratio = stone / avg
check("Entwicklungs-Item kostet grob 100-200 Durchschnitts-Encounter (Fallback-Dex)",
      100 <= ratio <= 200,
      f"Ø {avg:.0f} ₽ bei Lv. {mean_level:.0f}, Stein {stone} ₽ -> {ratio:.1f}")

# Getragene Items sind ebenfalls eine Endgame-Geldsenke, Fixpreis 50000 - ausser
# nicht-kaeufliche Erfolgs-Belohnungen wie den Erfahrungsteiler (Preis 0, wie
# der Meisterball bei den Baellen). Ausnahme: das Muenzamulett (150000, verdoppelt Preisgeld).
held_prices = {k: economy.unit_price(v) for k, v in economy.catalog_by_key().items()
               if v["category"] == "held" and economy.is_buyable(v) and k != "amulet_coin"}
check("Getragene Items kosten alle 50k", held_prices and set(held_prices.values()) == {50_000},
      str(held_prices))
check("Muenzamulett kostet 150k", economy.unit_price(economy.catalog_by_key()["amulet_coin"]) == 150_000)
check("Erfahrungsteiler ist nicht kaeuflich",
      not economy.is_buyable(economy.catalog_by_key()["exp_share"]))

# --------------------------------------------------------------- Starter
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")
check("Neuer Nutzer startet ohne Geld", economy.balance("alice") == 0)
users_api.choose_starter(users_api.StarterChoice(species_id=4), alice)
users_api.choose_starter(users_api.StarterChoice(species_id=7), bob)
check("Starter-Wahl gibt Startgeld", economy.balance("alice") == 1000)

# ------------------------------------------------------------------ Shop
catalog = economy.catalog_by_key()
check("Katalog hat Kategorien", all(it["category"] for it in catalog.values()))
all_keys = [it["key"] for it in economy.catalog()]
check("Katalog-Keys sind eindeutig (sonst gewinnt im Dict zufaellig ein Eintrag "
      "mit falschem Preis/falscher Kategorie - Bug: doppelter Metallmantel-Eintrag)",
      len(all_keys) == len(set(all_keys)),
      str([k for k in set(all_keys) if all_keys.count(k) > 1]))
# Regression: Metallmantel war als zwei Shop-Eintraege modelliert (Entwicklungs-Item
# 100k + getragenes Item 50k, gleicher Key) - im Katalog-Dict gewann immer der
# getragene Eintrag, also falscher Preis beim Kauf und falsche Kategorie im Beutel.
# Es ist in echten Spielen ohnehin ein einziges Item mit beiden Effekten.
check("Metallmantel: ein Entwicklungs-Item-Eintrag zu 100k",
      catalog["metal_coat"]["category"] == "evolution"
      and economy.unit_price(catalog["metal_coat"]) == 100_000,
      str(catalog["metal_coat"]))
check("Metallmantel bleibt zusaetzlich als getragenes Item ausruestbar (Stahl-Boost)",
      held_items.effect_of("metal_coat") == "type_boost")
shop = shop_api.shop(alice)
keys = {it["key"] for it in shop["items"]}
check("Shop verkauft Baelle und Steine", {"poke_ball", "net_ball", "fire_stone",
                                          "linking_cord"} <= keys)
check("Meisterball ist nicht im Shop", "master_ball" not in keys)

before_balls = db.query_one(
    "SELECT quantity FROM inventory WHERE user_id='alice' AND item_key='poke_ball'")["quantity"]
res = shop_api.buy(shop_api.PurchaseIn(item_key="poke_ball", quantity=3), alice)
check("Kauf bucht Geld ab", res["money"] == 1000 - 3 * catalog["poke_ball"]["price"],
      str(res))
check("Kauf landet im Inventar", res["owned"] == before_balls + 3)

expect_http("Meisterball-Kauf abgelehnt", 400, shop_api.buy,
            shop_api.PurchaseIn(item_key="master_ball"), alice)
expect_http("Unbekannter Artikel abgelehnt", 400, shop_api.buy,
            shop_api.PurchaseIn(item_key="rare_candy"), alice)
expect_http("Menge 0 abgelehnt", 400, shop_api.buy,
            shop_api.PurchaseIn(item_key="poke_ball", quantity=0), alice)
expect_http("Negative Menge abgelehnt", 400, shop_api.buy,
            shop_api.PurchaseIn(item_key="poke_ball", quantity=-5), alice)
money_before = economy.balance("alice")
expect_http("Zu wenig Geld -> 409", 409, shop_api.buy,
            shop_api.PurchaseIn(item_key="fire_stone", quantity=10), alice)
check("Fehlkauf aendert nichts (Geld)", economy.balance("alice") == money_before)
check("Fehlkauf aendert nichts (Inventar)", db.query_one(
    "SELECT 1 AS x FROM inventory WHERE user_id='alice' AND item_key='fire_stone'") is None)

db.set_setting("shop_price_multiplier", 0.5)
check("Preis-Multiplikator wirkt",
      economy.unit_price(catalog["fire_stone"]) == catalog["fire_stone"]["price"] // 2)
db.set_setting("shop_price_multiplier", 1.0)

# ------------------------------------------------------ Entwicklungsdaten
eevee_chain = {"chain": {
    "species": {"name": "eevee", "url": "https://pokeapi.co/api/v2/pokemon-species/133/"},
    "evolution_details": [],
    "evolves_to": [
        {"species": {"name": "vaporeon", "url": "https://pokeapi.co/api/v2/pokemon-species/134/"},
         "evolves_to": [], "evolution_details": [
             {"item": {"name": "water-stone"}, "trigger": {"name": "use-item"},
              "required_pokemon_form": {"name": "eevee"}},
             {"item": {"name": "water-stone"}, "trigger": {"name": "use-item"}}]},
        {"species": {"name": "espeon", "url": "https://pokeapi.co/api/v2/pokemon-species/196/"},
         "evolves_to": [], "evolution_details": [
             {"trigger": {"name": "level-up"}, "min_happiness": 160, "time_of_day": "day"}]},
    ]}}
rows = evolution.rows_from_chain(eevee_chain)
check("Kette: Duplikate zusammengefasst", len(rows) == 2, str(rows))
check("Kette: Stein-Item gemappt",
      any(r["to"] == 134 and r["item_key"] == "water_stone" and r["trigger"] == "use-item"
          for r in rows))

trade_chain = {"chain": {
    "species": {"name": "abra", "url": "https://pokeapi.co/api/v2/pokemon-species/63/"},
    "evolution_details": [],
    "evolves_to": [{
        "species": {"name": "kadabra", "url": "https://pokeapi.co/api/v2/pokemon-species/64/"},
        "evolution_details": [{"trigger": {"name": "level-up"}, "min_level": 16}],
        "evolves_to": [
            {"species": {"name": "alakazam", "url": "https://pokeapi.co/api/v2/pokemon-species/65/"},
             "evolves_to": [], "evolution_details": [{"trigger": {"name": "trade"}}]}]}]}}
rows = evolution.rows_from_chain(trade_chain)
check("Kette: Tausch -> Verbindungsschnur",
      any(r["from"] == 64 and r["to"] == 65 and r["item_key"] == "linking_cord" for r in rows))
check("Kette: Level-Entwicklung mit min_level",
      any(r["from"] == 63 and r["min_level"] == 16 and r["item_key"] == "" for r in rows))

vulpix_chain = {"chain": {
    "species": {"name": "vulpix", "url": "https://pokeapi.co/api/v2/pokemon-species/37/"},
    "evolution_details": [],
    "evolves_to": [{"species": {"name": "ninetales",
                                "url": "https://pokeapi.co/api/v2/pokemon-species/38/"},
                    "evolves_to": [], "evolution_details": [
                        {"item": {"name": "fire-stone"}, "trigger": {"name": "use-item"}},
                        {"item": {"name": "ice-stone"}, "trigger": {"name": "use-item"},
                         "required_pokemon_form": {"name": "vulpix-alola"}}]}]}}
rows = evolution.rows_from_chain(vulpix_chain)
check("Kette: Regionalformen uebersprungen", [r["item_key"] for r in rows] == ["fire_stone"])

# Current method wins (is_default): Magneton -> Magnezone is the Thunder Stone now,
# not "level up at a magnetic field" as well.
D = "https://pokeapi.co/api/v2/pokemon-species/"
magnet_chain = {"chain": {
    "species": {"name": "magneton", "url": D + "82/"}, "evolution_details": [],
    "evolves_to": [{"species": {"name": "magnezone", "url": D + "462/"}, "evolves_to": [],
                    "evolution_details": [
                        {"trigger": {"name": "level-up"}, "location": {"name": "mt-coronet"}},
                        {"trigger": {"name": "use-item"}, "item": {"name": "thunder-stone"},
                         "is_default": True}]}]}}
rows = evolution.rows_from_chain(magnet_chain)
check("Kette: nur aktuelle Methode (Stein statt Level)",
      [(r["trigger"], r["item_key"]) for r in rows] == [("use-item", "thunder_stone")], str(rows))

# Friendship -> fixed level (base form earlier than an already evolved one).
def _friend_chain(with_pre: bool) -> dict:
    friend = {"species": {"name": "b", "url": D + "2/"}, "evolves_to": [],
              "evolution_details": [{"trigger": {"name": "level-up"}, "min_happiness": 220,
                                     "is_default": True}]}
    mid = {"species": {"name": "a", "url": D + "1/"}, "evolves_to": [friend],
           "evolution_details": [{"trigger": {"name": "level-up"}, "min_level": 10}]}
    return {"chain": {"species": {"name": "z", "url": D + "0/"}, "evolution_details": [],
                      "evolves_to": [mid] if with_pre else [friend]}}
base_rows = evolution.rows_from_chain(_friend_chain(False))
check("Freundschaft: Basisform -> festes Level",
      base_rows[0]["min_level"] == evolution.FRIENDSHIP_LEVEL_BASE
      and base_rows[0]["detail"].get("friendship") is True, str(base_rows))
mid_rows = [r for r in evolution.rows_from_chain(_friend_chain(True)) if r["to"] == 2]
check("Freundschaft: Entwickelte Form -> hoeheres Level",
      mid_rows[0]["min_level"] == evolution.FRIENDSHIP_LEVEL_EVOLVED, str(mid_rows))

n = evolution.seed_from_fallback()
n2 = evolution.seed_from_fallback()
count = db.query_one("SELECT COUNT(*) AS n FROM evolution")["n"]
check("Fallback-Entwicklungen geladen + idempotent", n > 40 and count == n, f"{count}")
check("Fallback: Pikachu + Donnerstein -> Raichu", db.query_one(
    "SELECT 1 AS x FROM evolution WHERE from_species_id=25 AND to_species_id=26 "
    "AND item_key='thunder_stone'") is not None)
# PokeAPI-Daten ueberschreiben Fallback-Zeilen (source), ohne Duplikate.
evolution._write_rows([{"from": 25, "to": 26, "trigger": "use-item",
                        "item_key": "thunder_stone"}], "pokeapi", overwrite=True)
check("PokeAPI-Zeile ersetzt Fallback-Zeile", db.query_one(
    "SELECT source FROM evolution WHERE from_species_id=25 AND to_species_id=26")["source"]
      == "pokeapi" and db.query_one("SELECT COUNT(*) AS n FROM evolution")["n"] == count)
check("Ohne seed_meta gilt PokeAPI-Seeding als offen", not evolution.pokeapi_seeded())

# ensure_seeded darf ohne Netz nicht werfen.
async def _offline() -> bool:
    return False
seed_reachable = seed.reachable
seed.reachable = _offline
asyncio.run(evolution.ensure_seeded())
seed.reachable = seed_reachable
check("ensure_seeded ohne PokeAPI laeuft durch", True)


# ---------------------------------------------------- Item anwenden
def add_owned(user_id: str, species_id: int, ivs: dict, level: int = 20,
              gender: str = "male", hp: int | None = None) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "gender, current_hp, caught_at) VALUES (?,?,?,?,0,?,?,?,?,?)",
        (user_id, species_id, level, battle.exp_for_level(level), json.dumps(ivs), "hardy",
         gender, hp, NOW),
    )


sp = db.query_one("SELECT * FROM species WHERE id = 25")
pika_max = battle.stat_hp(sp["base_hp"], 10, 20)
pika_id = add_owned("alice", 25, IVS, hp=pika_max // 2)

bag = shop_api.bag(alice)
check("Beutel ohne Stein zeigt keinen Stein", all(i["key"] != "thunder_stone" for i in bag["items"]))
economy.credit("alice", 105000)  # "erspielt"
shop_api.buy(shop_api.PurchaseIn(item_key="thunder_stone"), alice)
bag = shop_api.bag(alice)
stone_entry = next((i for i in bag["items"] if i["key"] == "thunder_stone"), None)
check("Beutel zeigt Donnerstein mit Pikachu als Ziel",
      stone_entry is not None and any(t["owned_id"] == pika_id and t["to_species_id"] == 26
                                      for t in stone_entry["targets"]), str(stone_entry))

expect_http("Fremdes Pokémon -> 404", 404, shop_api.use_item, pika_id,
            shop_api.UseItemIn(item_key="thunder_stone"), bob)
expect_http("Falsches Item -> 400", 400, shop_api.use_item, pika_id,
            shop_api.UseItemIn(item_key="fire_stone"), alice)
expect_http("Ball ist kein Entwicklungs-Item -> 400", 400, shop_api.use_item, pika_id,
            shop_api.UseItemIn(item_key="poke_ball"), alice)
bob_pika = add_owned("bob", 25, IVS)
expect_http("Ohne Item im Beutel -> 409", 409, shop_api.use_item, bob_pika,
            shop_api.UseItemIn(item_key="thunder_stone"), bob)

evo = shop_api.use_item(pika_id, shop_api.UseItemIn(item_key="thunder_stone"), alice)
row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (pika_id,))
raichu = db.query_one("SELECT * FROM species WHERE id = 26")
raichu_max = battle.stat_hp(raichu["base_hp"], 10, 20)
check("Entwicklung: Art gewechselt", row["species_id"] == 26 and evo["to"]["id"] == 26)
check("Entwicklung: KP-Anteil bleibt",
      abs(row["current_hp"] / raichu_max - (pika_max // 2) / pika_max) < 0.05,
      f"{pika_max // 2}/{pika_max} -> {row['current_hp']}/{raichu_max}")
check("Entwicklung: Stein verbraucht", db.query_one(
    "SELECT quantity FROM inventory WHERE user_id='alice' AND item_key='thunder_stone'"
)["quantity"] == 0)
check("Entwicklung: Pokédex-Eintrag gefangen", battle.species_is_known(26, "alice"))
expect_http("Zweite Anwendung ohne Stein scheitert", 400, shop_api.use_item, pika_id,
            shop_api.UseItemIn(item_key="thunder_stone"), alice)

# Geschlechts-Bedingung: Leuchtstein nur fuer maennliche Kirlia.
for rec in ({"id": 281, "name": "kirlia", "type1": "psychic", "catch_rate": 120, "base_exp": 97,
             "growth_rate": "slow", "base_hp": 38, "base_atk": 35, "base_def": 35,
             "base_spa": 65, "base_spd": 55, "base_spe": 50},
            {"id": 475, "name": "gallade", "type1": "psychic", "catch_rate": 45, "base_exp": 259,
             "growth_rate": "slow", "base_hp": 68, "base_atk": 125, "base_def": 65,
             "base_spa": 65, "base_spd": 115, "base_spe": 80, "evolves_from": 281}):
    rec["generation"] = seed.generation_for(rec["id"])
    seed._upsert(rec)
kirlia_f = add_owned("alice", 281, IVS, gender="female")
db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES ('dawn_stone','alice',1)")
expect_http("Leuchtstein wirkt nicht auf weibliche Kirlia", 400, shop_api.use_item, kirlia_f,
            shop_api.UseItemIn(item_key="dawn_stone"), alice)
kirlia_m = add_owned("alice", 281, IVS, gender="male")
check("Leuchtstein wirkt auf maennliche Kirlia", shop_api.use_item(
    kirlia_m, shop_api.UseItemIn(item_key="dawn_stone"), alice)["to"]["id"] == 475)

# ---------------------------------------------------- Fang: Geld + IVs
check("Familie ueber evolves_from", economy.evolution_family(25) == {172, 25, 26},
      str(economy.evolution_family(25)))


def make_encounter(species_id: int, level: int, ivs: dict) -> int:
    # current_hp gesetzt = "wurde schon bekaempft", damit auch neue Arten fangbar sind.
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    return db.execute(
        "INSERT INTO encounter (species_id, level, shiny, ivs, nature, gender, status, "
        "current_hp, created_at, expires_at) VALUES (?,?,0,?,'hardy','male','active',5,?,?)",
        (species_id, level, json.dumps(ivs), NOW, expires),
    )


db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES ('master_ball','alice',5)")
# Alice besitzt (seit der Entwicklung) ein Raichu mit IVs 10 (Initiative 30).
# Sie faengt ein Pichu - gleiche Familie - mit durchweg besseren IVs. Die IVs
# des Fangs spielen keine Rolle mehr: genau EIN zufaelliger Wert steigt um 1-2.
new_ivs = {"hp": 31, "atk": 5, "def": 12, "spa": 12, "spd": 12, "spe": 12}
before_ivs = {**IVS, "spe": 30}
db.execute("UPDATE owned_pokemon SET ivs = ? WHERE id = ?", (json.dumps(before_ivs), pika_id))
check("Default: IV-Zuwachs 1-2", economy.repeat_catch_gain_range() == (1, 2),
      str(economy.repeat_catch_gain_range()))
enc_id = make_encounter(172, 12, new_ivs)
money_before = economy.balance("alice")
res = encounters_api.throw(enc_id, encounters_api.BallThrow(ball="master_ball"), alice)
check("Fang mit Meisterball", res["caught"] is True)
check("Fang bringt Geld", res["money_earned"] > 0
      and economy.balance("alice") == money_before + res["money_earned"]
      and res["money"] == economy.balance("alice"), f"+{res['money_earned']}")
gain = next((g for g in res["iv_gains"] if g["owned_id"] == pika_id), None)
changes = (gain or {}).get("changes", [])
check("IV: genau ein Wert steigt", len(changes) == 1, str(changes))
change = changes[0] if changes else {"stat": "hp", "from": 0, "to": 0}
check("IV: Zuwachs 1-2 Punkte (kein Max-Merge mit dem Fang)",
      change["from"] == before_ivs[change["stat"]]
      and 1 <= change["to"] - change["from"] <= 2 and change["to"] <= 31, str(change))
stored = json.loads(db.query_one("SELECT ivs FROM owned_pokemon WHERE id = ?", (pika_id,))["ivs"])
check("IV: persistiert, Rest unveraendert",
      stored == {**before_ivs, change["stat"]: change["to"]}, str(stored))
new_row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (res["owned_id"],))
check("Neuer Fang normal hinzugefuegt, IVs unveraendert",
      new_row["species_id"] == 172 and json.loads(new_row["ivs"]) == new_ivs)
check("Andere Familien bleiben unberuehrt",
      all(g["species_id"] in {172, 25, 26} for g in res["iv_gains"]))
check("Jedes Familienmitglied: genau ein Wert",
      all(len(g["changes"]) == 1 for g in res["iv_gains"]))
check("Bob bekommt keinen IV-Zuwachs",
      json.loads(db.query_one("SELECT ivs FROM owned_pokemon WHERE id = ?", (bob_pika,))["ivs"])
      == IVS)

# Wuerfel-Logik direkt: Deckel bei 31, nur nicht-maximale Werte, alles 31 -> nichts.
import random  # noqa: E402

rng = random.Random(1234)
seen_stats, seen_amounts = set(), set()
for _ in range(300):
    key, before, after = economy.roll_repeat_catch_gain(IVS, 1, 2, rng)
    seen_stats.add(key)
    seen_amounts.add(after - before)
check("Wuerfel: alle Werte moeglich", seen_stats == set(economy.IV_KEYS), str(seen_stats))
check("Wuerfel: Zuwachs genau 1 oder 2", seen_amounts == {1, 2}, str(seen_amounts))
almost = {k: 31 for k in economy.IV_KEYS} | {"spd": 30}
rolls = {economy.roll_repeat_catch_gain(almost, 1, 2, rng) for _ in range(50)}
check("Wuerfel: nur der nicht-maximale Wert, Deckel bei 31",
      rolls == {("spd", 30, 31)}, str(rolls))
check("Wuerfel: alles 31 -> kein Zuwachs",
      economy.roll_repeat_catch_gain({k: 31 for k in economy.IV_KEYS}, 1, 2, rng) is None)

# Familienmitglied mit perfekten IVs bekommt keinen Eintrag.
db.execute("UPDATE owned_pokemon SET ivs = ? WHERE user_id = 'alice' AND species_id IN (172, 25, 26)",
           (json.dumps({k: 31 for k in spawn.STAT_KEYS}),))
enc_id = make_encounter(25, 10, IVS)
res = encounters_api.throw(enc_id, encounters_api.BallThrow(ball="master_ball"), alice)
check("Alle IVs 31: kein Zuwachs", res["caught"] and res["iv_gains"] == [], str(res["iv_gains"]))

# Erstfang einer Art ohne Familie im Besitz: Geld ja, IVs nein.
enc_id = make_encounter(129, 10, IVS)
res = encounters_api.throw(enc_id, encounters_api.BallThrow(ball="master_ball"), alice)
check("Erstfang: kein IV-Zuwachs", res["caught"] and res["iv_gains"] == [])

# Einstellbarer Bereich (+ Absicherung gegen Unsinn in der DB).
db.set_setting("iv_repeat_catch_min_gain", 3)
db.set_setting("iv_repeat_catch_max_gain", 3)
db.execute("UPDATE owned_pokemon SET ivs = ? WHERE id = ?", (json.dumps(IVS), pika_id))
db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES ('master_ball','alice',5) "
           "ON CONFLICT(item_key, user_id) DO UPDATE SET quantity = quantity + 5")
enc_id = make_encounter(25, 10, IVS)
res = encounters_api.throw(enc_id, encounters_api.BallThrow(ball="master_ball"), alice)
gain = next((g for g in res["iv_gains"] if g["owned_id"] == pika_id), None)
check("Einstellung 3-3: genau +3 auf einen Wert",
      gain is not None and len(gain["changes"]) == 1
      and gain["changes"][0]["to"] - gain["changes"][0]["from"] == 3, str(gain))
db.set_setting("iv_repeat_catch_min_gain", 5)
db.set_setting("iv_repeat_catch_max_gain", 2)
check("Kaputter Bereich (min > max) wird geklemmt", economy.repeat_catch_gain_range() == (5, 5))
db.set_setting("iv_repeat_catch_min_gain", 1)
db.set_setting("iv_repeat_catch_max_gain", 2)

# Einstellungs-Validierung im Admin-PATCH.
from app.api import config_routes  # noqa: E402

expect_http("PATCH min > max wird abgelehnt", 400, config_routes.patch_settings,
            {"iv_repeat_catch_min_gain": 3, "iv_repeat_catch_max_gain": 2})
expect_http("PATCH min > gespeichertes max wird abgelehnt", 400, config_routes.patch_settings,
            {"iv_repeat_catch_min_gain": 3})
expect_http("PATCH min 0 wird abgelehnt", 400, config_routes.patch_settings,
            {"iv_repeat_catch_min_gain": 0})
expect_http("PATCH max 32 wird abgelehnt", 400, config_routes.patch_settings,
            {"iv_repeat_catch_max_gain": 32})
expect_http("PATCH Kommazahl wird abgelehnt", 400, config_routes.patch_settings,
            {"iv_repeat_catch_max_gain": 1.5})
expect_http("PATCH altes iv_repeat_catch_bonus ist unbekannt", 400,
            config_routes.patch_settings, {"iv_repeat_catch_bonus": 2})
config_routes.patch_settings({"iv_repeat_catch_min_gain": 2, "iv_repeat_catch_max_gain": 4})
check("Gueltiger PATCH gespeichert", economy.repeat_catch_gain_range() == (2, 4))
db.set_setting("iv_repeat_catch_min_gain", 1)
db.set_setting("iv_repeat_catch_max_gain", 2)

# Feature abschaltbar.
db.set_setting("iv_repeat_catch_enabled", False)
enc_id = make_encounter(25, 10, IVS)
res = encounters_api.throw(enc_id, encounters_api.BallThrow(ball="master_ball"), alice)
check("IV-Zuwachs abschaltbar", res["iv_gains"] == [])
check("IV-Untergrenze abschaltbar", res["iv_floor"] is None)
db.set_setting("iv_repeat_catch_enabled", True)

# ------------------------------------------------- IV-Untergrenze pro Art
check("Spalte species.min_ivs existiert",
      "min_ivs" in {r["name"] for r in db.query("PRAGMA table_info(species)")})
db.execute("UPDATE species SET min_ivs = NULL WHERE id = 25")
check("Untergrenze startet bei 5",
      economy.species_min_ivs(25) == {k: 5 for k in spawn.STAT_KEYS},
      str(economy.species_min_ivs(25)))
# Zufaellige Spawns einer frischen Art: nie unter 5, darueber weiterhin zufaellig.
fresh = [spawn.roll_ivs(25) for _ in range(300)]
check("Frische Art: Spawn-IVs nie unter 5",
      all(min(r.values()) >= 5 for r in fresh))
check("Frische Art: Spawn-IVs weiterhin zufaellig",
      all(len({r[k] for r in fresh}) > 10 for k in spawn.STAT_KEYS))
# Alte, niedrigere Werte in der DB (Startwert war frueher 1) werden angehoben.
db.execute("UPDATE species SET min_ivs = ? WHERE id = 25",
           (json.dumps({"hp": 1, "atk": 3, "def": 7, "spa": 0, "spd": 5, "spe": 12}),))
check("Gespeicherte Werte unter 5 werden auf 5 angehoben",
      economy.species_min_ivs(25) == {"hp": 5, "atk": 5, "def": 7, "spa": 5, "spd": 5, "spe": 12},
      str(economy.species_min_ivs(25)))
db.execute("UPDATE species SET min_ivs = NULL WHERE id = 25")
# Fang eines vor der Untergrenze gewuerfelten Encounters (IVs 0-3): auf 5 geklemmt.
low_enc = make_encounter(25, 10, {"hp": 0, "atk": 3, "def": 5, "spa": 9, "spd": 1, "spe": 31})
low_res = encounters_api.throw(low_enc, encounters_api.BallThrow(ball="master_ball"), alice)
low_ivs = json.loads(db.query_one("SELECT ivs FROM owned_pokemon WHERE id = ?",
                                  (low_res["owned_id"],))["ivs"])
check("Fang klemmt niedrige IVs auf die Untergrenze",
      low_ivs == {"hp": 5, "atk": 5, "def": 5, "spa": 9, "spd": 5, "spe": 31}, str(low_ivs))
db.execute("UPDATE species SET min_ivs = NULL WHERE id = 25")
other_before = economy.species_min_ivs(129)
enc_id = make_encounter(25, 10, IVS)
res = encounters_api.throw(enc_id, encounters_api.BallThrow(ball="master_ball"), alice)
floor = res["iv_floor"]
check("Fang hebt Untergrenze der Art", floor is not None and floor["species_id"] == 25
      and len(floor["changes"]) == 1
      and 1 <= floor["changes"][0]["to"] - floor["changes"][0]["from"] <= 2, str(floor))
check("Untergrenze in der DB gespeichert",
      economy.species_min_ivs(25) == floor["min_ivs"]
      and json.loads(db.query_one("SELECT min_ivs FROM species WHERE id = 25")["min_ivs"])
      == floor["min_ivs"])
check("Andere Arten unberuehrt",
      economy.species_min_ivs(129) == other_before)

# Spawn: nie unter der Untergrenze, darueber weiterhin zufaellig.
db.execute("UPDATE species SET min_ivs = ? WHERE id = 25",
           (json.dumps({"hp": 31, "atk": 20, "def": 1, "spa": 1, "spd": 1, "spe": 1}),))
rolls = [spawn.roll_ivs(25) for _ in range(300)]
check("Spawn-IVs nie unter Untergrenze",
      all(r["hp"] == 31 and r["atk"] >= 20 and min(r.values()) >= 5 for r in rolls))
check("Spawn-IVs oberhalb weiter zufaellig",
      len({r["atk"] for r in rolls}) > 5 and len({r["spe"] for r in rolls}) > 10)
check("roll_ivs ohne Art unveraendert 0-31",
      all(0 <= v <= 31 for v in spawn.roll_ivs().values()))
species_25 = db.query_one("SELECT * FROM species WHERE id = 25")
new_enc = spawn.create_encounter(species_25, None)
enc_ivs = json.loads(db.query_one("SELECT ivs FROM encounter WHERE id = ?", (new_enc,))["ivs"])
check("create_encounter nutzt Untergrenze", enc_ivs["hp"] == 31 and enc_ivs["atk"] >= 20,
      str(enc_ivs))

# Genug Faenge -> Untergrenze ueberall 31, danach passiert nichts mehr.
for _ in range(200):
    economy.raise_species_min_ivs({"species_id": 25})
check("Untergrenze erreicht Maximum",
      economy.species_min_ivs(25) == {k: 31 for k in spawn.STAT_KEYS})
check("Maximum: kein weiterer Anstieg", economy.raise_species_min_ivs({"species_id": 25}) is None)
check("Spawn bei maximaler Untergrenze perfekt",
      spawn.roll_ivs(25) == {k: 31 for k in spawn.STAT_KEYS})

# Seeding ueberschreibt die Untergrenze nicht.
seed._upsert({**species_25, "min_ivs": None})
check("Seeding behaelt Untergrenze",
      economy.species_min_ivs(25) == {k: 31 for k in spawn.STAT_KEYS})

# Maximierte Untergrenze druemmt den Spawn-Pool: kein weiterer Nutzen mehr aus
# einem Fang, also niedrigeres Gewicht statt Ausschluss (economy.species_ivs_maxed,
# spawn._rarity_weight, Setting maxed_ivs_spawn_weight_factor).
species_25_maxed = db.query_one("SELECT * FROM species WHERE id = 25")
check("species_ivs_maxed erkennt maximierte Art",
      economy.species_ivs_maxed(species_25_maxed))
check("Nicht maximierte Art gilt nicht als maxed",
      not economy.species_ivs_maxed({**species_25_maxed, "min_ivs": None}))
db.set_setting("maxed_ivs_spawn_weight_factor", 0.2)
weight_maxed = spawn._rarity_weight(species_25_maxed)
weight_fresh = spawn._rarity_weight({**species_25_maxed, "min_ivs": None})
check("Maximierte Art hat niedrigeres Spawn-Gewicht",
      weight_maxed < weight_fresh, f"{weight_maxed} vs {weight_fresh}")
check("Faktor wird korrekt angewendet",
      abs(weight_maxed - weight_fresh * 0.2) < 1e-9, f"{weight_maxed} vs {weight_fresh * 0.2}")
# Faktor 1.0 -> kein Unterschied mehr.
check("Faktor 1.0 hebt den Abzug auf",
      spawn._rarity_weight(species_25_maxed, maxed_ivs_factor=1.0)
      == spawn._rarity_weight({**species_25_maxed, "min_ivs": None}, maxed_ivs_factor=1.0))
db.set_setting("maxed_ivs_spawn_weight_factor", db.DEFAULT_SETTINGS["maxed_ivs_spawn_weight_factor"])
db.execute("UPDATE species SET min_ivs = NULL WHERE id = 25")

# Migration idempotent.
db.init_db()
check("init_db erneut ohne Fehler (Migration idempotent)", True)

# ------------------------------------------------------ Autokampf-Sieg
db.set_setting("auto_battle_enabled", True)
db.execute("UPDATE owned_pokemon SET party_slot = NULL WHERE user_id = 'alice'")
strong = add_owned("alice", 150, {k: 31 for k in spawn.STAT_KEYS}, level=80)
db.execute("UPDATE owned_pokemon SET party_slot = 1 WHERE id = ?", (strong,))
enc_id = make_encounter(129, 2, IVS)  # Karpador ist Alice bekannt
money_before = economy.balance("alice")
res = encounters_api.run_auto_battle(enc_id, alice)
check("Autokampf gewonnen", res["outcome"] == "won", res["outcome"])
check("Kampfsieg bringt Geld", res.get("money_earned", 0) > 0
      and economy.balance("alice") == money_before + res["money_earned"],
      f"+{res.get('money_earned')}")

# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
