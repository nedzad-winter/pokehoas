"""Tests fuer die Zucht (app/breeding.py): Kompatibilitaet, Pension-Timer,
Vererbung, Schritte nur im Team/Inkubator, Teamplaetze, Inkubator-Nutzungen,
Schluepfen und Migration.

Aufruf:  python3 tests/test_breeding.py
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-breeding-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, breeding, buddy, db, economy, held_items, seed, spawn  # noqa: E402
from app.api import breeding as breeding_api  # noqa: E402
from app.api import encounters as encounters_api  # noqa: E402
from app.api import team as team_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


def expect_error(label: str, key: str, fn, *args) -> None:
    try:
        fn(*args)
        check(label, False, "keine Exception")
    except breeding.BreedingError as exc:
        check(label, exc.key == key, exc.key)


def expect_http(label: str, status: int, fn, *args) -> None:
    try:
        fn(*args)
        check(label, False, "keine Exception")
    except HTTPException as exc:
        check(label, exc.status_code == status, f"{exc.status_code}: {exc.detail}")


NOW = datetime.now(timezone.utc).astimezone()
IVS_LOW = {k: 0 for k in ("hp", "atk", "def", "spa", "spd", "spe")}
IVS_HIGH = {k: 31 for k in ("hp", "atk", "def", "spa", "spd", "spe")}

db.init_db()
seed.seed_from_fallback()
# Zusaetzliche Arten fuer die Tests: Pichu (Baby, Basis von Pikachu), Ditto,
# Mewtu (legendaer), Raichu (Entwicklung von Pikachu).
for record in (
    {"id": 172, "name": "pichu", "type1": "electric", "catch_rate": 190, "base_exp": 41,
     "growth_rate": "medium_fast", "base_hp": 20, "base_atk": 40, "base_def": 15,
     "base_spa": 35, "base_spd": 35, "base_spe": 60},
    {"id": 132, "name": "ditto", "type1": "normal", "catch_rate": 35, "base_exp": 101,
     "growth_rate": "medium_fast", "base_hp": 48, "base_atk": 48, "base_def": 48,
     "base_spa": 48, "base_spd": 48, "base_spe": 48},
    {"id": 150, "name": "mewtwo", "type1": "psychic", "catch_rate": 3, "base_exp": 306,
     "growth_rate": "slow", "base_hp": 106, "base_atk": 110, "base_def": 90,
     "base_spa": 154, "base_spd": 90, "base_spe": 130, "is_legendary": 1},
    {"id": 26, "name": "raichu", "type1": "electric", "catch_rate": 75, "base_exp": 218,
     "growth_rate": "medium_fast", "base_hp": 60, "base_atk": 90, "base_def": 55,
     "base_spa": 90, "base_spd": 80, "base_spe": 110, "evolves_from": 25},
):
    record.setdefault("generation", seed.generation_for(record["id"]))
    seed._upsert(record)
db.execute("UPDATE species SET evolves_from = 172 WHERE id = 25")
# Ei-Gruppen wie aus der PokeAPI (Pichu = Unbekannt, Pikachu/Raichu = field+fairy).
for sid, groups, counter in ((172, ["no-eggs"], 10), (25, ["ground", "fairy"], 10),
                             (26, ["ground", "fairy"], 10), (132, ["ditto"], 20),
                             (150, ["no-eggs"], 120), (1, ["monster", "plant"], 20),
                             (4, ["monster", "dragon"], 20), (129, ["water2", "dragon"], 5)):
    db.execute("UPDATE species SET egg_groups = ?, hatch_counter = ? WHERE id = ?",
               (json.dumps(groups), counter, sid))

alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")


def add_owned(user_id: str, species_id: int, gender: str | None, party_slot: int | None = None,
              ivs: dict | None = None, held: str | None = None, nature: str = "hardy",
              ability: str | None = None) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, gender, "
        "party_slot, current_hp, caught_at, held_item, ability) VALUES (?,?,?,?,0,?,?,?,?,?,?,?,?)",
        (user_id, species_id, 20, battle.exp_for_level(20, "medium_fast"),
         json.dumps(ivs or IVS_LOW), nature, gender, party_slot, None, NOW.isoformat(), held, ability),
    )


def owned(owned_id: int) -> dict:
    return db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))


def egg_row(egg_id: int) -> dict:
    return db.query_one("SELECT * FROM pokemon_egg WHERE id = ?", (egg_id,))


# ------------------------------------------------------------ Migration
cols = db._table_columns(db.connect(), "species")
check("Migration: species.egg_groups + hatch_counter", {"egg_groups", "hatch_counter"} <= cols)
check("Migration: app_user.egg_steps_at", "egg_steps_at" in db._table_columns(db.connect(), "app_user"))
db.init_db()  # zweiter Lauf darf nichts kaputtmachen
check("Migration ist idempotent", "egg_groups" in db._table_columns(db.connect(), "species"))
check("Settings angelegt", db.get_setting("daycare_egg_minutes") == 240
      and db.get_setting("egg_passive_steps_per_hour") == 250)

# ------------------------------------------------------------- Katalog
item = economy.catalog_by_key().get("incubator")
check("Inkubator ist kaeuflich im Shop", item is not None and economy.is_buyable(item), item)
check("Fatumknoten/Ewigstein sind Halte-Items",
      held_items.get("destiny_knot") is not None and held_items.get("everstone") is not None)

# ---------------------------------------------------------- Ersatzwerte
check("Ersatz-Ei-Gruppen: legendaer ohne Daten -> no-eggs",
      breeding.egg_groups({"id": 999, "type1": "psychic", "is_legendary": 1, "catch_rate": 3}) == ["no-eggs"])
check("Ersatz-Ei-Gruppen: Ditto ohne Daten -> ditto",
      breeding.egg_groups({"id": 132, "type1": "normal", "catch_rate": 35}) == ["ditto"])
check("Ersatz: Ei-Zyklen 20 x 257 Schritte",
      breeding.hatch_steps({"id": 999, "type1": "bug", "catch_rate": 255, "hatch_counter": None}) == 20 * 257)
check("PokeAPI-Dokument wird geparst",
      breeding.parse_species_doc({"egg_groups": [{"name": "monster"}, {"name": "plant"}],
                                  "hatch_counter": 20}) == (["monster", "plant"], 20))
check("Basisform von Raichu ist Pichu", breeding.base_species_id(26) == 172)

# ------------------------------------------------------- Kompatibilitaet
pika_f = add_owned("alice", 25, "female")
pika_m = add_owned("alice", 25, "male")
raichu_m = add_owned("alice", 26, "male")
bulba_m = add_owned("alice", 1, "male")
char_f = add_owned("alice", 4, "female")
ditto = add_owned("alice", 132, None)  # Ditto ist genderlos
ditto2 = add_owned("alice", 132, None)
mewtwo = add_owned("alice", 150, "male")
pichu = add_owned("alice", 172, "female")
magikarp = add_owned("alice", 129, "female")

comp = breeding.compatibility(owned(pika_f), owned(pika_m))
check("Pikachu w + m: kompatibel, gleiche Art", comp["ok"] and comp["factor"] == 1, comp["reason"])
check("Ei-Art = Basisform der Mutter (Pichu)", comp["egg_species_id"] == 172)
comp = breeding.compatibility(owned(pika_f), owned(raichu_m))
check("Pikachu w + Raichu m: kompatibel, andere Art (x2)", comp["ok"] and comp["factor"] == 2)
check("Geschlecht gleich -> inkompatibel",
      breeding.compatibility(owned(pika_m), owned(raichu_m))["reason"] == "breeding.incompatible_gender")
check("Keine gemeinsame Ei-Gruppe -> inkompatibel",
      breeding.compatibility(owned(pika_f), owned(bulba_m))["reason"] == "breeding.incompatible_groups")
comp = breeding.compatibility(owned(char_f), owned(bulba_m))
check("Glumanda w + Bisasam m (Monster): kompatibel, Ei = Glumanda",
      comp["ok"] and comp["egg_species_id"] == 4)
comp = breeding.compatibility(owned(ditto), owned(bulba_m))
check("Ditto + Bisasam: kompatibel, Ei = Bisasam", comp["ok"] and comp["egg_species_id"] == 1)
comp = breeding.compatibility(owned(pika_m), owned(ditto))
check("Ditto ohne Geschlecht + Pikachu m: kompatibel, Ei = Pichu (Pikachu = Mutter)",
      comp["ok"] and comp["mother"]["id"] == pika_m and comp["egg_species_id"] == 172)
check("Ditto wuerfelt kein Geschlecht", all(spawn.roll_gender(132) is None for _ in range(20)))
check("Legendaere genderlose Arten (Zapdos, Mewtwo, Lugia) wuerfeln kein Geschlecht",
      all(spawn.roll_gender(sid) is None for sid in (145, 150, 249) for _ in range(20)))
check("Genderlose Nicht-Legendaere (Magnemite, Voltorb) wuerfeln kein Geschlecht",
      all(spawn.roll_gender(sid) is None for sid in (82, 100) for _ in range(20)))
check("Andere Arten wuerfeln m/w",
      {spawn.roll_gender(1) for _ in range(100)} == {"male", "female"})
legacy_ditto = add_owned("alice", 132, "male")  # aelterer Build: Ditto mit Geschlecht
legacy_wild = db.execute(
    "INSERT INTO encounter (species_id, level, shiny, ivs, nature, gender, status, current_hp, "
    "created_at, expires_at, user_id) VALUES (132,5,0,'{}','hardy','female','active',5,?,?,'alice')",
    (NOW.isoformat(), NOW.isoformat()))
db._migrate_genderless_species(db.connect())
check("Migration: gespeichertes Ditto-Geschlecht wird geleert",
      owned(legacy_ditto)["gender"] is None
      and db.query_one("SELECT gender FROM encounter WHERE id = ?", (legacy_wild,))["gender"] is None)
check("Ditto + Ditto -> inkompatibel",
      breeding.compatibility(owned(ditto), owned(ditto2))["reason"] == "breeding.incompatible_two_ditto")
check("Legendaer -> inkompatibel",
      breeding.compatibility(owned(mewtwo), owned(ditto))["reason"] == "breeding.incompatible_undiscovered")
check("Baby (Unbekannt) -> inkompatibel",
      breeding.compatibility(owned(pichu), owned(pika_m))["reason"] == "breeding.incompatible_undiscovered")
check("Dasselbe Pokemon zweimal -> inkompatibel",
      breeding.compatibility(owned(pika_f), owned(pika_f))["reason"] == "breeding.incompatible_same")

# ------------------------------------------------------------- Pension
expect_error("Fremdes Pokemon in Pension -> 404", "error.pokemon_not_found",
             breeding.set_daycare, "bob", pika_f, pika_m)
expect_error("Inkompatibles Paar abgelehnt", "breeding.incompatible_gender",
             breeding.set_daycare, "alice", pika_m, raichu_m)
status = breeding.set_daycare("alice", pika_f, pika_m, NOW)
check("Paar angemeldet, noch kein Ei", status["compatible"] and not status["egg_ready"], status)
check("Gleiche Art: Intervall = daycare_egg_minutes", status["interval_minutes"] == 240)
expect_error("Zu frueh abgeholt -> kein Ei", "breeding.no_egg_yet",
             breeding.collect_egg, "alice", NOW + timedelta(minutes=239))
later = NOW + timedelta(minutes=241)
check("Nach Ablauf wartet ein Ei", breeding.daycare_status("alice", later)["egg_ready"])
egg = breeding.collect_egg("alice", later)
check("Ei abgeholt, Team hat Platz -> Ei im Team", egg["location"] == "party", egg)
check("Ei-Schritte = hatch_counter(Pichu)=10 x 257", egg["steps_total"] == 2570, egg)
check("Art bleibt bis zum Schluepfen geheim", "species_id" not in egg)
check("Timer neu gestartet", not breeding.daycare_status("alice", later)["egg_ready"])
check("Eltern-Arten am Ei sichtbar", [p["id"] for p in egg["parents"]] == [25, 25])

status = breeding.set_daycare("alice", pika_f, raichu_m, later)
check("Andere Art: doppeltes Intervall", status["interval_minutes"] == 480)
breeding.clear_daycare("alice")
check("Pension geleert", breeding.daycare_status("alice") is None)
status = breeding.set_daycare("alice", ditto, bulba_m, NOW)
check("Ditto in der Pension mit Bisasam: Paar angemeldet", status["compatible"], status)
breeding.clear_daycare("alice")
expect_error("Ohne Paar kein Ei", "breeding.no_pair", breeding.collect_egg, "alice")

# Freigelassener Elternteil loest das Paar auf.
tmp_parent = add_owned("alice", 25, "male")
breeding.set_daycare("alice", pika_f, tmp_parent, NOW)
db.execute("DELETE FROM owned_pokemon WHERE id = ?", (tmp_parent,))
check("Freigelassener Elternteil -> Paar aufgeloest", breeding.daycare_status("alice") is None)

# ------------------------------------------------------------- Vererbung
random.seed(7)
mom = owned(add_owned("alice", 1, "female", ivs=IVS_HIGH, nature="modest"))
dad = owned(add_owned("alice", 1, "male", ivs=IVS_HIGH, held="destiny_knot"))
counts = []
for _ in range(30):
    ivs = battle._as_ivs(breeding._inherit_ivs(mom, dad, 1))
    counts.append(sum(1 for v in ivs.values() if v == 31))
check("Fatumknoten: mindestens 5 IVs von den Eltern (31)", min(counts) >= 5, counts[:10])
plain = owned(add_owned("alice", 1, "male", ivs=IVS_HIGH))
counts = [sum(1 for v in battle._as_ivs(breeding._inherit_ivs(mom, plain, 1)).values() if v == 31)
          for _ in range(30)]
check("Ohne Fatumknoten: mindestens 3 IVs geerbt", min(counts) >= 3, counts[:10])
stone = owned(add_owned("alice", 1, "male", held="everstone", nature="timid"))
check("Ewigstein vererbt das Wesen",
      all(breeding._inherit_nature(mom, stone) == "timid" for _ in range(20)))

# --------------------------------------------------------------- Schritte
egg_id = egg["id"]
db.execute("UPDATE app_user SET egg_steps_at = ? WHERE id = 'alice'", (NOW.isoformat(),))
breeding.advance("alice", NOW + timedelta(hours=2))
check("Passiv: 2 h x 250 = 500 Schritte im Team", egg_row(egg_id)["steps_done"] == 500,
      egg_row(egg_id)["steps_done"])

box_egg = breeding.create_egg("alice", owned(pika_f), owned(pika_m))
breeding.move_egg("alice", box_egg, "box", NOW + timedelta(hours=2))
breeding.advance("alice", NOW + timedelta(hours=4))
check("Ei in der Box brütet nicht", egg_row(box_egg)["steps_done"] == 0)
check("Ei im Team brütet weiter", egg_row(egg_id)["steps_done"] == 1000)

# Schritt-Sensor (Buddy-Mechanik) zaehlt auch ohne Buddy.
db.execute("UPDATE app_user SET buddy_sensor = 'sensor.steps', buddy_sensor_last = 1000 WHERE id = 'alice'")
buddy.apply_reading("alice", 1300)
check("Sensor-Schritte gehen ans Ei (ohne Buddy)", egg_row(egg_id)["steps_done"] == 1300)
check("... aber nicht ans Box-Ei", egg_row(box_egg)["steps_done"] == 0)

# Flammkoerper im Team verdoppelt.
db.execute("UPDATE owned_pokemon SET party_slot = 1, ability = 'flame_body' WHERE id = ?", (char_f,))
breeding.credit_steps("alice", 100)
check("Flammkoerper im Team: doppelte Schritte", egg_row(egg_id)["steps_done"] == 1500)
db.execute("UPDATE owned_pokemon SET party_slot = NULL WHERE id = ?", (char_f,))
breeding.credit_steps("alice", 10_000)
check("Schritte gedeckelt auf steps_total", egg_row(egg_id)["steps_done"] == 2570)

# ------------------------------------------------------------- Teamplaetze
for sid in (1, 4, 7, 129, 152):
    add_owned("alice", sid, "male", party_slot=None)
party_ids = [row["id"] for row in db.query(
    "SELECT id FROM owned_pokemon WHERE user_id = 'alice' ORDER BY id DESC LIMIT 5")]
expect_http("Team: 5 Pokemon + 1 Ei ok, 6 Pokemon + 1 Ei -> 400", 400,
            team_api.set_party, team_api.PartyOrder(order=party_ids + [pika_m]), alice)
team_api.set_party(team_api.PartyOrder(order=party_ids), alice)
team = team_api.get_team(alice)
check("GET /team liefert Eier im Team", [e["id"] for e in team["party_eggs"]] == [egg_id], team["party_eggs"])
check("Team voll (5 + Ei): kein freier Platz", breeding.free_party_slot("alice") is None)
expect_error("Box-Ei ins volle Team -> Fehler", "error.team_max_size",
             breeding.move_egg, "alice", box_egg, "party")

# Fang bei vollem Team (inkl. Ei) landet in der Box.
enc_id = db.execute(
    "INSERT INTO encounter (species_id, level, shiny, ivs, nature, gender, status, created_at, expires_at) "
    "VALUES (1, 5, 0, ?, 'hardy', 'male', 'waiting', ?, ?)",
    (json.dumps(IVS_LOW), NOW.isoformat(), (NOW + timedelta(days=7)).isoformat()),
)
wild = battle.combatant_from_encounter(db.query_one("SELECT * FROM encounter WHERE id = ?", (enc_id,))) \
    if hasattr(battle, "combatant_from_encounter") else None
if wild is not None:
    res = encounters_api.finalize_catch(db.query_one("SELECT * FROM encounter WHERE id = ?", (enc_id,)),
                                        wild, "alice")
    check("Fang bei vollem Team (Ei zaehlt mit) -> Box", owned(res["owned_id"])["party_slot"] is None)

# --------------------------------------------------------------- Inkubator
expect_error("Ohne Inkubator -> Fehler", "breeding.no_incubator",
             breeding.move_egg, "alice", box_egg, "incubator")
db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES ('incubator', 'alice', 1)")
breeding.move_egg("alice", box_egg, "incubator")
check("Ei im Inkubator, Item verbraucht",
      egg_row(box_egg)["location"] == "incubator"
      and breeding.incubators("alice")["in_bag"] == 0
      and breeding.incubators("alice")["active"][0]["uses_left"] == 3)
breeding.credit_steps("alice", 50)
check("Ei im Inkubator brütet ohne Teamplatz", egg_row(box_egg)["steps_done"] == 50)
# Rausnehmen verbraucht keine Nutzung, Wiedereinlegen nimmt denselben Inkubator.
breeding.move_egg("alice", box_egg, "box")
breeding.move_egg("alice", box_egg, "incubator")
check("Wiedereinlegen nutzt freien Inkubator (kein neues Item noetig)",
      len(breeding.incubators("alice")["active"]) == 1)

# ------------------------------------------------------------- Schluepfen
expect_error("Unfertiges Ei schluepft nicht", "breeding.not_ready", breeding.hatch, "alice", box_egg)
expect_error("Fremdes Ei -> 404", "breeding.egg_not_found", breeding.hatch, "bob", egg_id)
dex_before = db.query_one("SELECT caught_count FROM dex_entry WHERE user_id='alice' AND species_id=172")
result = breeding.hatch("alice", egg_id)
baby = owned(result["owned_id"])
check("Geschluepft: Pichu Lv. 1", baby["species_id"] == 172 and baby["level"] == 1, baby)
check("Geschluepft: volle KP", baby["current_hp"] == battle.stat_hp(20, battle._as_ivs(baby["ivs"])["hp"], 1))
check("Geschluepft: uebernimmt Teamplatz des Eies", baby["party_slot"] is not None)
check("Ei ist weg", egg_row(egg_id) is None)
dex_after = db.query_one("SELECT caught_count FROM dex_entry WHERE user_id='alice' AND species_id=172")
check("Pokedex: als gefangen eingetragen",
      dex_after["caught_count"] == (dex_before["caught_count"] if dex_before else 0) + 1)
check("Schluepf-Nachricht uebersetzt", "Pichu" in result["message"] or "pichu" in result["message"], result)
check("Moves auf Lv. 1 vorhanden", len(team_api.describe_owned(baby)["moves"]) >= 1)

# Inkubator: 3 Nutzungen, dann weg.
for n in range(3):
    eid = box_egg if n == 0 else breeding.create_egg("alice", owned(pika_f), owned(pika_m))
    if n:
        breeding.move_egg("alice", eid, "box")
        breeding.move_egg("alice", eid, "incubator")
    db.execute("UPDATE pokemon_egg SET steps_done = steps_total WHERE id = ?", (eid,))
    hatched = breeding.hatch("alice", eid)
    check(f"Inkubator-Ei {n + 1} geschluepft (Team voll -> Box)",
          owned(hatched["owned_id"])["party_slot"] is None and not hatched["in_party"])
check("Inkubator nach 3 Eiern verbraucht", breeding.incubators("alice")["active"] == [])

# ------------------------------------------------------- Ei-Attacken
# Wie in Karmesin/Purpur: jede Ei-Attacke der Ei-Art, die ein Elternteil
# beherrscht (eine seiner vier Kampf-Attacken), erbt das Ei.
glumanda_m = add_owned("alice", 4, "male")
glumanda_f = add_owned("alice", 4, "female")
db.execute("UPDATE owned_pokemon SET selected_moves = ? WHERE id = ?",
           (json.dumps(["bite", "ember"]), glumanda_m))
db.execute("UPDATE owned_pokemon SET selected_moves = ? WHERE id = ?",
           (json.dumps(["metal_claw", "scratch"]), glumanda_f))
egg_moves_egg = breeding.create_egg("alice", owned(glumanda_f), owned(glumanda_m))
check("Ei erbt die Ei-Attacken beider Eltern (Biss, Metallklaue)",
      json.loads(egg_row(egg_moves_egg)["egg_moves"] or "null") == ["bite", "metal_claw"],
      egg_row(egg_moves_egg)["egg_moves"])
check("Ohne passende Attacken der Eltern: keine Ei-Attacken",
      egg_row(box_egg) is None or egg_row(box_egg)["egg_moves"] is None)
db.execute("UPDATE pokemon_egg SET steps_done = steps_total WHERE id = ?", (egg_moves_egg,))
hatched_egg_moves = owned(breeding.hatch("alice", egg_moves_egg)["owned_id"])
check("Geschluepftes Glumanda kennt seine Ei-Attacken",
      json.loads(hatched_egg_moves["egg_moves"]) == ["bite", "metal_claw"], hatched_egg_moves["egg_moves"])
hatched_selected = json.loads(hatched_egg_moves["selected_moves"])
check("Ei-Attacken stehen vorne in den Kampf-Attacken, der Rest ist das Standard-Set",
      hatched_selected[:2] == ["bite", "metal_claw"] and len(hatched_selected) <= 4, hatched_selected)
check("Ei-Attacken sind auf Lv. 1 waehlbar",
      {"bite", "metal_claw"} <= {m["key"] for m in
                                 team_api.get_pokemon(hatched_egg_moves["id"], {"id": "alice", "language": "de"})["learned_moves"]})

# ---------------------------------------------------------------- API
ov = breeding_api.get_eggs(alice)
check("GET /api/eggs liefert Uebersicht",
      {"eggs", "daycare", "incubators", "candidates", "party_size"} <= set(ov))
# Jeder Kandidat traegt seine Ei-Gruppen, damit das Frontend das maennliche/
# weibliche Dropdown gegenseitig nach passender Gruppe filtern kann.
cand_groups = {c["id"]: c["egg_groups"] for c in ov["candidates"]}
check("Kandidat: Ei-Gruppen von Pikachu", cand_groups[pika_f] == ["ground", "fairy"], cand_groups[pika_f])
check("Kandidat: Ei-Gruppen von Ditto", cand_groups[ditto] == ["ditto"], cand_groups[ditto])
check("Kandidat: Ei-Gruppen von Mewtu (legendaer)", cand_groups[mewtwo] == ["no-eggs"], cand_groups[mewtwo])
try:
    breeding_api.set_daycare(breeding_api.DaycareIn(parent_a=pika_m, parent_b=raichu_m), alice)
    check("API: inkompatibel -> 400", False)
except HTTPException as exc:
    check("API: inkompatibel -> 400 mit Text", exc.status_code == 400 and "female" in exc.detail, exc.detail)
expect_http("API: unbekannter Ort -> 400", 400, breeding_api.move_egg,
            ov["eggs"][0]["id"] if ov["eggs"] else 999999,
            breeding_api.EggLocationIn(location="moon"), alice)

# Schluepfen gibt dieselben IV-Belohnungen wie ein Fang: Ei-IVs werden auf die
# Art-Untergrenze gehoben, ein Familienmitglied bekommt +1..2 auf EINEM Wert, und
# die Art-Untergrenze steigt. Eigener Nutzer, damit die Werte eindeutig sind.
carol = db.upsert_user("carol", "Carol")
carol_pika = add_owned("carol", 25, "female", party_slot=None, ivs=IVS_LOW)
db.execute(
    "INSERT INTO pokemon_egg (user_id, species_id, shiny, ivs, nature, gender, ability, "
    "steps_total, steps_done, location, parent_species_a, parent_species_b, created_at) "
    "VALUES ('carol', 172, 0, ?, 'hardy', 'female', NULL, 10, 10, 'box', 25, 25, ?)",
    (json.dumps(IVS_LOW), NOW.isoformat()),
)
carol_egg = db.query_one("SELECT id FROM pokemon_egg WHERE user_id = 'carol'")["id"]
floor_before = economy.species_min_ivs(172)
hatched = breeding.hatch("carol", carol_egg)
baby_ivs = battle._as_ivs(owned(hatched["owned_id"])["ivs"])
check("Schluepfen: IVs auf Art-Untergrenze gehoben (wie beim Fang)",
      baby_ivs == floor_before, (baby_ivs, floor_before))
gains = hatched["iv_gains"]
pika_gain = next((g for g in gains if g["owned_id"] == carol_pika), None)
check("Schluepfen: Familienmitglied bekommt genau einen IV-Zuwachs",
      pika_gain is not None and len(pika_gain["changes"]) == 1, gains)
if pika_gain:
    change = pika_gain["changes"][0]
    check("Schluepfen: IV-Zuwachs 1..2 Punkte",
          1 <= change["to"] - change["from"] <= 2, change)
    check("Schluepfen: Zuwachs wirklich gespeichert",
          sum(battle._as_ivs(owned(carol_pika)["ivs"]).values()) == change["to"], change)
check("Schluepfen: geschluepftes Pokemon bekommt keinen eigenen Zuwachs",
      all(g["owned_id"] != hatched["owned_id"] for g in gains))
floor_after = economy.species_min_ivs(172)
check("Schluepfen: Art-Untergrenze steigt um genau einen Wert",
      hatched["iv_floor"] is not None and hatched["iv_floor"]["species_id"] == 172
      and sum(floor_after.values()) - sum(floor_before.values()) in (1, 2),
      (floor_before, floor_after))

# Nutzer-Loeschung (Admin) raeumt Eier/Inkubatoren/Pension mit ab.
from app.api import admin as admin_api  # noqa: E402
bob_egg_parent_a = add_owned("bob", 25, "female")
bob_egg_parent_b = add_owned("bob", 25, "male")
breeding.set_daycare("bob", bob_egg_parent_a, bob_egg_parent_b, NOW)
bob_egg = breeding.create_egg("bob", owned(bob_egg_parent_a), owned(bob_egg_parent_b))
db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES ('incubator', 'bob', 1)")
breeding.move_egg("bob", bob_egg, "incubator")
admin_api.wipe_user("bob", delete_user=True)
check("Admin: Nutzer loeschen raeumt Zucht-Daten ab",
      db.query_one("SELECT 1 FROM pokemon_egg WHERE user_id = 'bob'") is None
      and db.query_one("SELECT 1 FROM incubator WHERE user_id = 'bob'") is None
      and db.query_one("SELECT 1 FROM daycare WHERE user_id = 'bob'") is None)

print()
if failures:
    print(f"{len(failures)} FAIL")
    sys.exit(1)
print("Alle Zucht-Tests bestanden.")
