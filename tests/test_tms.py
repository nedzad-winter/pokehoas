"""Tests fuer TMs (app/tms.py) und die Item-Belohnungen der Erfolge
(app/rewards.py): Katalog, Arena-Belohnungen je Orden, Meisterball je Region
und das Beibringen einer TM-Attacke.

Aufruf:  python3 tests/test_tms.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-tms-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import (  # noqa: E402
    achievements, db, economy, i18n, moves as moves_mod, rewards, seed, tms, trainer_battle,
    trainers,
)
from app.api import shop as shop_api, team as team_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


def expect_error(label: str, fn, *args) -> None:
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001 - genau das ist der Test
        check(label, True, getattr(exc, "detail", exc))
        return
    check(label, False, "kein Fehler")


# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")

pika = db.query_one("SELECT * FROM species WHERE name = 'pikachu'")
karp = db.query_one("SELECT * FROM species WHERE name = 'magikarp'")
check("Fallback-Pokédex enthaelt Pikachu und Karpador", pika is not None and karp is not None)


def add_pokemon(user_id: str, species: dict, level: int, party_slot: int | None = 1) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "caught_at, party_slot) VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, species["id"], level, 0, 0, json.dumps({k: 20 for k in
         ("hp", "atk", "def", "spa", "spd", "spe")}), "hardy", db.now_iso() if hasattr(db, "now_iso")
         else "2026-01-01T00:00:00+00:00", party_slot),
    )


def bag(user_id: str) -> dict[str, int]:
    return {row["item_key"]: row["quantity"] for row in
            db.query("SELECT item_key, quantity FROM inventory WHERE user_id = ?", (user_id,))}


# --------------------------------------------------------------- Katalog
catalog = tms.all_tms()
check("TM-Katalog nicht leer", len(catalog) > 20, len(catalog))
check("Jeder TM-Key folgt dem Muster tm_<attacke>",
      all(key == f"tm_{tm['move']}" for key, tm in catalog.items()))
check("Jede TM-Attacke steht in data/moves.json",
      all(tm["move"] in moves_mod.all_moves() for tm in catalog.values()))
numbers = [tm["number"] for tm in catalog.values()]
check("TM-Nummern sind lueckenlos und eindeutig (1..n)",
      sorted(numbers) == list(range(1, len(catalog) + 1)), numbers)

gym_leaders = [t for t in trainers.all_trainers() if t["kind"] == "gym_leader"]
check("Jeder Arenaleiter vergibt eine TM",
      all(tms.for_gym(t["id"]) for t in gym_leaders),
      [t["id"] for t in gym_leaders if not tms.for_gym(t["id"])])
check("Jeder Arenaleiter nennt die TM der Hauptspiele (Dokumentation)",
      all(tms.canonical_for_gym(t["id"]) for t in gym_leaders))
check("Top Vier und Champs vergeben keine TM",
      not any(tms.for_gym(t["id"]) for t in trainers.all_trainers()
              if t["kind"] != "gym_leader"))
gym_awarded = {tms.for_gym(t["id"]) for t in gym_leaders}
check("Jede TM ohne Preis wird von mindestens einer Arena vergeben (Shop-TMs duerfen Arena-frei sein)",
      {key for key, tm in catalog.items() if tm.get("price") is None} <= gym_awarded)
check("Arenen vergeben nur TMs aus dem Katalog", gym_awarded <= set(catalog))
check("Alola hat keine Arenen (Inselpruefungen) und damit keine TM",
      not [t for t in gym_leaders if t["region"] == "alola"])

brock_tm = tms.for_gym("kanto_brock")
check("Brock vergibt die TM zu Steinwurf (wie in Feuerrot/Blattgrün)",
      tms.move_key(brock_tm) == "rock_tomb", brock_tm)
check("TM-Label nennt Nummer und Attacke", tms.label(brock_tm) == "TM33 Rock Tomb",
      tms.label(brock_tm))
check("TM-Label ist uebersetzt", tms.label(brock_tm, "de") == "TM33 Felsgrab",
      tms.label(brock_tm, "de"))
check("TM-Beschreibung nennt das Mindestlevel der Attacke",
      "12" in tms.description(brock_tm), tms.description(brock_tm))
check("TM-Icon ist die Disc im Typ der Attacke",
      tms.sprite_url(brock_tm).endswith("tm-rock.png"), tms.sprite_url(brock_tm))

by_key = economy.catalog_by_key()
check("TMs stehen im Item-Katalog (Label im Beutel, Kampf-Log)",
      all(key in by_key for key in catalog))

priced_keys = {key for key, tm in catalog.items() if tm.get("price") is not None}
unpriced_keys = set(catalog) - priced_keys
check("Im Hauptspiel fuer Geld verkaufte TMs sind kaeuflich",
      bool(priced_keys) and all(economy.is_buyable(by_key[key]) for key in priced_keys))
check("Alle anderen TMs bleiben Arena-exklusiv (nicht kaeuflich)",
      not any(economy.is_buyable(by_key[key]) for key in unpriced_keys))
check("Arena-exklusive TMs haben keinen Preis (sortieren im Beutel nach hinten)",
      all(by_key[key]["price"] is None for key in unpriced_keys))
check("Staerkere Attacken kosten mehr: Donner (110 Power) ist teurer als Steinwurf (60 Power)",
      by_key["tm_thunder"]["price"] > by_key["tm_rock_tomb"]["price"])
check("economy.item_label liefert das TM-Label in beiden Sprachen",
      economy.item_label(brock_tm) == "TM33 Rock Tomb"
      and economy.item_label(brock_tm, "de") == "TM33 Felsgrab")


# ------------------------------------------------- Belohnungen je Arena
kanto_gyms = [t["id"] for t in gym_leaders if t["region"] == "kanto"]
check("Kanto hat acht Arenen", len(kanto_gyms) == 8, kanto_gyms)
check("gym_index zaehlt innerhalb der Region ab 1",
      [rewards.gym_index(tid) for tid in kanto_gyms] == list(range(1, 9)))
check("Top Vier hat keinen Arena-Index", rewards.gym_index("kanto_lorelei") is None)


def balls(trainer_id: str) -> dict[str, int]:
    return {item["key"]: item["quantity"] for item in rewards.gym_items(trainer_id)
            if not tms.is_tm(item["key"])}


check("Erste Arena bringt 2 Pokébaelle", balls("kanto_brock") == {"poke_ball": 2},
      balls("kanto_brock"))
check("Achte Arena bringt 5 Poké-, 5 Super- und 5 Hyperbaelle",
      balls("kanto_giovanni") == {"poke_ball": 5, "great_ball": 5, "ultra_ball": 5},
      balls("kanto_giovanni"))
check("Jede Arena bringt zusaetzlich genau eine TM",
      all(sum(item["quantity"] for item in rewards.gym_items(t["id"])
              if tms.is_tm(item["key"])) == 1 for t in gym_leaders))

tiers = ["poke_ball", "great_ball", "ultra_ball"]
by_index = {index: rewards.GYM_BALLS[index] for index in sorted(rewards.GYM_BALLS)}
check("Nur die drei regulaeren Ball-Stufen kommen vor (bis Hyperball)",
      all(set(entry) <= set(tiers) for entry in by_index.values()))
check("Nie mehr als 5 Baelle je Stufe",
      all(count <= 5 for entry in by_index.values() for count in entry.values()))
check("Je Arena hoechstens drei Stufen", all(len(entry) <= 3 for entry in by_index.values()))
check("Keine Stufe wird mit steigender Arena schlechter",
      all(by_index[index].get(tier, 0) <= by_index[index + 1].get(tier, 0)
          for tier in tiers for index in range(1, max(by_index))),
      by_index)
check("Die Gesamtzahl der Baelle steigt mit jeder Arena",
      all(sum(by_index[index].values()) < sum(by_index[index + 1].values())
          for index in range(1, max(by_index))),
      {i: sum(e.values()) for i, e in by_index.items()})
check("Hoehere Stufen kommen erst spaeter dazu",
      "great_ball" not in by_index[3] and "ultra_ball" not in by_index[5])

galar_gyms = [t["id"] for t in gym_leaders if t["region"] == "galar"]
check("Galar hat zehn Arenen", len(galar_gyms) == 10, len(galar_gyms))
check("Galars Arenen 9 und 10 zahlen wie eine achte Arena",
      balls(galar_gyms[8]) == balls(galar_gyms[9]) == balls("kanto_giovanni"))

check("Eine komplette Region bringt genau einen Meisterball",
      rewards.region_items("kanto") == [{"key": rewards.MASTER_BALL, "quantity": 1}])
check("Belohnungs-Text nennt Anzahl und Item",
      rewards.label(rewards.gym_items("kanto_brock")) == "2× Poké Ball, TM33 Rock Tomb",
      rewards.label(rewards.gym_items("kanto_brock")))
check("Belohnungs-Text ist uebersetzt",
      rewards.label(rewards.gym_items("kanto_brock"), "de") == "2× Pokéball, TM33 Felsgrab",
      rewards.label(rewards.gym_items("kanto_brock"), "de"))

defs = {d["key"]: d for d in achievements.definitions()}
check("Jeder Arena-Erfolg traegt seine Belohnung",
      all(defs[achievements.trainer_key(t["id"])]["reward"] for t in gym_leaders))
check("Top-Vier-Erfolge haben keine Item-Belohnung (nur Preisgeld)",
      defs[achievements.trainer_key("kanto_lorelei")]["reward"] is None)
check("Jeder Regions-Erfolg belohnt einen Meisterball",
      all(defs[achievements.region_key(r["key"])]["reward"]["items"]
          == [{"key": rewards.MASTER_BALL, "quantity": 1}] for r in trainers.regions()))
check("Liga-Meister behaelt den Schillerpin",
      defs[achievements.LEAGUE_MASTER]["reward"]["items"]
      == [{"key": achievements.SHINY_CHARM, "quantity": 1}])


# ------------------------------------------- Vergabe beim ersten Sieg
new = achievements.on_trainer_defeated("alice", "kanto_brock")
check("Sieg ueber Brock schaltet den Erfolg frei", [a["key"] for a in new]
      == [achievements.trainer_key("kanto_brock")], new)
check("Brocks Belohnung landet im Beutel",
      bag("alice") == {"poke_ball": 2, brock_tm: 1}, bag("alice"))
achievements.on_trainer_defeated("alice", "kanto_brock")
check("Ein zweiter Sieg ueber Brock bringt nichts mehr (nicht farmbar)",
      bag("alice") == {"poke_ball": 2, brock_tm: 1}, bag("alice"))

achievements.on_trainer_defeated("alice", "kanto_misty")
check("Die zweite Arena legt ihre eigene Belohnung dazu",
      bag("alice") == {"poke_ball": 5, brock_tm: 1, tms.for_gym("kanto_misty"): 1},
      bag("alice"))

# Dieselbe TM aus einer anderen Region stapelt (TMs sind Einmal-Items).
achievements.on_trainer_defeated("bob", "kanto_brock")
achievements.on_trainer_defeated("bob", "hoenn_roxanne")
check("Roxanne vergibt dieselbe TM wie Brock",
      tms.for_gym("hoenn_roxanne") == brock_tm)
check("Dieselbe TM aus zwei Regionen stapelt sich", bag("bob").get(brock_tm) == 2, bag("bob"))
check("Belohnungen sind pro Nutzer", bag("alice").get(brock_tm) == 1)

# Ganze Region -> Meisterball, aber erst beim letzten Trainer.
kanto_ids = [t["id"] for t in trainers.all_trainers() if t["region"] == "kanto"]
for tid in kanto_ids[:-1]:
    achievements.on_trainer_defeated("alice", tid)
check("Vor dem letzten Kanto-Trainer gibt es keinen Meisterball",
      rewards.MASTER_BALL not in bag("alice"), bag("alice"))
last = achievements.on_trainer_defeated("alice", kanto_ids[-1])
check("Der letzte Trainer schaltet auch den Regions-Erfolg frei",
      achievements.region_key("kanto") in {a["key"] for a in last}, [a["key"] for a in last])
check("Komplette Region bringt einen Meisterball", bag("alice").get(rewards.MASTER_BALL) == 1)
check("Alle acht Kanto-TMs liegen im Beutel",
      all(bag("alice").get(tms.for_gym(tid)) for tid in kanto_gyms), bag("alice"))
check("Kanto-Baelle summieren sich ueber alle acht Orden",
      bag("alice")["poke_ball"] == sum(rewards.GYM_BALLS[i].get("poke_ball", 0)
                                       for i in range(1, 9)),
      bag("alice")["poke_ball"])
before = bag("alice")
achievements.on_trainer_defeated("alice", kanto_ids[-1])
check("Erneuter Sieg gibt keinen zweiten Meisterball", bag("alice") == before)


# ------------------------------------------------- TM-Attacke beibringen
pika_id = add_pokemon("alice", pika, 30)
surf_tm = tms.for_gym("sinnoh_wake")
check("Crasher Wake vergibt die TM zu Surfer", tms.move_key(surf_tm) == "surf")
brick_tm = "tm_brick_break"
check("Karateschlag gehoert nicht zur normalen Lernliste von Pikachu",
      "brick_break" not in moves_mod.learned_moves(pika, 30))
check("Pikachu ist ein moegliches Ziel fuer die Karateschlag-TM",
      pika_id in [t["owned_id"] for t in tms.targets_for("alice", brick_tm)])

expect_error("Ohne TM im Beutel schlaegt das Beibringen fehl",
             tms.teach, "alice", pika_id, brick_tm)
rewards.grant("alice", [{"key": brick_tm, "quantity": 2}])
# Zwei gewaehlte Attacken: es ist noch Platz, die TM-Attacke kommt direkt dazu.
db.execute("UPDATE owned_pokemon SET selected_moves = ? WHERE id = ?",
           (json.dumps(["thunderbolt", "quick_attack"]), pika_id))
result = tms.teach("alice", pika_id, brick_tm)
check("Rueckmeldung nennt Pokémon und Attacke",
      "Pikachu" in result["message"] and "Brick Break" in result["message"], result["message"])
row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (pika_id,))
check("Die Attacke steht in taught_moves", tms.taught_moves(row) == ["brick_break"], row["taught_moves"])
check("Die TM ist verbraucht", bag("alice")[brick_tm] == 1, bag("alice")[brick_tm])
check("Karateschlag ist jetzt Teil der Lernliste dieses Pikachu",
      "brick_break" in moves_mod.learned_moves(pika, 30, tms.taught_moves(row)))
check("Ohne die TM-Attacke bleibt die Lernliste der Art unveraendert",
      "brick_break" not in moves_mod.learned_moves(pika, 30))
selected = json.loads(row["selected_moves"] or "null") or []
check("Freier Attackenplatz wird direkt belegt", "brick_break" in selected, selected)
check("Die bisherigen Attacken bleiben stehen",
      selected == ["thunderbolt", "quick_attack", "brick_break"], selected)
check("Es bleiben maximal vier Attacken", len(selected) <= moves_mod.MAX_MOVES, selected)
check("Der Rueckgabewert meldet die automatische Auswahl", result["selected"] is True, result)

expect_error("Dieselbe Attacke ein zweites Mal wird abgelehnt",
             tms.teach, "alice", pika_id, brick_tm)
check("Ein Pokémon, das die Attacke schon kennt, ist kein Ziel mehr",
      pika_id not in [t["owned_id"] for t in tms.targets_for("alice", brick_tm)])
check("Die abgelehnte TM ist noch im Beutel", bag("alice")[brick_tm] == 1)

# Levelgrenze der Attacke gilt weiter.
low_id = add_pokemon("alice", pika, 5, party_slot=2)
blizzard_tm = tms.for_gym("galar_melony")
check("Melony vergibt die TM zu Blizzard", tms.move_key(blizzard_tm) == "blizzard")
check("Testaufbau: Pikachu (5) ist fuer Karateschlag kompatibel, nur zu niedrig",
      tms.compatible(pika, brick_tm) and low_id not in
      [t["owned_id"] for t in tms.targets_for("alice", brick_tm)])
expect_error("Zu niedriges Level wird abgelehnt", tms.teach, "alice", low_id, brick_tm)
check("Ein zu niedriges Pokémon ist kein Ziel",
      low_id not in [t["owned_id"] for t in tms.targets_for("alice", brick_tm)])
check("Die Levelgrenze ist die der Attacke",
      tms.min_level(brick_tm) == moves_mod.get("brick_break")["min_level"])
check("Fehlversuch verbraucht keine TM", bag("alice")[brick_tm] == 1)

# Noch keine eigene Auswahl: das generierte Set wird zuerst festgeschrieben,
# damit die TM-Attacke keine Attacke stillschweigend verdraengt.
bulba = db.query_one("SELECT * FROM species WHERE name = 'bulbasaur'")
fresh_id = add_pokemon("alice", bulba, 25, party_slot=4)
bug_tm = "tm_body_slam"
generated = moves_mod.moveset_for(bulba, 25)
check("Testaufbau: Bodyslam ist neu fuer Bisasam (in Karmesin/Purpur per TM, nicht per Level)",
      "body_slam" not in moves_mod.learned_moves(bulba, 25) and tms.compatible(bulba, bug_tm))
check("Struggle Bug kann Bisasam in Gen 9 nicht per TM lernen",
      not tms.compatible(bulba, "tm_struggle_bug"))
rewards.grant("alice", [{"key": bug_tm, "quantity": 1}])
tms.teach("alice", fresh_id, bug_tm)
fresh_sel = json.loads(db.query_one(
    "SELECT selected_moves FROM owned_pokemon WHERE id = ?", (fresh_id,))["selected_moves"])
check("Ohne eigene Auswahl wird das generierte Set festgeschrieben",
      fresh_sel[:len(generated)] == generated, fresh_sel)
check("Keine der generierten Attacken geht dabei verloren",
      all(key in fresh_sel for key in generated), (generated, fresh_sel))

# Volles Attackenset: die TM-Attacke wird gelernt, aber nicht automatisch gewaehlt.
squirtle = db.query_one("SELECT * FROM species WHERE name = 'squirtle'")
full_id = add_pokemon("alice", squirtle, 60, party_slot=3)
db.execute("UPDATE owned_pokemon SET selected_moves = ? WHERE id = ?",
           (json.dumps(moves_mod.moveset_for(squirtle, 60)[:4]), full_id))
check("Testaufbau: vier Attacken gewaehlt",
      len(json.loads(db.query_one("SELECT selected_moves FROM owned_pokemon WHERE id = ?",
                                  (full_id,))["selected_moves"])) == 4)
rewards.grant("alice", [{"key": blizzard_tm, "quantity": 1}])
full_result = tms.teach("alice", full_id, blizzard_tm)
check("Bei vollem Set wird die Attacke gelernt, aber nicht gewaehlt",
      not full_result["selected"], full_result)
full_row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (full_id,))
check("Sie steht trotzdem in der Lernliste",
      "blizzard" in moves_mod.learned_moves(squirtle, 60, tms.taught_moves(full_row)))
check("Die gewaehlten Attacken bleiben bei vier",
      len(json.loads(full_row["selected_moves"])) == 4, full_row["selected_moves"])

# Fremde Pokémon bleiben tabu.
rewards.grant("bob", [{"key": brick_tm, "quantity": 1}])
expect_error("Fremdes Pokémon wird abgelehnt", tms.teach, "bob", pika_id, brick_tm)
expect_error("Unbekannte TM wird abgelehnt", tms.teach, "alice", pika_id, "tm_nonexistent")


# ------------------------------------------- Kompatibilitaet (Typ + Werte)
def sp(**kw) -> dict:
    """Minimale Art fuer tms.compatible (Typen und Basiswerte)."""
    return {"type1": "normal", "type2": None, "base_atk": 100, "base_spa": 100,
            "is_mythical": 0, **kw}


rules = tms._load()["compatibility"]
all_types = set(rules["type_affinity"])
check("Affinitaeten gibt es fuer alle 18 Typen", len(all_types) == 18, sorted(all_types))
check("Affinitaeten und Attacken nennen nur bekannte Typen",
      all(set(v) <= all_types for v in rules["type_affinity"].values())
      and {m["type"] for m in moves_mod.all_moves().values()} <= all_types)
check("Keine Affinitaet listet den eigenen Typ (der steht eh in der Lernliste)",
      all(t not in v for t, v in rules["type_affinity"].items()))

# Real TM learnsets (Scarlet/Violet) beat the type rule: Pikachu learns Surf
# by TM there, Aerial Ace not.
check("Pikachu lernt Surfer per TM (wie in Karmesin/Purpur)", tms.compatible(pika, surf_tm))
check("Pikachu lernt Aero-Ass nicht per TM (Karmesin/Purpur)",
      not tms.compatible(pika, "tm_aerial_ace"))
check("Pikachu kann Feuerwerk (Feuer) nicht lernen",
      not tms.compatible(pika, "tm_flamethrower"))
check("Pikachu kann Karateschlag (Kampf) lernen", tms.compatible(pika, brick_tm))
check("Feuer-Pokémon kann Erdbeben (Boden) lernen",
      tms.compatible(sp(type1="fire"), "tm_earthquake"))
check("Feuer-Pokémon kann Surfer (Wasser) nicht lernen",
      not tms.compatible(sp(type1="fire"), surf_tm))
check("Wasser-Pokémon kann Eisstrahl (Eis) lernen",
      tms.compatible(sp(type1="water"), "tm_ice_beam"))
check("Wasser-Pokémon kann Flammenwurf nicht lernen",
      not tms.compatible(sp(type1="water"), "tm_flamethrower"))
check("Doppeltypen vereinen die Affinitaeten beider Typen",
      tms.compatible(sp(type1="fire", type2="water"), "tm_ice_beam")
      and tms.compatible(sp(type1="fire", type2="water"), "tm_earthquake")
      and not tms.compatible(sp(type1="fire", type2="water"), "tm_energy_ball"))
check("Auch bei Doppeltyp gilt: eigener Typ und Affinitaet, sonst nichts",
      not tms.compatible(sp(type1="grass", type2="poison"), "tm_thunderbolt"))
check("Status-TMs folgen dem Typ ohne Wertegrenze",
      tms.compatible(sp(type1="poison", base_atk=1, base_spa=1), "tm_toxic")
      and not tms.compatible(sp(type1="water", base_atk=1, base_spa=1), "tm_toxic"))

edge = rules["min_offense"]
check("Physische TM braucht Angriff ab min_offense",
      tms.compatible(sp(type1="fire", base_atk=edge, base_spa=1), "tm_earthquake")
      and not tms.compatible(sp(type1="fire", base_atk=edge - 1, base_spa=200), "tm_earthquake"))
check("Spezielle TM braucht Spezial-Angriff ab min_offense",
      tms.compatible(sp(type1="water", base_atk=1, base_spa=edge), "tm_ice_beam")
      and not tms.compatible(sp(type1="water", base_atk=200, base_spa=edge - 1), "tm_ice_beam"))
check("Karpador lernt keine schadensmachende Fremd-TM",
      not any(tms.compatible(karp, key) for key in catalog
              if moves_mod.get(tms.move_key(key))["power"] is not None))
check("Mythische Pokémon lernen jede TM",
      all(tms.compatible(sp(type1="normal", base_atk=1, base_spa=1, is_mythical=1), key)
          for key in catalog))
check("Nicht jede TM passt zu jeder Art (Fallback-Pokédex)",
      all(0 < sum(tms.compatible(s, key) for s in db.query("SELECT * FROM species")) < 29
          for key in ("tm_thunderbolt", "tm_ice_beam", "tm_brick_break")))

# Unpassende Ziele: nicht im Beutel angeboten, von der API abgelehnt, TM bleibt.
ice_tm = blizzard_tm
pika_row_lvl = db.query_one("SELECT level FROM owned_pokemon WHERE id = ?", (pika_id,))["level"]
db.execute("UPDATE owned_pokemon SET level = 60 WHERE id = ?", (pika_id,))
check("Testaufbau: Pikachu (60) erfuellt die Levelgrenze fuer Blizzard",
      tms.min_level(ice_tm) <= 60)
targets = [t["owned_id"] for t in tms.targets_for("alice", ice_tm)]
check("Inkompatibles Pokémon (Pikachu) wird nicht als Ziel angeboten",
      pika_id not in targets, targets)
squirtle_bliz = add_pokemon("alice", squirtle, 60, party_slot=None)
check("Kompatibles Pokémon (Schiggy) wird als Ziel angeboten",
      squirtle_bliz in [t["owned_id"] for t in tms.targets_for("alice", ice_tm)])
rewards.grant("alice", [{"key": ice_tm, "quantity": 1}])
before_tm = bag("alice")[ice_tm]
try:
    tms.teach("alice", pika_id, ice_tm)
    check("Inkompatibles Pokémon wird abgelehnt (400)", False, "kein Fehler")
except Exception as exc:  # noqa: BLE001
    check("Inkompatibles Pokémon wird abgelehnt (400)",
          getattr(exc, "status_code", None) == 400, getattr(exc, "detail", exc))
    check("Die Fehlermeldung nennt Pokémon und Attacke",
          "Pikachu" in exc.detail and "Blizzard" in exc.detail, exc.detail)
check("Die abgelehnte TM bleibt im Beutel", bag("alice")[ice_tm] == before_tm)
check("Am Pikachu hat sich nichts geaendert",
      "blizzard" not in tms.taught_moves(
          db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (pika_id,))))
db.execute("UPDATE owned_pokemon SET level = ? WHERE id = ?", (pika_row_lvl, pika_id))

# Altbestand: vor der Regel gelernte Attacken bleiben nutzbar.
legacy = json.loads(db.query_one(
    "SELECT taught_moves FROM owned_pokemon WHERE id = ?", (pika_id,))["taught_moves"])
db.execute("UPDATE owned_pokemon SET taught_moves = ? WHERE id = ?",
           (json.dumps(legacy + ["surf"]), pika_id))
legacy_row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (pika_id,))
check("Bereits gelernte, jetzt inkompatible TM-Attacke bleibt in der Lernliste",
      "surf" in moves_mod.learned_moves(pika, 30, tms.taught_moves(legacy_row)))
db.execute("UPDATE owned_pokemon SET taught_moves = ? WHERE id = ?",
           (json.dumps(legacy), pika_id))


# ------------------------------------------------------------ API-Schicht
current = {"id": "alice", "language": "de", "name": "Alice"}
detail = team_api.get_pokemon(pika_id, current)
learned = {m["key"]: m for m in detail["learned_moves"]}
check("Team-Detail listet die TM-Attacke", "brick_break" in learned)
check("Die TM-Attacke ist als solche markiert", learned["brick_break"]["via_tm"] is True)
check("Normale Attacken sind nicht als TM markiert",
      all(not m["via_tm"] for key, m in learned.items() if key != "brick_break"))
check("Die TM-Attacke ist gewaehlt (Speichern muss sie akzeptieren)",
      learned["brick_break"]["selected"] is True)
saved = team_api.set_moves(pika_id, team_api.MoveSelection(moves=["brick_break"]), current)
check("PUT /team/{id}/moves akzeptiert die TM-Attacke",
      [m["key"] for m in saved["moves"]] == ["brick_break"], saved["moves"])

bag_view = shop_api.bag(current)
tm_cards = {it["key"]: it for it in bag_view["items"] if it["category"] == tms.CATEGORY}
check("Beutel zeigt die TMs in ihrer eigenen Kategorie", brick_tm in tm_cards, list(tm_cards))
check("Beutel-Karte traegt Label, Beschreibung und Icon",
      tm_cards[brick_tm]["label_de"].startswith("TM")
      and tm_cards[brick_tm]["description_de"]
      and tm_cards[brick_tm]["sprite_url"].endswith("tm-fighting.png"),
      tm_cards[brick_tm])
check("Beutel-Karte bietet die moeglichen Ziele an",
      isinstance(tm_cards[brick_tm]["targets"], list))
shop_view = shop_api.shop(current)
shop_tm_keys = {it["key"] for it in shop_view["items"] if it["category"] == tms.CATEGORY}
check("Der Shop verkauft genau die TMs mit Preis",
      shop_tm_keys and shop_tm_keys == priced_keys, shop_tm_keys)
check("Brick Break (Karmesin/Purpur-TM) steht auch im Shop", brick_tm in shop_tm_keys)
check("Roost (reine Arena-Belohnung) steht nicht im Shop", "tm_roost" not in shop_tm_keys)
check("Ersatz-TMs ohne echte TM (type_based) bleiben Arena-exklusiv (ausser Aurora Beam = Icy Wind)",
      not any(key in shop_tm_keys for key in tms._load()["type_based"] if key != "tm_aurora_beam"))
check("Neue Karmesin/Purpur-TMs ohne Arena sind kaeuflich (Crunch, Draco Meteor)",
      {"tm_crunch", "tm_draco_meteor"} <= shop_tm_keys)
check("Status-Attacken haben einen Preis (Dragon Dance)",
      (by_key["tm_dragon_dance"]["price"] or 0) > 0)


# Laufender Kampf sperrt das Beibringen (die Attackenauswahl ist dann fix).
db.execute(
    "INSERT INTO trainer_battle_session (user_id, trainer_id, status, state, created_at, "
    "updated_at) VALUES (?,?,?,?,?,?)",
    ("alice", "kanto_brock", "ongoing", "{}", "2026-01-01T00:00:00+00:00",
     "2026-01-01T00:00:00+00:00"),
)
ace_tm = "tm_thunder_punch"
rewards.grant("alice", [{"key": ace_tm, "quantity": 1}])
expect_error("Waehrend eines Kampfes wird nicht gelernt",
             tms.teach, "alice", pika_id, ace_tm)
db.execute("UPDATE trainer_battle_session SET status = 'done' WHERE user_id = 'alice'", ())
check("Nach dem Kampf geht es wieder",
      tms.teach("alice", pika_id, ace_tm)["ok"] is True)


# ------------------------------------------- Arena-Uebersicht (Liga-Tab)
overview = trainer_battle.overview("bob")
kanto_region = next(r for r in overview["regions"] if r["key"] == "kanto")
by_id = {t["id"]: t for t in kanto_region["trainers"]}
check("Liga-Uebersicht nennt die Belohnung fuer den ersten Sieg",
      by_id["kanto_misty"]["reward"]["label"].startswith("3× "),
      by_id["kanto_misty"]["reward"])
check("Top Vier hat dort keine Item-Belohnung", by_id["kanto_lorelei"]["reward"] is None)
check("Liga-Uebersicht nennt die Belohnung der ganzen Region",
      kanto_region["reward"]["items"] == [{"key": rewards.MASTER_BALL, "quantity": 1}],
      kanto_region["reward"])
check("Liga-Uebersicht meldet, ob die Region schon komplett ist",
      kanto_region["cleared"] is False)


# ------------------------------------ PokeAPI-Lernlisten (species.tm_moves)
import asyncio  # noqa: E402

FAKE_DOC = {"moves": [
    {"move": {"name": "body-slam"}, "version_group_details": [
        {"move_learn_method": {"name": "level-up"}}, {"move_learn_method": {"name": "machine"}}]},
    {"move": {"name": "surf"}, "version_group_details": [{"move_learn_method": {"name": "machine"}}]},
    {"move": {"name": "thunder-shock"}, "version_group_details": [
        {"move_learn_method": {"name": "level-up"}}]},
    {"move": {"name": "razor-leaf"}, "version_group_details": [{"move_learn_method": {"name": "egg"}}]},
]}
check("Nur Lernmethode 'machine' zaehlt, Bindestriche werden zu Unterstrichen",
      seed.tm_moves_from_pokemon_doc(FAKE_DOC) == ["body_slam", "surf"],
      seed.tm_moves_from_pokemon_doc(FAKE_DOC))
check("Dokument ohne Attacken ergibt eine leere Liste", seed.tm_moves_from_pokemon_doc({}) == [])

check("Spalte species.tm_moves existiert (Migration)",
      "tm_moves" in {r["name"] for r in db.query("PRAGMA table_info(species)")})
db.init_db()  # zweiter Lauf: Migration ist idempotent
check("Fallback-Pokédex hat noch keine Lernliste (NULL -> Typ-Naeherung)", pika["tm_moves"] is None)
check("Ohne Lernliste greift die Typ-Naeherung", tms.learnable_moves(pika) is None
      and tms.compatible(pika, brick_tm))

real_pika = {**pika, "tm_moves": json.dumps(["surf", "thunderbolt"])}
# PokeAPI's any-game TM list only decides for moves that are no TM in the
# species' learnset generation (Scarlet/Violet) - Submission is none there.
old_tm = "tm_submission"
check("Testaufbau: Submission ist keine Karmesin/Purpur-TM",
      not moves_mod.is_machine_move("submission", 9))
check("Mit Lernliste entscheidet PokeAPI, nicht der Typ (Pikachu lernt Surfer)",
      tms.compatible(real_pika, surf_tm))
check("Mit Lernliste: was nicht drinsteht, ist nicht lernbar (auch wenn der Typ passt)",
      not tms.compatible(real_pika, old_tm)
      and not tms.compatible({**real_pika, "type1": "fighting"}, old_tm))
check("Mit Lernliste: auch Mythische lernen nur, was in der Liste steht",
      not tms.compatible({**real_pika, "is_mythical": 1}, old_tm))
check("Karmesin/Purpur-TMs folgen der Gen-9-Lernliste, nicht PokeAPI (Pikachu: Karateschlag)",
      tms.compatible(real_pika, brick_tm))
check("Leere Lernliste = lernt keine TM (ausser Ersatz-TMs)",
      not any(tms.compatible({**karp, "tm_moves": "[]"}, key) for key in catalog
              if key not in tms._load()["type_based"]))
check("Kaputte Lernliste faellt auf die Typ-Naeherung zurueck",
      tms.compatible({**pika, "tm_moves": "{nope"}, brick_tm))
check("Ersatz-TMs (kein Haupt-TM) sind Katalog-TMs",
      set(tms._load()["type_based"]) <= set(catalog))
grass = {**sp(type1="grass"), "tm_moves": "[]"}
check("Ersatz-TMs folgen trotz Lernliste der Typ-Naeherung",
      tms.compatible(grass, "tm_razor_leaf")
      and not tms.compatible({**sp(type1="fire"), "tm_moves": "[]"}, "tm_razor_leaf"))

# Ein Pokémon mit Lernliste bekommt nur TMs aus der Liste angeboten.
db.execute("UPDATE species SET tm_moves = ? WHERE id = ?", (json.dumps(["surf"]), pika["id"]))
pika_listed = db.query_one("SELECT * FROM species WHERE id = ?", (pika["id"],))
check("Lernliste steht in der DB", tms.learnable_moves(pika_listed) == {"surf"})
pika_owned = add_pokemon("bob", pika_listed, 60, party_slot=None)
pika_owned_row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (pika_owned,))
check("teachable folgt der DB-Lernliste",
      tms.teachable(pika_owned_row, surf_tm) and not tms.teachable(pika_owned_row, old_tm))
# Erneutes Seeden ueberschreibt die Lernliste nicht.
seed.seed_from_fallback()
check("Fallback-Seed laesst vorhandene Lernlisten unangetastet",
      db.query_one("SELECT tm_moves FROM species WHERE id = ?", (pika["id"],))["tm_moves"]
      == json.dumps(["surf"]))

# Nachtrag: ein Fake-Client statt Netzwerk.
class _FakeResponse:
    def __init__(self, doc): self.doc = doc
    def raise_for_status(self): pass
    def json(self): return self.doc


class _FakeClient:
    def __init__(self, *args, **kwargs): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    async def get(self, url):
        dex_id = int(url.rsplit("/", 1)[-1])
        return _FakeResponse({"moves": []} if dex_id == karp["id"] else
                             {"error": "x"} if dex_id == pika["id"] else FAKE_DOC)


async def _reachable() -> bool: return True


db.execute("UPDATE species SET tm_moves = NULL")
real_client, real_reachable = tms.httpx.AsyncClient, tms.seed.reachable
tms.httpx.AsyncClient, tms.seed.reachable = _FakeClient, _reachable
try:
    total = db.query_one("SELECT COUNT(*) AS n FROM species")["n"]
    updated = asyncio.run(tms.backfill())
finally:
    tms.httpx.AsyncClient, tms.seed.reachable = real_client, real_reachable
check("Nachtrag traegt alle Arten mit gueltiger Antwort nach", updated == total - 1, (updated, total))
check("Antwort ohne 'moves' laesst die Art auf NULL (naechster Start versucht es erneut)",
      db.query_one("SELECT tm_moves FROM species WHERE id = ?", (pika["id"],))["tm_moves"] is None)
check("Art ohne TM-Attacken bekommt eine leere Liste statt NULL",
      db.query_one("SELECT tm_moves FROM species WHERE id = ?", (karp["id"],))["tm_moves"] == "[]")
bulba_row = db.query_one("SELECT tm_moves FROM species WHERE name = 'bulbasaur'")
check("Nachtrag speichert die Lernliste als JSON",
      json.loads(bulba_row["tm_moves"]) == ["body_slam", "surf"], bulba_row)
check("Nach dem Nachtrag fehlt nur noch die fehlgeschlagene Art",
      tms._species_missing_learnset() == [pika["id"]])
db.execute("UPDATE species SET tm_moves = NULL")


# ------------------------------------------------------------------ i18n
KEYS = ["item.tm_label", "item.tm_description", "item_category.tm", "move.via_tm",
        "reward.item_count", "tm.result", "tm.result_not_selected", "tm.bag.none_can_use",
        "error.tm_level_too_low", "error.tm_already_known", "error.tm_incompatible",
        "league.first_win_reward", "league.region_reward",
        "guide.tms.title", "guide.tms.p1", "guide.tms.p2"]
for lang in i18n.available_languages():
    catalog_lang = json.loads(
        (ROOT / "pokemon_encounter/app/data/i18n" / f"{lang}.json").read_text(encoding="utf-8"))
    missing = [key for key in KEYS if key not in catalog_lang]
    check(f"i18n {lang} kennt alle TM-Texte", not missing, missing)


print()
if failures:
    print(f"{len(failures)} FAIL: " + ", ".join(failures))
    sys.exit(1)
print("Alle TM-Tests ok.")
