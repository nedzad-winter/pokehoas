"""Tests fuer Level-Entwicklungen (im Pokémon-Menue ausgeloest, nie
automatisch) und die Basiswerte in der Pokémon-Detailansicht.

Aufruf:  python3 tests/test_evolution.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-evolution-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, db, evolution, healing, i18n, seed  # noqa: E402
from app.api import team as team_api  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


def expect_http(label: str, status: int, fn, *args) -> None:
    try:
        fn(*args)
        check(label, False, "keine Exception")
    except HTTPException as exc:
        check(label, exc.status_code == status, f"{exc.status_code}: {exc.detail}")


NOW = datetime.now(timezone.utc).isoformat()
IVS = {k: 10 for k in ("hp", "atk", "def", "spa", "spd", "spe")}

# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()
evolution.seed_from_fallback()

# Zielarten, die der Fallback-Pokédex nicht hat.
for rec in (
    {"id": 5, "name": "charmeleon", "name_de": "Glutexo", "name_en": "Charmeleon", "type1": "fire",
     "catch_rate": 45, "base_exp": 142, "growth_rate": "medium_slow", "base_hp": 58,
     "base_atk": 64, "base_def": 58, "base_spa": 80, "base_spd": 65, "base_spe": 80,
     "evolves_from": 4},
    {"id": 8, "name": "wartortle", "name_de": "Schillok", "name_en": "Wartortle", "type1": "water",
     "catch_rate": 45, "base_exp": 142, "growth_rate": "medium_slow", "base_hp": 59,
     "base_atk": 63, "base_def": 80, "base_spa": 65, "base_spd": 80, "base_spe": 58,
     "evolves_from": 7},
    {"id": 9, "name": "blastoise", "name_de": "Turtok", "name_en": "Blastoise", "type1": "water",
     "catch_rate": 45, "base_exp": 239, "growth_rate": "medium_slow", "base_hp": 79,
     "base_atk": 83, "base_def": 100, "base_spa": 85, "base_spd": 105, "base_spe": 78,
     "evolves_from": 8},
    {"id": 26, "name": "raichu", "name_de": "Raichu", "name_en": "Raichu", "type1": "electric",
     "catch_rate": 75, "base_exp": 218, "growth_rate": "medium_fast", "base_hp": 60,
     "base_atk": 90, "base_def": 55, "base_spa": 90, "base_spd": 80, "base_spe": 110,
     "evolves_from": 25},
    {"id": 290, "name": "nincada", "name_de": "Protagoni", "name_en": "Nincada", "type1": "bug",
     "catch_rate": 255, "base_exp": 53, "growth_rate": "medium_fast", "base_hp": 31,
     "base_atk": 45, "base_def": 90, "base_spa": 30, "base_spd": 30, "base_spe": 40},
    {"id": 291, "name": "ninjask", "name_de": "Ninjasky", "name_en": "Ninjask", "type1": "bug",
     "catch_rate": 120, "base_exp": 160, "growth_rate": "medium_fast", "base_hp": 61,
     "base_atk": 90, "base_def": 45, "base_spa": 50, "base_spd": 50, "base_spe": 160,
     "evolves_from": 290},
    {"id": 292, "name": "shedinja", "name_de": "Ninjadow", "name_en": "Shedinja", "type1": "bug",
     "catch_rate": 45, "base_exp": 170, "growth_rate": "medium_fast", "base_hp": 1,
     "base_atk": 90, "base_def": 45, "base_spa": 30, "base_spd": 30, "base_spe": 40,
     "evolves_from": 290},
):
    rec.setdefault("generation", seed.generation_for(rec["id"]))
    seed._upsert(rec)

# Glumanda -> Glutexo ab Lv. 16 (wie in der PokeAPI). Schiggy bekommt zwei
# erfundene Level-Ziele mit Tageszeit-Bedingung, um die Auswahl zu testen.
evolution._write_rows([
    {"from": 4, "to": 5, "trigger": "level-up", "min_level": 16},
    {"from": 7, "to": 8, "trigger": "level-up", "min_level": 16, "detail": {"time_of_day": "day"}},
    {"from": 7, "to": 9, "trigger": "level-up", "min_level": 16, "detail": {"time_of_day": "night"}},
    {"from": 1, "to": 9, "trigger": "level-up", "min_level": 10, "detail": {"held_item": "leftovers"}},
    {"from": 25, "to": 5, "trigger": "level-up", "min_level": 10, "detail": {"gender": "female"}},
    # Level-Entwicklung ohne min_level (z.B. Freundschaft) - nicht unterstuetzt.
    {"from": 129, "to": 5, "trigger": "level-up", "min_level": None},
    # Nincada -> Ninjask UND Shedinja, beide Lv. 20 (wie in der PokeAPI) -
    # Shedinja ist nie eine waehlbare Alternative, siehe evolution.py.
    {"from": 290, "to": 291, "trigger": "level-up", "min_level": 20},
    {"from": 290, "to": 292, "trigger": "level-up", "min_level": 20},
], "pokeapi", overwrite=True)

alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")


def add_owned(user_id: str, species_id: int, level: int, gender: str = "male",
              held_item: str | None = None, party_slot: int | None = None) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "gender, held_item, party_slot, caught_at) VALUES (?,?,?,?,0,?,?,?,?,?,?)",
        (user_id, species_id, level, battle.exp_for_level(level, "medium_slow"), json.dumps(IVS),
         "hardy", gender, held_item, party_slot, NOW),
    )


def owned(owned_id: int) -> dict:
    return db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))


# ------------------------------------------------- Welche Entwicklungen?
low = add_owned("alice", 4, 15, party_slot=1)
check("Lv. 15: keine Level-Entwicklung", evolution.level_evolutions(owned(low)) == [])
db.execute("UPDATE owned_pokemon SET level = 16 WHERE id = ?", (low,))
evos = evolution.level_evolutions(owned(low))
check("Lv. 16: Glumanda kann zu Glutexo", [e["to_species_id"] for e in evos] == [5], evos)
check("Ziel hat Namen und Mindestlevel",
      evos and evos[0]["to_name"] == "Charmeleon" and evos[0]["to_name_de"] == "Glutexo"
      and evos[0]["min_level"] == 16, evos)

squirtle = add_owned("alice", 7, 20)
check("Tageszeit: tagsueber nur Schillok",
      [e["to_species_id"] for e in evolution.level_evolutions(owned(squirtle), "day")] == [8])
check("Tageszeit: nachts nur Turtok",
      [e["to_species_id"] for e in evolution.level_evolutions(owned(squirtle), "night")] == [9])
check("current_time_of_day: 12 Uhr = day, 23 Uhr = night",
      evolution.current_time_of_day(datetime(2026, 1, 1, 12)) == "day"
      and evolution.current_time_of_day(datetime(2026, 1, 1, 23)) == "night")

bulba = add_owned("alice", 1, 12)
check("Getragenes Item: ohne Item keine Entwicklung", evolution.level_evolutions(owned(bulba)) == [])
db.execute("UPDATE owned_pokemon SET held_item = 'leftovers' WHERE id = ?", (bulba,))
check("Getragenes Item: mit Item moeglich",
      [e["to_species_id"] for e in evolution.level_evolutions(owned(bulba))] == [9])

pika_m = add_owned("alice", 25, 12, gender="male")
pika_f = add_owned("alice", 25, 12, gender="female")
check("Geschlecht: maennlich nein", evolution.level_evolutions(owned(pika_m)) == [])
check("Geschlecht: weiblich ja", len(evolution.level_evolutions(owned(pika_f))) == 1)

karp = add_owned("alice", 129, 50)
check("Level-Entwicklung ohne min_level wird nicht angeboten",
      evolution.level_evolutions(owned(karp)) == [])

# ------------------------------------------------ Detailansicht / Team
detail = team_api.get_pokemon(low, alice)
check("Detailansicht: level_evolutions", [e["to_species_id"] for e in detail["level_evolutions"]] == [5])
charmander = db.query_one("SELECT * FROM species WHERE id = 4")
check("Detailansicht: Basiswerte der Art",
      detail["species"]["base_stats"] == {
          "hp": charmander["base_hp"], "atk": charmander["base_atk"],
          "def": charmander["base_def"], "spa": charmander["base_spa"],
          "spd": charmander["base_spd"], "spe": charmander["base_spe"]},
      detail["species"].get("base_stats"))
check("Detailansicht: eigene Werte (Basiswert+IV+Level), nicht die Basiswerte der Art",
      detail["stats"] == {
          "hp": battle.stat_hp(charmander["base_hp"], IVS["hp"], detail["level"]),
          "atk": battle.stat_other(charmander["base_atk"], IVS["atk"], detail["level"]),
          "def": battle.stat_other(charmander["base_def"], IVS["def"], detail["level"]),
          "spa": battle.stat_other(charmander["base_spa"], IVS["spa"], detail["level"]),
          "spd": battle.stat_other(charmander["base_spd"], IVS["spd"], detail["level"]),
          "spe": battle.stat_other(charmander["base_spe"], IVS["spe"], detail["level"]),
      },
      detail.get("stats"))
check("Eigene Werte unterscheiden sich von den Basiswerten (IV/Level fliessen ein, Vergleich zweier Pokémon)",
      detail["stats"] != detail["species"]["base_stats"])
check("stats.hp stimmt mit max_hp ueberein", detail["stats"]["hp"] == detail["max_hp"])
team = team_api.get_team(alice)
in_party = next(p for p in team["party"] if p["id"] == low)
check("Team-Liste: Entwicklung als Hinweis sichtbar", len(in_party["level_evolutions"]) == 1)
check("Team-Liste: ohne Entwicklung leer",
      next(p for p in team["box"] if p["id"] == karp)["level_evolutions"] == [])

# ------------------------------------------ Keine Auto-Entwicklung im Kampf
fresh = add_owned("alice", 4, 15)
member = battle.combatant_from_owned(owned(fresh))
need = battle.exp_for_level(16, "medium_slow") - owned(fresh)["exp"]
log: list[str] = []
battle.grant_exp(log, [member], {0: need + 1}, "en")
after = owned(fresh)
check("Kampf-EXP: Level 16 erreicht", after["level"] == 16, after["level"])
check("Kampf-EXP: KEINE automatische Entwicklung", after["species_id"] == 4, after["species_id"])
hint = i18n.t("en", "battle.log.can_evolve", name=member.name)
check("Kampf-Log weist auf moegliche Entwicklung hin", hint in log, log)
log = []
battle.grant_exp(log, [battle.combatant_from_owned(owned(karp))], {0: 1}, "en")
check("Kein Hinweis ohne Level-Aufstieg / Entwicklung",
      not any("evolve" in line for line in log), log)

# ------------------------------------------------------------- Entwickeln
expect_http("Fremdes Pokémon -> 403", 403, team_api.evolve, low, team_api.EvolveIn(), bob)
expect_http("Zu niedriges Level -> 400", 400, team_api.evolve,
            add_owned("alice", 4, 10), team_api.EvolveIn(), alice)
expect_http("Falsches Ziel -> 400", 400, team_api.evolve, low,
            team_api.EvolveIn(to_species_id=9), alice)

# Laufender Kampf sperrt (wie alle Team-Aenderungen).
orig_has_battle = healing.has_ongoing_battle
healing.has_ongoing_battle = lambda user_id: True
expect_http("Im Kampf gesperrt -> 409", 409, team_api.evolve, low, team_api.EvolveIn(), alice)
healing.has_ongoing_battle = orig_has_battle
check("Gesperrter Versuch aendert nichts", owned(low)["species_id"] == 4)

db.execute("UPDATE owned_pokemon SET current_hp = 10 WHERE id = ?", (low,))
old_max = battle.stat_hp(charmander["base_hp"], 10, 16)
res = team_api.evolve(low, None, alice)
row = owned(low)
new_max = battle.stat_hp(58, 10, 16)
check("Entwicklung: Art gewechselt", row["species_id"] == 5 and res["to"]["id"] == 5, res["to"])
check("Entwicklung: Level/EXP unveraendert", row["level"] == 16)
check("Entwicklung: KP-Anteil bleibt",
      abs(row["current_hp"] / new_max - 10 / old_max) < 0.05, f"{row['current_hp']}/{new_max}")
check("Entwicklung: kein Item verbraucht", res["item_key"] is None and res["trigger"] == "level-up")
check("Entwicklung: Pokédex-Eintrag gefangen", battle.species_is_known(5, "alice"))
check("Entwicklung: Antwort enthaelt neue Detailansicht",
      res["pokemon"]["species"]["id"] == 5 and res["pokemon"]["level_evolutions"] == [])
expect_http("Zweites Entwickeln -> 400", 400, team_api.evolve, low, team_api.EvolveIn(), alice)

# Mehrere Ziele: ohne Auswahl abgelehnt, mit Auswahl ok.
orig_tod = evolution.current_time_of_day
evolution.current_time_of_day = lambda now=None: "day"
db.execute("INSERT INTO evolution (from_species_id, to_species_id, trigger_kind, min_level) "
           "VALUES (7, 5, 'level-up', 18)")
expect_http("Mehrere Ziele ohne Auswahl -> 400", 400, team_api.evolve,
            squirtle, team_api.EvolveIn(), alice)
res = team_api.evolve(squirtle, team_api.EvolveIn(to_species_id=8), alice)
check("Mehrere Ziele: gewaehltes Ziel", owned(squirtle)["species_id"] == 8, res["to"])
evolution.current_time_of_day = orig_tod

# ------------------------------------------- Nincada -> Ninjask + Shedinja
# Shedinja ist nie eine waehlbare zweite Entwicklung - Nincada wird immer zu
# Ninjask; Shedinja kommt zusaetzlich dazu, aber NUR wenn im Team noch ein
# Platz frei ist (sonst ueberhaupt nicht, siehe evolution.py). `low` belegt
# bereits Platz 1 (siehe oben), daher Plaetze 2-6 fuer den "Team voll"-Fall.
check("Nincada: nur Ninjask als Ziel (Shedinja nicht waehlbar)",
      [e["to_species_id"] for e in evolution.level_evolutions(owned(
          add_owned("alice", 290, 20)))] == [291])

# Team komplett voll (6/6) -> Nincada (in der Box) wird zu Ninjask, aber
# Shedinja hat keinen Platz.
for slot in range(2, 7):
    add_owned("alice", 129, 5, party_slot=slot)
nincada_full = add_owned("alice", 290, 20)  # bleibt in der Box
before_count = db.query_one("SELECT COUNT(*) AS n FROM owned_pokemon WHERE user_id = 'alice'")["n"]
res = evolution.evolve_by_level("alice", nincada_full)
check("Nincada (Team voll): wird zu Ninjask", owned(nincada_full)["species_id"] == 291, res["to"])
check("Nincada (Team voll): kein Shedinja", res.get("shedinja") is None, res)
after_count = db.query_one("SELECT COUNT(*) AS n FROM owned_pokemon WHERE user_id = 'alice'")["n"]
check("Nincada (Team voll): keine neue Pokemon-Zeile angelegt", after_count == before_count,
      (before_count, after_count))

# Genau EIN Teamplatz wird frei (wie im Bug-Report) -> ein zweites Nincada
# bringt zusaetzlich ein Shedinja auf genau diesem Platz mit.
freed = db.query_one(
    "SELECT id FROM owned_pokemon WHERE user_id = 'alice' AND species_id = 129 "
    "AND party_slot IS NOT NULL LIMIT 1"
)
db.execute("UPDATE owned_pokemon SET party_slot = NULL WHERE id = ?", (freed["id"],))
nincada_second = add_owned("alice", 290, 20)  # ebenfalls in der Box
res = evolution.evolve_by_level("alice", nincada_second)
check("Nincada (ein freier Platz): wird zu Ninjask", owned(nincada_second)["species_id"] == 291, res["to"])
check("Nincada (ein freier Platz): Shedinja erscheint", res.get("shedinja") is not None, res)
shedinja_row = owned(res["shedinja"]["owned_id"]) if res.get("shedinja") else None
check("Shedinja: eigene Pokemon-Zeile im Team, gleiches Level",
      shedinja_row is not None and shedinja_row["species_id"] == 292
      and shedinja_row["party_slot"] is not None and shedinja_row["level"] == 20, shedinja_row)
check("Shedinja: geschlechtslos", shedinja_row is not None and shedinja_row["gender"] is None, shedinja_row)
check("Shedinja: immer genau 1 KP, Formel wird ignoriert",
      res["shedinja"]["max_hp"] == 1, res["shedinja"])
check("Shedinja: im Pokedex als gefangen vermerkt", battle.species_is_known(292, "alice"))

# Der Platz ist jetzt wieder belegt -> ein drittes Nincada bekommt kein
# weiteres Shedinja mehr.
nincada_third = add_owned("alice", 290, 20)
res = evolution.evolve_by_level("alice", nincada_third)
check("Drittes Nincada: kein Shedinja mehr (Platz erneut belegt)", res.get("shedinja") is None, res)

# Item-Entwicklung bleibt unberuehrt: Pikachu + Donnerstein ist KEINE
# Level-Entwicklung und wird im Menue nicht angeboten.
raichu_ok = db.query_one("SELECT 1 AS x FROM evolution WHERE from_species_id = 25 "
                         "AND item_key = 'thunder_stone'") is not None
check("Item-Entwicklung (Donnerstein) weiterhin vorhanden", raichu_ok)
check("Item-Entwicklungen tauchen nicht als Level-Entwicklung auf",
      all(e["to_species_id"] != 26 for e in evolution.level_evolutions(owned(pika_m))))

# ------------------------------------------- Item-Entwicklung im Pokémon-Menue
# Feature: wie bei Level-Entwicklungen soll das Pokémon-Menue (und die
# Team-Uebersicht) eine moegliche Item-Entwicklung anzeigen, sobald das
# passende Item im Beutel liegt - Anwenden bleibt weiterhin ein bewusster
# Schritt (POST /pokemon/{id}/use-item), nie automatisch.
check("Ohne Donnerstein im Beutel: keine Item-Entwicklung angeboten",
      evolution.item_evolutions(owned(pika_m), "alice") == [])
db.execute(
    "INSERT INTO inventory (item_key, user_id, quantity) VALUES ('thunder_stone', 'alice', 1) "
    "ON CONFLICT(item_key, user_id) DO UPDATE SET quantity = excluded.quantity"
)
item_evos = evolution.item_evolutions(owned(pika_m), "alice")
check("Mit Donnerstein im Beutel: Pikachu kann zu Raichu",
      [e["to_species_id"] for e in item_evos] == [26], item_evos)
check("Item-Entwicklung nennt das Item", item_evos and item_evos[0]["item_key"] == "thunder_stone", item_evos)
check("Item-Entwicklung fuer Bob (kein Donnerstein im Beutel) leer",
      evolution.item_evolutions(owned(pika_f), "bob") == [])

detail = team_api.get_pokemon(pika_m, alice)
check("Detailansicht: item_evolutions",
      [e["to_species_id"] for e in detail["item_evolutions"]] == [26], detail.get("item_evolutions"))
team = team_api.get_team(alice)
in_party_pika = next((p for p in team["party"] if p["id"] == pika_m), None)
in_box_pika = next((p for p in team["box"] if p["id"] == pika_m), None)
pika_in_team = in_party_pika or in_box_pika
check("Team-Liste: Item-Entwicklung als Hinweis sichtbar", len(pika_in_team["item_evolutions"]) == 1)

res = evolution.use_item("alice", pika_m, "thunder_stone")
check("Item-Entwicklung ueber use_item: Art gewechselt", owned(pika_m)["species_id"] == 26, res["to"])
check("Item-Entwicklung: Stein verbraucht",
      db.query_one("SELECT quantity FROM inventory WHERE item_key = 'thunder_stone' AND user_id = 'alice'")["quantity"] == 0)
check("Nach Entwicklung: keine Item-Entwicklung mehr angeboten (kein Stein mehr im Beutel)",
      evolution.item_evolutions(owned(pika_m), "alice") == [])

# ------------------------------------------------------------------- HTTP
# Endpunkt ueber FastAPI: Body ist optional (ein Ziel), oder mit Ziel-Art.
client = TestClient(app)  # ohne `with` -> kein Lifespan (Poller/Seeding)
logging.getLogger("httpx").setLevel(logging.WARNING)
HEADERS = {"X-Remote-User-Id": "alice", "X-Remote-User-Name": "Alice"}
via_http = add_owned("alice", 4, 30)
r = client.post(f"/api/team/{via_http}/evolve", headers=HEADERS)
check("POST /team/{id}/evolve ohne Body", r.status_code == 200 and owned(via_http)["species_id"] == 5,
      f"{r.status_code} {r.text[:200]}")
via_http = add_owned("alice", 4, 30)
r = client.post(f"/api/team/{via_http}/evolve", headers=HEADERS, json={"to_species_id": 5})
check("POST /team/{id}/evolve mit Ziel", r.status_code == 200 and r.json()["to"]["id"] == 5,
      f"{r.status_code} {r.text[:200]}")
r = client.post(f"/api/team/{via_http}/evolve", headers=HEADERS, json={})
check("POST /team/{id}/evolve ohne moegliche Entwicklung -> 400", r.status_code == 400, r.status_code)

# ------------------------------------------------- Box: Stufe + Sammel-Entwicklung
check("Entwicklungsstufe: Glumanda = 1", evolution.stage_of(4) == 1, evolution.stage_of(4))
check("Entwicklungsstufe: Glutexo = 2", evolution.stage_of(5) == 2, evolution.stage_of(5))
check("Entwicklungsstufe: Turtok = 3", evolution.stage_of(9) == 3, evolution.stage_of(9))

box_charm = add_owned("alice", 4, 20)        # genau ein Level-Ziel -> entwickelt
box_squirtle = add_owned("alice", 7, 20)     # zwei Ziele (Tageszeit-Ziel + 7 -> 5 von oben) -> uebersprungen
box_bulba = add_owned("alice", 1, 20)        # braucht getragenes Item -> uebersprungen
box_low = add_owned("alice", 4, 5)           # Level zu niedrig -> uebersprungen
r = client.get("/api/team", headers=HEADERS)
box_entry = next(p for p in r.json()["box"] if p["id"] == box_charm)
check("GET /team: Box-Eintrag hat Stufe und Seltenheit",
      box_entry["species"]["stage"] == 1 and box_entry["species"]["rarity_tier"] == "rare",
      box_entry["species"])
r = client.post("/api/team/evolve-many", headers=HEADERS,
                json={"ids": [box_charm, box_bulba, box_low, box_charm]})
body = r.json()
check("POST /team/evolve-many: entwickelt nur eindeutige Level-Entwicklungen",
      r.status_code == 200 and [e["owned_id"] for e in body["evolved"]] == [box_charm]
      and owned(box_charm)["species_id"] == 5, f"{r.status_code} {r.text[:300]}")
check("POST /team/evolve-many: Rest uebersprungen, doppelte IDs ignoriert",
      body.get("skipped") == [box_bulba, box_low] and owned(box_bulba)["species_id"] == 1
      and owned(box_low)["species_id"] == 4, body.get("skipped"))
res = evolution.evolve_many_by_level("alice", [box_squirtle])
check("Sammel-Entwicklung: mehrere Ziele -> uebersprungen, keine Wahl getroffen",
      res["evolved"] == [] and res["skipped"] == [box_squirtle]
      and owned(box_squirtle)["species_id"] == 7, res)
r = client.post("/api/team/evolve-many", headers=HEADERS, json={"ids": []})
check("POST /team/evolve-many ohne Auswahl -> 400", r.status_code == 400, r.status_code)
bob_mon = add_owned("bob", 4, 20)
r = client.post("/api/team/evolve-many", headers=HEADERS, json={"ids": [bob_mon]})
check("POST /team/evolve-many mit fremdem Pokemon -> 403, nichts entwickelt",
      r.status_code == 403 and owned(bob_mon)["species_id"] == 4, r.status_code)

# -------------------------------------------------------------------- i18n
en = json.loads((ROOT / "pokemon_encounter/app/data/i18n/en.json").read_text(encoding="utf-8"))
de = json.loads((ROOT / "pokemon_encounter/app/data/i18n/de.json").read_text(encoding="utf-8"))
new_keys = ["battle.log.can_evolve", "error.cannot_evolve_now", "error.evolution_choose_target",
            "evolution.badge", "evolution.confirm", "evolution.evolve_into", "evolution.evolve_with_item",
            "evolution.evolved", "evolution.ready", "evolution.ready_hint", "evolution.ready_hint_item",
            "pokedex.base_stats_total", "team.own_stats", "error.evolve_none_selected",
            "team.box_all_rarities", "team.box_all_stages", "team.box_can_evolve",
            "team.box_evolve_selected", "team.box_evolve_selected_confirm", "team.box_evolve_skipped",
            "team.box_evolved_many", "team.box_stage"]
check("Neue Texte in en und de vorhanden", all(k in en and k in de for k in new_keys),
      [k for k in new_keys if k not in en or k not in de])

# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
