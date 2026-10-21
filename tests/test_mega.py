"""Tests fuer Mega-Entwicklung und Proto-Formen (app/mega.py).

Deckt Katalog/Shop-Konsistenz, die Kampfregeln (einmal pro Seite und Kampf,
kostet die Runde nicht, haelt bis Kampfende) und ab, dass die Form NUR im
Kampf lebt - `owned_pokemon` bleibt unberuehrt.

Aufruf:  python3 tests/test_mega.py
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-mega-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, battle_session, db, economy, held_items, i18n, mega, seed, spawn  # noqa: E402
from app import trainer_battle, trainers  # noqa: E402
from app.api import shop as shop_api  # noqa: E402
from app.api import team as team_api  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


def expect_http(label: str, status: int, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
        check(label, False, "keine Exception")
    except HTTPException as exc:
        check(label, exc.status_code == status, f"{exc.status_code}: {exc.detail}")


# ---------------------------------------------------------------- Setup
random.seed(int(os.environ.get("TEST_SEED", "4242")))
db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
IVS = {k: 15 for k in spawn.STAT_KEYS}
NOW = datetime.now().astimezone()


def make_species(species_id: int, name: str, type1: str, type2: str | None = None) -> dict:
    """Mega-faehige Arten fehlen im Fallback-Pokédex - direkt einfuegen."""
    db.execute(
        """
        INSERT INTO species (id, name, name_en, name_de, type1, type2, generation, catch_rate,
            base_exp, growth_rate, base_hp, base_atk, base_def, base_spa, base_spd, base_spe)
        VALUES (?,?,?,?,?,?,1,45,240,'medium_slow',78,84,78,109,85,100)
        ON CONFLICT(id) DO NOTHING
        """,
        (species_id, name, name.title(), name.title(), type1, type2),
    )
    return db.query_one("SELECT * FROM species WHERE id = ?", (species_id,))


CHARIZARD = make_species(6, "charizard", "fire", "flying")
KYOGRE = make_species(382, "kyogre", "water")
KARP = db.query_one("SELECT * FROM species WHERE name = 'magikarp'")

STONE = "charizardite_x"  # Glurak -> Mega-Glurak X (Feuer/Drache, Tough Claws)


def make_owned(sp: dict, level: int, slot: int | None, item: str | None = None,
               user_id: str = "alice") -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "current_hp, party_slot, caught_at, held_item, ability) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, sp["id"], level, battle.exp_for_level(level, sp["growth_rate"]), 0,
         json.dumps(IVS), "hardy", None, slot, NOW.isoformat(), item, "blaze"),
    )


def owned(owned_id: int) -> dict:
    return db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))


def combatant(sp: dict, level: int = 50, item: str | None = None) -> battle.Combatant:
    return battle.Combatant(species=sp, level=level, ivs=dict(IVS), held_item=item)


# --------------------------------------------------------------- Katalog
catalog = mega.catalog()
check("Katalog enthaelt alle Mega-Formen mit Stein", len(catalog) >= 45, len(catalog))
check(
    "Jede Form hat Art, Typ, Basiswerte, Faehigkeit und Sprite",
    all(
        form["species_id"] and form["type1"] and form["sprite_url"] and form["ability"]
        and form["kind"] in ("mega", "primal")
        and all(form.get(f"base_{key}") for key in ("hp", "atk", "def", "spa", "spd", "spe"))
        for form in catalog.values()
    ),
)
check("Beide Sprachen haben einen Formnamen",
      all(form["name"] and form["name_de"] for form in catalog.values()))
check("Glurak hat zwei Formen (X und Y)", len(mega.forms_for_species(6)) == 2)
check("Proto-Formen laufen ueber dieselben Daten",
      {mega.get("blue_orb")["kind"], mega.get("red_orb")["kind"]} == {"primal"})

held = held_items.catalog()
check("Jeder Stein ist ein getragenes Item mit effect 'mega_stone'",
      all(held.get(key, {}).get("effect") == mega.EFFECT for key in catalog))
shop_items = {item["key"]: item for item in economy.catalog()}
check("Jeder Stein steht kaeuflich im Shop (Kategorie 'mega')",
      all(shop_items.get(key, {}).get("category") == mega.CATEGORY
          and shop_items[key]["price"] > 0 and economy.is_buyable(shop_items[key])
          for key in catalog))

check("form_for verlangt die passende Art",
      mega.form_for(6, STONE) is not None and mega.form_for(129, STONE) is None
      and mega.form_for(6, "leftovers") is None)
check("Shop bietet nur Steine an, deren Art der Pokédex kennt",
      STONE in mega.available_stones() and "venusaurite" not in mega.available_stones(),
      sorted(mega.available_stones()))
shop_keys = {item["key"] for item in shop_api.shop(current_user=db.get_user("alice"))["items"]}
check("/api/shop blendet Steine unbekannter Arten aus",
      STONE in shop_keys and "venusaurite" not in shop_keys)


# ------------------------------------------------------- Formwechsel
form = mega.form_for(6, STONE)
plain, megad = combatant(CHARIZARD), combatant(CHARIZARD, item=STONE)
check("Nur mit passendem Stein ist eine Mega-Entwicklung moeglich",
      battle.mega_candidate(megad) is not None and battle.mega_candidate(plain) is None)

before_atk, before_hp = megad.stat("atk"), megad.current_hp
battle.apply_mega(megad, form)
check("Mega-Form tauscht Typen", megad.types == ["fire", "dragon"], megad.types)
check("Mega-Form tauscht Basiswerte", megad.stat("atk") > before_atk,
      f"{before_atk} -> {megad.stat('atk')}")
check("Mega-Form tauscht Faehigkeit und Namen",
      megad.ability == form["ability"] and megad.name == form["name"],
      f"{megad.ability} / {megad.name}")
check("KP bleiben unveraendert", megad.current_hp == before_hp)
check("Dex-Nummer und Basis-EXP bleiben die der Grundform",
      megad.species["id"] == CHARIZARD["id"]
      and megad.species["base_exp"] == CHARIZARD["base_exp"])
check("base_species/base_ability zeigen weiter auf die Grundform",
      megad.base_species["name"] == CHARIZARD["name"] and megad.base_ability is None)
check("describe() meldet die Mega-Form mit Sprite",
      battle.describe(megad)["mega"] == STONE
      and battle.describe(megad)["sprite_url"] == form["sprite_url"])
check("Ein Level-up rechnet weiter mit der Grundform",
      (lambda c: (c.refresh_after_level(),
                  c.max_hp == battle.stat_hp(CHARIZARD["base_hp"], 15, c.level))[1])(megad))
# Gegen einen Gegner, der den Treffer aushaelt (expected_damage deckelt sonst
# bei dessen KP) - Feuersturm behaelt in der Mega-Form seinen STAB-Bonus.
tank = combatant(KYOGRE, 100)
check("Mega-Form macht mehr Schaden",
      battle.expected_damage(megad, tank, "fire_blast")
      > battle.expected_damage(plain, tank, "fire_blast"))
check("Hoehere Basiswerte schlagen im Kampfwert durch",
      battle.effective_stat(megad, "spa") > battle.effective_stat(plain, "spa"),
      f"{battle.effective_stat(plain, 'spa')} -> {battle.effective_stat(megad, 'spa')}")


# ------------------------------------------------------ Regeln im Kampf
def fresh_battle(items: list[str | None]) -> battle.Battle:
    team = [combatant(CHARIZARD, 50, item) for item in items]
    return battle.Battle(wild=combatant(KARP, 5), team=team, active=0, lang="en")


b = fresh_battle([STONE, STONE])
check("Vor der Mega-Entwicklung ist sie verfuegbar", battle.can_mega_evolve(b, b.active_mon))
view = battle.mega_view(b)
check("mega_view nennt die moegliche Form",
      view["available"] and not view["used"] and view["option"]["stone"] == STONE
      and view["active"] is None)
battle.mega_evolve(b, b.active_mon)
check("Mega-Entwicklung markiert die Seite", b.mega_used and b.active_mon.mega == STONE)
check("Log nennt Form und Stein",
      "Mega Charizard X" in b.log[-1] and "Charizardite X" in b.log[-1], b.log[-1])
check("Mega-Entwicklung kostet die Runde nicht",
      b.turn == 0 and b.wild.current_hp == b.wild.max_hp)
try:
    battle.mega_evolve(b, b.active_mon)
    check("Zweite Mega-Entwicklung wird abgelehnt", False)
except ValueError as exc:
    check("Zweite Mega-Entwicklung wird abgelehnt",
          str(exc) == i18n.t("en", "battle.error.mega_already_used"), str(exc))
check("Auch ein anderes Teammitglied kann nicht mehr megaentwickeln",
      not battle.can_mega_evolve(b, b.team[1]))
check("mega_view meldet die verbrauchte Mega-Entwicklung",
      battle.mega_view(b)["used"] and not battle.mega_view(b)["available"]
      and battle.mega_view(b)["active"]["stone"] == STONE)

# Auswechseln beendet die Form nicht (wie im Spiel).
b.team[1].held_item = None
battle.switch_to(b, 1)
battle.switch_to(b, 0)
check("Mega-Form ueberlebt das Auswechseln", b.team[0].mega == STONE)

b = fresh_battle([None])
try:
    battle.mega_evolve(b, b.active_mon)
    check("Ohne Stein keine Mega-Entwicklung", False)
except ValueError as exc:
    check("Ohne Stein keine Mega-Entwicklung",
          str(exc) == i18n.t("en", "battle.error.mega_not_possible"), str(exc))

# Gegner (Arena/Liga/Spielerkopie) megaentwickelt von selbst, wilde nie.
b = fresh_battle([None])
b.wild = combatant(CHARIZARD, 50, STONE)
b.wild.trainer = "Rival"
battle.run_turn(b, b.active_mon.moves[0])
check("Trainer-Gegner megaentwickelt zu Rundenbeginn",
      b.foe_mega_used and b.wild.mega == STONE)
check("Aber auch nur einmal", not battle.can_mega_evolve(b, b.wild))
wild_b = fresh_battle([None])
wild_b.wild.held_item = STONE  # wilde Pokémon tragen nie Items - defensiv geprueft
battle.run_turn(wild_b, wild_b.active_mon.moves[0])
check("Wildes Pokémon megaentwickelt nicht", wild_b.wild.mega is None)


# ---------------------------------------------------- Manueller Kampf
char_id = make_owned(CHARIZARD, 50, 1, STONE)
encounter_id = spawn.create_encounter(KARP, source_entity="test.trigger")
db.execute("UPDATE encounter SET level = ?, ivs = ? WHERE id = ?", (3, json.dumps(IVS), encounter_id))
session = battle_session.start(encounter_id, "alice")
check("Kampfansicht bietet die Mega-Entwicklung an",
      session["mega"]["available"] and session["mega"]["option"]["stone"] == STONE)
after = battle_session.act(session["id"], "alice", {"action": "mega"})
check("Aktion 'mega' entwickelt das aktive Pokémon",
      after["mega"]["used"] and after["team"][0]["mega"] == STONE
      and after["team"][0]["name"] == form["name"], after["team"][0]["name"])
check("Aktion 'mega' verbraucht keine Runde", after["turn"] == 0)
check("Mega-Form uebersteht das Neuladen der Sitzung",
      battle_session.get_view(session["id"], "alice")["team"][0]["mega"] == STONE)
check("owned_pokemon behaelt seine Art", owned(char_id)["species_id"] == CHARIZARD["id"])
expect_http("Zweite Mega-Entwicklung im Kampf: 409", 409,
            battle_session.act, session["id"], "alice", {"action": "mega"})

# Kampf zu Ende spielen: danach ist das Pokémon wieder normal.
result = after
for _ in range(20):
    if result["over"]:
        break
    result = battle_session.act(session["id"], "alice", {"action": "move",
                                                         "move": result["team"][0]["moves"][0]["key"]})
check("Kampf endet mit Sieg", result["outcome"] == "won", result["outcome"])
check("Nach dem Kampf ist die Mega-Form weg (nur Kampfzustand)",
      owned(char_id)["species_id"] == CHARIZARD["id"]
      and owned(char_id)["held_item"] == STONE
      and team_api.describe_owned(owned(char_id))["species"]["name"] == CHARIZARD["name"])

# Ohne Stein lehnt die Aktion ab.
db.execute("UPDATE owned_pokemon SET held_item = NULL WHERE id = ?", (char_id,))
encounter2 = spawn.create_encounter(KARP, source_entity="test.trigger")
db.execute("UPDATE encounter SET level = ?, ivs = ? WHERE id = ?", (3, json.dumps(IVS), encounter2))
session2 = battle_session.start(encounter2, "alice")
check("Ohne Stein bietet die Ansicht keine Mega-Entwicklung",
      not session2["mega"]["available"] and session2["mega"]["option"] is None)
expect_http("Aktion 'mega' ohne Stein: 409", 409,
            battle_session.act, session2["id"], "alice", {"action": "mega"})
battle_session.act(session2["id"], "alice", {"action": "run"})


# --------------------------------------------------------- Auto-Kampf
db.execute("UPDATE owned_pokemon SET held_item = ? WHERE id = ?", (STONE, char_id))
auto_encounter = spawn.create_encounter(KARP, source_entity="test.trigger")
db.execute("UPDATE encounter SET level = ?, ivs = ? WHERE id = ?",
           (3, json.dumps(IVS), auto_encounter))
auto = battle.auto_battle(
    db.query_one("SELECT * FROM encounter WHERE id = ?", (auto_encounter,)), "alice")
check("Auto-Kampf nutzt die Mega-Entwicklung von selbst",
      any("Mega Charizard X" in line for line in auto["log"]), auto["log"][:4])


# --------------------------------------------------------- Team-Ansicht
db.execute("UPDATE owned_pokemon SET held_item = ? WHERE id = ?", (STONE, char_id))
described = team_api.describe_owned(owned(char_id))
check("Pokémon-Menue nennt die erreichbare Mega-Form",
      described["mega_form"]["stone"] == STONE
      and {f["stone"] for f in described["mega_forms"]} == {"charizardite_x", "charizardite_y"})
db.execute("UPDATE owned_pokemon SET held_item = 'leftovers' WHERE id = ?", (char_id,))
check("Ohne passenden Stein bleibt nur der Hinweis auf moegliche Steine",
      team_api.describe_owned(owned(char_id))["mega_form"] is None
      and len(team_api.describe_owned(owned(char_id))["mega_forms"]) == 2)
karp_id = make_owned(KARP, 10, None)
check("Arten ohne Mega-Form melden keine",
      not team_api.describe_owned(owned(karp_id))["mega_forms"])


# ------------------------------------------------------------ Arenakampf
# Arena-/Liga- und Spielerkaempfe halten ihren Kampfzustand anders als der
# wilde Kampf (trainer_battle.mon_state) - deshalb hier derselbe Durchlauf
# mit einem synthetischen Trainer (wie in tests/test_league.py).
db.execute("UPDATE owned_pokemon SET held_item = ? WHERE id = ?", (STONE, char_id))
_saved_raw, _saved_trainers = trainers._raw, trainers._trainers
trainers._raw = {
    "regions": [{"key": "kanto", "label": "Kanto"}],
    "trainers": [{
        "id": "test_mega", "kind": "gym_leader", "region": "kanto",
        "name": "Testa", "team": [["magikarp", 30]],
    }],
}
trainers._trainers = None
view = trainer_battle.start("test_mega", "alice")
check("Arenakampf bietet die Mega-Entwicklung an", view["mega"]["available"])
view = trainer_battle.act(view["id"], "alice", {"action": "mega"})
check("Aktion 'mega' wirkt auch im Arenakampf",
      view["mega"]["used"] and view["team"][0]["mega"] == STONE and view["turn"] == 0)
check("Mega-Form uebersteht das Neuladen der Arena-Sitzung",
      trainer_battle.get_view(view["id"], "alice")["team"][0]["mega"] == STONE)
for _ in range(30):
    if view["over"]:
        break
    active_member = next(m for m in view["team"] if m["active"])
    view = trainer_battle.act(view["id"], "alice",
                              {"action": "move", "move": active_member["moves"][0]["key"]})
check("Arenakampf endet", view["over"], view.get("outcome"))
check("Auch nach dem Arenakampf bleibt die Art unveraendert",
      owned(char_id)["species_id"] == CHARIZARD["id"])
trainers._raw, trainers._trainers = _saved_raw, _saved_trainers


# ------------------------------------------------------------ Ueber HTTP
# Einmal der ganze Weg ueber die echten Endpunkte: kaufen, anlegen,
# megaentwickeln.
client = TestClient(app)  # ohne `with` -> kein Lifespan (Poller/Seeding)
logging.getLogger("httpx").setLevel(logging.WARNING)
HEADERS = {"X-Remote-User-Id": "bob", "X-Remote-User-Name": "Bob"}
db.upsert_user("bob", "Bob")
bob_char = make_owned(CHARIZARD, 40, 1, user_id="bob")
price = economy.unit_price(shop_items[STONE])
db.execute("UPDATE app_user SET money = ? WHERE id = 'bob'", (price,))
r = client.post("/api/shop/buy", headers=HEADERS, json={"item_key": STONE, "quantity": 1})
check("POST /shop/buy kauft einen Mega-Stein", r.status_code == 200 and r.json()["owned"] == 1,
      f"{r.status_code} {r.text[:200]}")
r = client.put(f"/api/team/{bob_char}/held-item", headers=HEADERS, json={"item": STONE})
check("PUT /team/{id}/held-item legt den Stein an",
      r.status_code == 200 and r.json()["mega_form"]["stone"] == STONE,
      f"{r.status_code} {r.text[:200]}")
bob_encounter = spawn.create_encounter(KARP, source_entity="test.trigger")
db.execute("UPDATE encounter SET level = ?, ivs = ?, user_id = 'bob' WHERE id = ?",
           (3, json.dumps(IVS), bob_encounter))
r = client.post(f"/api/encounters/{bob_encounter}/battle", headers=HEADERS)
check("POST /encounters/{id}/battle meldet die Mega-Option",
      r.status_code == 200 and r.json()["mega"]["available"], f"{r.status_code} {r.text[:200]}")
battle_id = r.json()["id"]
r = client.post(f"/api/battles/{battle_id}/action", headers=HEADERS, json={"action": "mega"})
check("POST /battles/{id}/action mit action=mega",
      r.status_code == 200 and r.json()["team"][0]["mega"] == STONE
      and r.json()["mega"]["used"], f"{r.status_code} {r.text[:200]}")
r = client.post(f"/api/battles/{battle_id}/action", headers=HEADERS, json={"action": "mega"})
check("Zweite Mega-Entwicklung ueber HTTP: 409", r.status_code == 409, r.status_code)
client.post(f"/api/battles/{battle_id}/action", headers=HEADERS, json={"action": "run"})


# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
