"""Tests fuer Admin-Erkennung und Admin-Panel (ohne laufendes Home Assistant).

Aufruf:  python3 tests/test_admin.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-admin-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"
# Override-Liste: Name (Gross-/Kleinschreibung egal) und eine User-ID.
os.environ["POKEMON_ADMIN_USERS"] = "Carol\n override-id "

from fastapi.testclient import TestClient  # noqa: E402

from app import achievements, admin, battle, db, encounter_state, rarity, seed, spawn  # noqa: E402
from app.api import admin as admin_api  # noqa: E402
from app.api import users as users_api  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()
client = TestClient(app)  # ohne `with` -> kein Lifespan (Poller/Seeding)
logging.getLogger("httpx").setLevel(logging.WARNING)

HA_USERS = [
    {"id": "alice", "name": "Alice", "username": "alice", "is_owner": False,
     "is_active": True, "group_ids": ["system-admin"]},
    {"id": "bob", "name": "Bob", "username": "bob", "is_owner": False,
     "is_active": True, "group_ids": ["system-users"]},
    {"id": "owner", "name": "Owner", "username": "owner", "is_owner": True,
     "is_active": True, "group_ids": ["system-users"]},
    {"id": "ghost", "name": "Ghost", "username": "ghost", "is_owner": False,
     "is_active": False, "group_ids": ["system-admin"]},
]
fetch_calls = {"n": 0, "fail": False}


async def fake_fetch() -> list[dict]:
    fetch_calls["n"] += 1
    if fetch_calls["fail"]:
        raise RuntimeError("HA nicht erreichbar")
    return [dict(user) for user in HA_USERS]


admin._fetch_ha_users = fake_fetch


def as_user(user_id: str, name: str | None = None) -> dict[str, str]:
    return {"X-Remote-User-Id": user_id, "X-Remote-User-Name": name or user_id}


ALICE = as_user("alice")
BOB = as_user("bob")


def me(headers: dict | None = None) -> dict:
    return client.get("/api/users/me", headers=headers or {}).json()


# ------------------------------------------------------- Admin-Erkennung
check("Option admin_users wird zeilenweise gelesen",
      settings.admin_users == ("Carol", "override-id"), repr(settings.admin_users))
check("HA-Admin (system-admin) ist Admin", me(ALICE)["is_admin"] is True)
check("Normaler HA-Nutzer ist kein Admin", me(BOB)["is_admin"] is False)
check("HA-Besitzer ist Admin", me(as_user("owner"))["is_admin"] is True)
check("Deaktivierter Admin ist kein Admin", me(as_user("ghost"))["is_admin"] is False)
check("Unbekannter Nutzer ist kein Admin", me(as_user("stranger"))["is_admin"] is False)
check("HA-Nutzerliste wird gecacht", fetch_calls["n"] == 1, f"{fetch_calls['n']} Abrufe")

check("Override per Nutzername (case-insensitive)", me(as_user("c-id", "carol"))["is_admin"])
check("Override per User-ID", me(as_user("override-id", "x"))["is_admin"])

admin._cache_at -= admin.CACHE_TTL_SECONDS + 1
me(ALICE)
check("Nach Ablauf der TTL wird neu abgefragt", fetch_calls["n"] == 2)

# HA faellt aus: alte Liste darf noch eine Weile weiterverwendet werden ...
fetch_calls["fail"] = True
admin._cache_at -= admin.CACHE_TTL_SECONDS + 1
check("HA-Ausfall: frische-genug Liste gilt weiter", me(ALICE)["is_admin"] is True)
calls_before = fetch_calls["n"]
me(ALICE)
check("Nach Fehler kein sofortiger Neuversuch (Backoff)", fetch_calls["n"] == calls_before)
# ... ohne jede Liste: fail closed.
admin.clear_cache()
check("HA-Ausfall ohne Cache: kein Admin", me(ALICE)["is_admin"] is False)
fetch_calls["fail"] = False
admin.clear_cache()
check("Nach Erholung wieder Admin", me(ALICE)["is_admin"] is True)

# Lokale Entwicklung (kein SUPERVISOR_TOKEN): Dev-Nutzer ist Admin.
os.environ.pop("SUPERVISOR_TOKEN")
check("Dev-Nutzer ist lokal Admin", me()["is_admin"] is True and me()["id"] == "dev-local")
os.environ["POKEMON_DEV_ADMIN"] = "0"
check("POKEMON_DEV_ADMIN=0 nimmt die Rechte", me()["is_admin"] is False)
os.environ.pop("POKEMON_DEV_ADMIN")
check("Anderer Header-Nutzer ist lokal kein Admin", me(BOB)["is_admin"] is False)
os.environ["SUPERVISOR_TOKEN"] = "dummy"
check("Dev-Nutzer-ID per Header ist im Add-on kein Admin",
      me(as_user("dev-local"))["is_admin"] is False)

# ------------------------------------------------ Serverseitige Durchsetzung
admin_routes = [
    (method, route.path)
    for route in app.routes
    if getattr(route, "path", "").startswith("/api/admin")
    for method in route.methods
]
check("Admin-Endpunkte registriert", len(admin_routes) >= 12, f"{len(admin_routes)}")
blocked = []
for method, path in admin_routes:
    url = path.replace("{user_id}", "alice").replace("{pokemon_id}", "1") \
              .replace("{species_id}", "1").replace("{item_key}", "poke_ball")
    response = client.request(method, url, headers=BOB, json={})
    blocked.append(response.status_code == 403)
    if response.status_code != 403:
        print("   ", method, url, response.status_code)
check("Alle /api/admin-Endpunkte liefern 403 fuer Nicht-Admins", all(blocked))
check("Admin darf /api/admin/users", client.get("/api/admin/users", headers=ALICE).status_code == 200)

users = {u["id"]: u for u in client.get("/api/admin/users", headers=ALICE).json()}
check("Nutzerliste enthaelt Admin-Flag", users["alice"]["is_admin"] and not users["bob"]["is_admin"])

# ------------------------------------------------------------ Artensuche
def search(q: str) -> list[dict]:
    return client.get("/api/admin/species", params={"q": q}, headers=ALICE).json()


check("Suche nach Nummer", [s["id"] for s in search("25")] == [25])
check("Suche nach englischem Namen", search("pika")[0]["name"] == "pikachu")
# Der Fallback-Pokédex hat keine deutschen Namen - einen nachtragen.
db.execute("UPDATE species SET name_de = 'Glumanda' WHERE id = 4")
check("Suche nach deutschem Namen", search("glumanda")[0]["id"] == 4)
check("Suche mit #-Praefix", search("#7")[0]["id"] == 7)

all_species_ids = {row["id"] for row in db.query("SELECT id FROM species")}
# Niemand hat eine Liga besiegt -> nur Generation 1 ist ueberhaupt freigeschaltet
# (achievements.unlocked_generations(None), Vereinigung ueber alle Nutzer).
gen1_ids = {row["id"] for row in db.query("SELECT id FROM species WHERE generation = 1")}
random_hits = {client.get("/api/admin/species/random", headers=ALICE).json()["id"] for _ in range(20)}
check("Zufaellige Art liefert eine bekannte Art",
      random_hits and random_hits <= all_species_ids, str(random_hits))
check("Zufaellige Art kommt nur aus dem echten Spawn-Pool (Generation 1, niemand hat eine Liga)",
      random_hits <= gen1_ids, str(random_hits))
check("Route /species/random schlaegt nicht als /species/{id} fehl",
      client.get("/api/admin/species/random", headers=ALICE).status_code == 200)

# ------------------------------------------------------------- Spawnen
r = client.post("/api/admin/encounters", headers=ALICE,
                json={"species_id": 25, "level": 42, "shiny": True, "nature": "timid"})
enc = db.query_one("SELECT * FROM encounter WHERE id = ?", (r.json()["encounter_id"],))
check("Encounter mit Level/Shiny/Wesen erzeugt",
      enc["level"] == 42 and enc["shiny"] == 1 and enc["nature"] == "timid"
      and enc["status"] == "waiting", str({k: enc[k] for k in ("level", "shiny", "nature")}))
check("Encounter taucht in der Warteschlange auf",
      any(e["id"] == enc["id"] for e in client.get("/api/encounters", headers=BOB).json()))
check("Unbekannte Art -> 404",
      client.post("/api/admin/encounters", headers=ALICE,
                  json={"species_id": 99999, "level": 5}).status_code == 404)
check("Level > 100 -> 422",
      client.post("/api/admin/encounters", headers=ALICE,
                  json={"species_id": 25, "level": 101}).status_code == 422)

# Generation 2 ist fuer niemanden freigeschaltet (keine Liga besiegt) - ein
# solcher Spawn waere fuer die echte Spawn-Logik aller Spieler unmoeglich und
# muss abgelehnt werden (vorher: jede Art ging einfach durch).
r = client.post("/api/admin/encounters", headers=ALICE, json={"species_id": 152, "level": 5})
check("Gesperrte Generation im Spawn-Pool -> 422", r.status_code == 422, r.text)
check("Kein Encounter fuer die gesperrte Art angelegt",
      db.query_one("SELECT 1 FROM encounter WHERE species_id = 152") is None)
# Direktes Vergeben bleibt davon unberuehrt (kein Wild-Encounter, siehe unten).

charizard = db.query_one("SELECT * FROM species WHERE id = 6") or db.query_one(
    "SELECT * FROM species WHERE id = 4")
r = client.post("/api/admin/users/bob/pokemon", headers=ALICE, json={
    "species_id": charizard["id"], "level": 50, "shiny": True,
    "ivs": {"hp": 31, "atk": 30}, "nickname": "Feuerle", "placement": "party",
})
given = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (r.json()["owned_id"],))
given_ivs = json.loads(given["ivs"])
check("Pokémon direkt vergeben (Team-Platz 1)", given["user_id"] == "bob" and given["party_slot"] == 1)
check("EXP passt zum Level",
      given["exp"] == battle.exp_for_level(50, charizard["growth_rate"]))
check("Vorgegebene IVs uebernommen", given_ivs["hp"] == 31 and given_ivs["atk"] == 30)
check("Volle KP", given["current_hp"] == battle.stat_hp(charizard["base_hp"], 31, 50))
dex = db.query_one("SELECT * FROM dex_entry WHERE user_id = 'bob' AND species_id = ?",
                   (charizard["id"],))
check("Im Pokédex als gefangen markiert",
      dex and dex["caught_count"] == 1 and dex["seen_count"] >= 1 and dex["first_caught_at"])

for _ in range(5):
    client.post("/api/admin/users/bob/pokemon", headers=ALICE,
                json={"species_id": 129, "level": 5})
r = client.post("/api/admin/users/bob/pokemon", headers=ALICE,
                json={"species_id": 129, "level": 5}).json()
check("Volles Team -> Box", r["party_slot"] is None and r["placed_in_box_because_party_full"])
r = client.post("/api/admin/users/bob/pokemon", headers=ALICE,
                json={"species_id": 129, "level": 5, "placement": "box"}).json()
check("Direkt in die Box", r["party_slot"] is None and not r["placed_in_box_because_party_full"])
boxed_id = r["owned_id"]
check("Unbekannter Nutzer -> 404",
      client.post("/api/admin/users/nobody/pokemon", headers=ALICE,
                  json={"species_id": 25, "level": 5}).status_code == 404)

# Zapdos ist genderlos: auch eine ausdruecklich gewuenschte Angabe wird verworfen.
# Nicht im Fallback-Datensatz enthalten, also fuer den Test anlegen.
record = {"id": 145, "name": "zapdos", "type1": "electric", "catch_rate": 3, "base_exp": 290,
          "growth_rate": "slow", "base_hp": 90, "base_atk": 90, "base_def": 85,
          "base_spa": 125, "base_spd": 90, "base_spe": 100, "is_legendary": 1}
record.setdefault("generation", seed.generation_for(record["id"]))
seed._upsert(record)
r = client.post("/api/admin/users/bob/pokemon", headers=ALICE,
                json={"species_id": 145, "level": 40, "gender": "female", "placement": "box"})
zapdos = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (r.json()["owned_id"],))
check("Genderloses Pokemon vergeben -> kein Geschlecht", zapdos["gender"] is None)
r = client.post("/api/admin/encounters", headers=ALICE,
                json={"species_id": 145, "level": 40, "gender": "male"})
zapdos_enc = db.query_one("SELECT * FROM encounter WHERE id = ?", (r.json()["encounter_id"],))
check("Genderloses Encounter -> kein Geschlecht", zapdos_enc["gender"] is None)
r = client.patch(f"/api/admin/pokemon/{zapdos['id']}", headers=ALICE, json={"gender": "male"})
check("Genderloses Pokemon nachtraeglich mit Geschlecht -> bleibt genderlos",
      r.json()["pokemon"]["gender"] is None)

# ------------------------------------------------------------ Team bearbeiten
def patch(pid: int, body: dict):
    return client.patch(f"/api/admin/pokemon/{pid}", headers=ALICE, json=body)


pid = given["id"]
# Schaden simulieren, dann Level aendern: erlittener Schaden bleibt gleich.
db.execute("UPDATE owned_pokemon SET current_hp = current_hp - 10 WHERE id = ?", (pid,))
r = patch(pid, {"level": 60}).json()["pokemon"]
check("Level-Aenderung setzt EXP konsistent",
      r["level"] == 60 and r["exp"] == battle.exp_for_level(60, charizard["growth_rate"]))
check("Schaden bleibt bei Level-Aenderung erhalten", r["current_hp"] == r["max_hp"] - 10,
      f"{r['current_hp']}/{r['max_hp']}")
r = patch(pid, {"full_heal": True}).json()["pokemon"]
check("Vollheilung", r["current_hp"] == r["max_hp"])
r = patch(pid, {"current_hp": 99999}).json()["pokemon"]
check("KP werden auf Maximum begrenzt", r["current_hp"] == r["max_hp"])
r = patch(pid, {"ivs": {"spe": 0}, "nature": "jolly", "shiny": False, "gender": "female",
                "nickname": "  "}).json()["pokemon"]
check("IVs/Wesen/Shiny/Geschlecht/Spitzname",
      r["ivs"]["spe"] == 0 and r["ivs"]["hp"] == 31 and r["nature"] == "jolly"
      and r["shiny"] is False and r["gender"] == "female" and r["nickname"] is None)
check("IV > 31 -> 400", patch(pid, {"ivs": {"atk": 32}}).status_code == 400)
check("Unbekanntes Wesen -> 400", patch(pid, {"nature": "grumpy"}).status_code == 400)
check("Unbekannter IV-Schluessel -> 400", patch(pid, {"ivs": {"luck": 3}}).status_code == 400)

slot3 = db.query_one("SELECT id FROM owned_pokemon WHERE user_id='bob' AND party_slot=3")["id"]
patch(pid, {"party_slot": 3})
check("Slot-Tausch",
      db.query_one("SELECT party_slot FROM owned_pokemon WHERE id=?", (pid,))["party_slot"] == 3
      and db.query_one("SELECT party_slot FROM owned_pokemon WHERE id=?", (slot3,))["party_slot"] == 1)
patch(boxed_id, {"party_slot": 3})
check("Box -> belegter Platz: Tauschpartner geht in die Box",
      db.query_one("SELECT party_slot FROM owned_pokemon WHERE id=?", (pid,))["party_slot"] is None)
patch(boxed_id, {"party_slot": None})
check("Team -> Box",
      db.query_one("SELECT party_slot FROM owned_pokemon WHERE id=?", (boxed_id,))["party_slot"] is None)

# Freilassen raeumt auch Verweise aus (spaeter hinzukommende Tabellen).
db.execute("CREATE TABLE IF NOT EXISTS held_item_test ("
           "owned_id INTEGER NOT NULL REFERENCES owned_pokemon(id), item TEXT)")
db.execute("INSERT INTO held_item_test VALUES (?, 'leftovers')", (pid,))
check("Foreign Keys sind aktiv", db.query_one("PRAGMA foreign_keys")["foreign_keys"] == 1)
r = client.delete(f"/api/admin/pokemon/{pid}", headers=ALICE)
check("Freilassen", r.status_code == 200
      and db.query_one("SELECT 1 AS x FROM owned_pokemon WHERE id=?", (pid,)) is None)
check("Verweisende Zeilen werden mitgeloescht",
      db.query_one("SELECT COUNT(*) AS n FROM held_item_test")["n"] == 0)
check("Freilassen unbekannt -> 404", client.delete(f"/api/admin/pokemon/{pid}", headers=ALICE).status_code == 404)

# -------------------------------------------------------------- Konten
def put_inv(key: str, qty: int):
    return client.put(f"/api/admin/users/bob/inventory/{key}", headers=ALICE, json={"quantity": qty})


put_inv("ultra_ball", 7)
put_inv("fire_stone", 2)
inv = {i["item_key"]: i["quantity"] for i in
       client.get("/api/admin/users/bob", headers=ALICE).json()["inventory"]}
check("Inventar setzen (beliebiger item_key)", inv == {"ultra_ball": 7, "fire_stone": 2}, str(inv))
put_inv("ultra_ball", 0)
check("Menge 0 entfernt den Eintrag",
      db.query_one("SELECT 1 AS x FROM inventory WHERE user_id='bob' AND item_key='ultra_ball'") is None)
check("Ungueltiger item_key -> 400", put_inv("Bad Key!", 1).status_code == 400)
check("Negative Menge -> 422", put_inv("poke_ball", -1).status_code == 422)


# Editierbare app_user-Felder: nur, was es als Spalte wirklich gibt.
meta = client.get("/api/admin/meta", headers=ALICE).json()
check("Meta: Wesen und Items", "timid" in meta["natures"] and "poke_ball" in meta["item_keys"])
check("Geld ist editierbar (Spalte aus der Wirtschafts-Migration)",
      "money" in meta["editable_user_fields"])
r = client.patch("/api/admin/users/bob", headers=ALICE, json={"money": 1234})
check("Geld setzen", r.status_code == 200 and r.json()["user"]["money"] == 1234)
check("Geld negativ -> 400",
      client.patch("/api/admin/users/bob", headers=ALICE, json={"money": -1}).status_code == 400)

# ------------------------------------------- Full editor: account fields
fields = meta["editable_user_fields"]
check("Preferences are editable account fields",
      all(k in fields for k in ("language", "region_lock", "battle_style", "auto_heal")))
check("Choice fields ship their options", "de" in fields["language"]["options"]
      and fields["region_lock"]["options"] == list(db.REGION_LOCK_MODES))
r = client.patch("/api/admin/users/bob", headers=ALICE,
                 json={"language": "de", "region_lock": "unlocked", "auto_heal": True})
check("Set language / region lock / auto heal", r.status_code == 200
      and r.json()["user"]["language"] == "de" and r.json()["user"]["region_lock"] == "unlocked"
      and r.json()["user"]["auto_heal"] == 1)
check("Unknown choice value -> 400",
      client.patch("/api/admin/users/bob", headers=ALICE, json={"language": "xx"}).status_code == 400)
check("Only money and god mode are reset on wipe (preferences have no reset value)",
      sorted(k for k, spec in fields.items() if "reset" in spec) == ["god_mode", "money"])
client.patch("/api/admin/users/bob", headers=ALICE, json={"language": "en"})

# ------------------------------------------- Full editor: Pokémon fields
check("Meta: move / ability / held item pickers",
      any(m["key"] == "thunderbolt" for m in meta["moves"])
      and any(a["key"] == "intimidate" for a in meta["abilities"])
      and any(h["key"] == "leftovers" for h in meta["held_items"]))
mon = client.patch(f"/api/admin/pokemon/{boxed_id}", headers=ALICE,
                   json={"ability": "intimidate", "held_item": "leftovers", "favorite": True,
                         "locked": True}).json()["pokemon"]
check("Ability / held item / favorite / locked", mon["ability"] == "intimidate"
      and mon["held_item"] == "leftovers" and mon["favorite"] is True and mon["locked"] is True)
check("Unknown ability -> 400", patch(boxed_id, {"ability": "nonsense"}).status_code == 400)
check("Unknown held item -> 400", patch(boxed_id, {"held_item": "nonsense"}).status_code == 400)
mon = patch(boxed_id, {"ability": "", "held_item": None}).json()["pokemon"]
check("Clear ability and held item", mon["ability"] is None and mon["held_item"] is None)

mon = patch(boxed_id, {"evs": {"atk": 252, "spe": 252}}).json()["pokemon"]
check("Set EVs", mon["evs"]["atk"] == 252 and mon["evs"]["spe"] == 252 and mon["evs"]["hp"] == 0)
check("EV above the per-stat cap -> 400", patch(boxed_id, {"evs": {"hp": 256}}).status_code == 400)

growth = db.query_one("SELECT growth_rate FROM species WHERE id = 129")["growth_rate"]
mon = patch(boxed_id, {"exp": battle.exp_for_level(42, growth) + 1}).json()["pokemon"]
check("EXP sets the level that matches it", mon["level"] == 42
      and mon["exp"] == battle.exp_for_level(42, growth) + 1, f"{mon['level']} / {mon['exp']}")

check("Magikarp can't learn Thunderbolt on its own", "thunderbolt" not in mon["learned_moves"])
mon = patch(boxed_id, {"moves": ["thunderbolt", "tackle"]}).json()["pokemon"]
check("Any move can be set", mon["moves"] == ["thunderbolt", "tackle"])
check("A move it couldn't learn is granted into its own pool",
      "thunderbolt" in mon["learned_moves"])
check("Unknown move -> 400", patch(boxed_id, {"moves": ["nope"]}).status_code == 400)
check("Repeated move -> 400", patch(boxed_id, {"moves": ["tackle", "tackle"]}).status_code == 400)
check("More than four moves -> 422", patch(boxed_id, {
    "moves": ["tackle", "thunderbolt", "flamethrower", "hydro_pump", "surf"]}).status_code == 422)
mon = patch(boxed_id, {"moves": []}).json()["pokemon"]
check("[] restores the standard moveset", mon["moves"] == [])

db.execute("UPDATE owned_pokemon SET current_hp = 1 WHERE id = ?", (boxed_id,))
mon = patch(boxed_id, {"species_id": 25}).json()["pokemon"]
check("Change species in place", mon["species_id"] == 25 and mon["species"]["id"] == 25
      and 1 <= mon["current_hp"] < mon["max_hp"])
check("New species counts as caught", db.query_one(
    "SELECT caught_count FROM dex_entry WHERE user_id = 'bob' AND species_id = 25")["caught_count"] >= 1)
check("Unknown species -> 404", patch(boxed_id, {"species_id": 99999}).status_code == 404)

# ------------------------------------------------------- Pokédex editor
dex = client.get("/api/admin/users/bob/dex", headers=ALICE).json()
species_total = db.query_one("SELECT COUNT(*) AS n FROM species")["n"]
check("Pokédex lists every species", len(dex["entries"]) == species_total)
r = client.put("/api/admin/users/bob/dex/1", headers=ALICE,
               json={"seen_count": 0, "caught_count": 3, "shiny_caught": True})
entry = r.json()["entry"]
check("Caught implies seen; shiny stored", entry["seen_count"] == 1 and entry["caught_count"] == 3
      and entry["shiny_caught"] == 1 and entry["first_caught_at"])
r = client.put("/api/admin/users/bob/dex/1", headers=ALICE,
               json={"seen_count": 2, "caught_count": 0})
check("Back to seen only clears the catch date", r.json()["entry"]["caught_count"] == 0
      and r.json()["entry"]["first_caught_at"] is None)
client.put("/api/admin/users/bob/dex/1", headers=ALICE, json={"seen_count": 0, "caught_count": 0})
check("All zero removes the entry", db.query_one(
    "SELECT 1 AS x FROM dex_entry WHERE user_id = 'bob' AND species_id = 1") is None)

client.post("/api/admin/users/bob/dex/bulk", headers=ALICE, json={"state": "caught", "generation": 1})
gen1 = db.query_one("SELECT COUNT(*) AS n FROM species WHERE generation = 1")["n"]
caught_gen1 = db.query_one(
    "SELECT COUNT(*) AS n FROM dex_entry d JOIN species s ON s.id = d.species_id "
    "WHERE d.user_id = 'bob' AND s.generation = 1 AND d.caught_count > 0")["n"]
check("Bulk: whole generation caught", caught_gen1 == gen1, f"{caught_gen1}/{gen1}")
client.post("/api/admin/users/bob/dex/bulk", headers=ALICE, json={"state": "shiny"})
check("Bulk: everything shiny", db.query_one(
    "SELECT COUNT(*) AS n FROM dex_entry WHERE user_id = 'bob' AND shiny_caught = 1")["n"] == species_total)
client.post("/api/admin/users/bob/dex/bulk", headers=ALICE, json={"state": "none"})
check("Bulk: none empties the Pokédex", db.query_one(
    "SELECT COUNT(*) AS n FROM dex_entry WHERE user_id = 'bob'")["n"] == 0)

# --------------------------------------------------- Achievement editor
ach = client.get("/api/admin/users/bob/achievements", headers=ALICE).json()["achievements"]
badge = next(a for a in ach if a.get("trainer_id"))
check("Achievements list gym badges, locked by default", not badge["unlocked"])
balls_before = db.query_one(
    "SELECT COALESCE(SUM(quantity), 0) AS n FROM inventory WHERE user_id = 'bob'")["n"]
r = client.put(f"/api/admin/users/bob/achievements/{badge['key']}", headers=ALICE,
               json={"unlocked": True, "reward": True})
balls_after = db.query_one(
    "SELECT COALESCE(SUM(quantity), 0) AS n FROM inventory WHERE user_id = 'bob'")["n"]
check("Grant a gym badge with reward", r.json()["changed"] == 1
      and badge["key"] in achievements.unlocked("bob")
      and (balls_after > balls_before or not badge.get("reward")))
client.put(f"/api/admin/users/bob/achievements/{badge['key']}", headers=ALICE, json={"unlocked": False})
check("Revoke it again", db.query_one(
    "SELECT 1 AS x FROM achievement WHERE user_id = 'bob' AND key = ?", (badge["key"],)) is None)
keys = [a["key"] for a in ach if a.get("trainer_id")]
r = client.post("/api/admin/users/bob/achievements/bulk", headers=ALICE,
                json={"keys": keys, "unlocked": True})
check("Bulk grant every badge without reward", r.json()["changed"] == len(keys)
      and db.query_one("SELECT COALESCE(SUM(quantity), 0) AS n FROM inventory WHERE user_id = 'bob'")["n"] == balls_after)
check("Unknown achievement -> 400", client.put(
    "/api/admin/users/bob/achievements/nope", headers=ALICE, json={"unlocked": True}).status_code == 400)
client.post("/api/admin/users/bob/achievements/bulk", headers=ALICE, json={"keys": keys, "unlocked": False})
check("Non-admin can't edit the Pokédex", client.post(
    "/api/admin/users/bob/dex/bulk", headers=BOB, json={"state": "caught"}).status_code == 403)

# ------------------------------------------------------- Raw data editor
tables = {t["table"]: t for t in client.get("/api/admin/users/bob/tables", headers=ALICE).json()}
check("Raw editor lists per-player tables incl. the account row and trades",
      {"owned_pokemon", "inventory", "pokemon_egg", "app_user", "trade"} <= set(tables)
      and tables["app_user"]["rows"] == 1 and tables["app_user"]["fixed"])
check("Shared tables without an owner are not listed",
      "species" not in tables and "setting" not in tables)
raw = client.get("/api/admin/users/bob/tables/inventory", headers=ALICE).json()
check("Rows come with column types", any(c["name"] == "quantity" and "INT" in c["type"]
                                         for c in raw["columns"]))
r = client.post("/api/admin/users/bob/tables/inventory", headers=ALICE,
                json={"values": {"item_key": "rare_candy", "quantity": "5"}})
check("Insert a row (owner set automatically, string converted to INTEGER)",
      r.status_code == 200 and db.query_one(
          "SELECT quantity FROM inventory WHERE user_id = 'bob' AND item_key = 'rare_candy'")["quantity"] == 5)
rowid = r.json()["rowid"]
check("Duplicate primary key -> 400", client.post(
    "/api/admin/users/bob/tables/inventory", headers=ALICE,
    json={"values": {"item_key": "rare_candy", "quantity": 1}}).status_code == 400)
r = client.patch(f"/api/admin/users/bob/tables/inventory/{rowid}", headers=ALICE,
                 json={"values": {"quantity": "9"}})
check("Update a row", r.status_code == 200 and db.query_one(
    "SELECT quantity FROM inventory WHERE user_id = 'bob' AND item_key = 'rare_candy'")["quantity"] == 9)
check("Non-number in an INTEGER column -> 400", client.patch(
    f"/api/admin/users/bob/tables/inventory/{rowid}", headers=ALICE,
    json={"values": {"quantity": "lots"}}).status_code == 400)
check("NOT NULL column emptied -> 400", client.patch(
    f"/api/admin/users/bob/tables/inventory/{rowid}", headers=ALICE,
    json={"values": {"quantity": ""}}).status_code == 400)
check("Owner column can't be changed", client.patch(
    f"/api/admin/users/bob/tables/inventory/{rowid}", headers=ALICE,
    json={"values": {"user_id": "alice"}}).status_code == 400)
check("Unknown column -> 400", client.patch(
    f"/api/admin/users/bob/tables/inventory/{rowid}", headers=ALICE,
    json={"values": {"nope": 1}}).status_code == 400)
alice_row = db.query_one("SELECT rowid AS r FROM inventory WHERE user_id = 'alice' LIMIT 1")
if alice_row:
    check("Another player's row isn't reachable", client.patch(
        f"/api/admin/users/bob/tables/inventory/{alice_row['r']}", headers=ALICE,
        json={"values": {"quantity": 1}}).status_code == 404)
check("Unknown table -> 404",
      client.get("/api/admin/users/bob/tables/species", headers=ALICE).status_code == 404)
r = client.patch("/api/admin/users/bob/tables/app_user/"
                 + str(db.query_one("SELECT rowid AS r FROM app_user WHERE id = 'bob'")["r"]),
                 headers=ALICE, json={"values": {"starter_species_id": "25"}})
check("Account row: any column editable", r.status_code == 200
      and db.get_user("bob")["starter_species_id"] == 25)
check("Account row can't be deleted", client.delete(
    "/api/admin/users/bob/tables/app_user/1", headers=ALICE).status_code == 400)
check("Delete a row", client.delete(f"/api/admin/users/bob/tables/inventory/{rowid}",
                                    headers=ALICE).status_code == 200
      and db.query_one("SELECT 1 AS x FROM inventory WHERE user_id = 'bob' AND item_key = 'rare_candy'") is None)

# Deleting an encounter cleans up what points at it (encounter_user ...).
enc_id = spawn.create_encounter(db.query_one("SELECT * FROM species WHERE id = 25"),
                                source_entity="test", source_kind="home", user_id="bob")
encounter_state.save_progress(enc_id, "bob", current_hp=1)
enc_rowid = db.query_one("SELECT rowid AS r FROM encounter WHERE id = ?", (enc_id,))["r"]
r = client.delete(f"/api/admin/users/bob/tables/encounter/{enc_rowid}", headers=ALICE)
check("Delete an encounter incl. its per-player progress", r.status_code == 200
      and db.query_one("SELECT 1 AS x FROM encounter_user WHERE encounter_id = ?", (enc_id,)) is None)
check("Non-admin can't use the raw editor",
      client.get("/api/admin/users/bob/tables", headers=BOB).status_code == 403)
check("Identitaetsfelder nicht editierbar",
      client.patch("/api/admin/users/bob", headers=ALICE, json={"name": "x"}).status_code == 400)

# Starter zuruecksetzen, Team behalten.
users_api.choose_starter(users_api.StarterChoice(species_id=7), db.get_user("carol")
                         or db.upsert_user("carol", "carol"))
client.post("/api/admin/users/carol/pokemon", headers=ALICE, json={"species_id": 25, "level": 9})
r = client.post("/api/admin/users/carol/reset-starter", headers=ALICE, json={"keep_pokemon": True})
check("Starter-Reset: Team in die Box", r.json().get("moved_to_box") == 2
      and battle.party("carol") == [] and db.get_user("carol")["starter_species_id"] is None)
check("Starter-Reset: /users/me meldet has_starter=false",
      me(as_user("carol"))["has_starter"] is False)
users_api.choose_starter(users_api.StarterChoice(species_id=1), db.get_user("carol"))
party = battle.party("carol")
check("Neuer Starter allein auf Platz 1", len(party) == 1 and party[0]["species_id"] == 1)
check("Alte Pokémon noch in der Box",
      db.query_one("SELECT COUNT(*) AS n FROM owned_pokemon WHERE user_id='carol'")["n"] == 3)
client.post("/api/admin/users/carol/reset-starter", headers=ALICE, json={"keep_pokemon": False})
check("Starter-Reset ohne Team loescht alle Pokémon",
      db.query_one("SELECT COUNT(*) AS n FROM owned_pokemon WHERE user_id='carol'")["n"] == 0)

# Spielstand loeschen. Die eigene Warteschlange (encounter.user_id) gehoert
# dazu, der geteilte Admin-Spawn (user_id NULL) dagegen nicht.
own_enc = spawn.create_encounter(
    db.query_one("SELECT * FROM species WHERE id = 129"),
    source_entity="test.trigger", user_id="bob",
)
encounter_state.save_progress(own_enc, "bob", current_hp=3)
db.execute("UPDATE encounter SET claimed_by = 'bob' WHERE id = ?", (enc["id"],))
db.execute("INSERT INTO battle_log (encounter_id, user_id, created_at) VALUES (?, 'bob', 'x')",
           (enc["id"],))
r = client.delete("/api/admin/users/bob/data", headers=ALICE).json()
counts = {t: db.query_one(f"SELECT COUNT(*) AS n FROM {t} WHERE user_id='bob'")["n"]
          for t in ("owned_pokemon", "inventory", "dex_entry", "battle_log")}
check("Spielstand geloescht", all(n == 0 for n in counts.values()), str(counts))
bob_row = db.get_user("bob")
check("Nutzer bleibt, Geld zurueckgesetzt", bob_row and bob_row["money"] == 0
      and bob_row["starter_species_id"] is None)
check("Eigene Encounter gehoeren zum Spielstand und sind mit geloescht",
      db.query_one("SELECT 1 AS x FROM encounter WHERE id = ?", (own_enc,)) is None
      and db.query_one("SELECT 1 AS x FROM encounter_user WHERE encounter_id = ?",
                       (own_enc,)) is None)
check("Geteilte Warteschlange unberuehrt",
      db.query_one("SELECT claimed_by FROM encounter WHERE id=?", (enc["id"],))["claimed_by"] == "bob")
r = client.delete("/api/admin/users/bob/data", params={"delete_user": "true"}, headers=ALICE)
check("Nutzer ganz loeschen", r.status_code == 200 and db.get_user("bob") is None)
check("Verweise auf den Nutzer werden geloest",
      db.query_one("SELECT claimed_by FROM encounter WHERE id=?", (enc["id"],))["claimed_by"] is None)

# --------------------------------------------------- Arten-Abfrage (Debug)
info = client.get("/api/admin/species/25", headers=ALICE).json()
check("Arten-Abfrage: Zeile, Basiswerte und Summe",
      info["species"]["id"] == 25 and set(info["base_stats"]) == set(spawn.STAT_KEYS)
      and info["base_stat_total"] == sum(info["base_stats"].values()))
check("Arten-Abfrage: Seltenheit passt zu rarity.tier_for",
      info["rarity"]["tier"] == rarity.tier_for(info["species"])
      and info["rarity"]["tier_weight"] == rarity.tier_multipliers()[info["rarity"]["tier"]])
check("Arten-Abfrage: Spawn-Gewicht wie im Spawn-Code",
      abs(info["rarity"]["spawn_weight"] - round(spawn._rarity_weight(info["species"]), 4)) < 1e-9
      and 0 < info["rarity"]["spawn_share_percent"] < 100)
check("Arten-Abfrage: IV-Untergrenze und Nutzung",
      set(info["min_ivs"]) == set(spawn.STAT_KEYS) and info["usage"]["owned"] >= 0)
db.execute("INSERT OR IGNORE INTO evolution (from_species_id, to_species_id, trigger_kind, item_key) "
           "VALUES (25, 26, 'use-item', 'thunder_stone')")
db.execute("INSERT OR IGNORE INTO species_ability (species_id, ability_key, slot) "
           "VALUES (25, 'static', 0), (25, 'lightning_rod', 2)")
info = client.get("/api/admin/species/25", headers=ALICE).json()
check("Arten-Abfrage: Entwicklungen", any(
    e["from"]["id"] == 25 and e["item"] == "thunder_stone" for e in info["evolutions"]))
check("Arten-Abfrage: Faehigkeiten mit Versteckt-Flag",
      {a["key"]: a["hidden"] for a in info["abilities"]} == {"static": False, "lightning_rod": True})
check("Arten-Abfrage: unbekannte Art -> 404",
      client.get("/api/admin/species/99999", headers=ALICE).status_code == 404)
check("Meta liefert Item-Kategorien",
      meta["items"]["poke_ball"]["category"] == "ball")

# ------------------------------------------------- Spiel-Einstellungen (Admin)
groups = client.get("/api/admin/settings", headers=ALICE).json()["groups"]
grouped_keys = [s["key"] for g in groups for s in g["settings"]]
check("Jede Einstellung gehoert genau einer Gruppe",
      sorted(grouped_keys) == sorted(db.DEFAULT_SETTINGS), str(set(db.DEFAULT_SETTINGS) ^ set(grouped_keys)))
check("Nichts landet in 'other' (neue Einstellung -> in SETTING_GROUPS eintragen)",
      all(g["key"] != admin_api.OTHER_SETTINGS_GROUP for g in groups))
check("Ohne Aenderung ist nichts als geaendert markiert",
      not any(s["modified"] for g in groups for s in g["settings"]))

check("Einstellungen speichern (Admin)", client.patch(
    "/api/settings", headers=ALICE,
    json={"max_spawns_per_day": 99, "shiny_rate": 0.5, "auto_battle_enabled": True}).status_code == 200)
check("Einstellungen speichern (Nicht-Admin) -> 403", client.patch(
    "/api/settings", headers=BOB, json={"max_spawns_per_day": 1}).status_code == 403)
modified = {s["key"] for g in client.get("/api/admin/settings", headers=ALICE).json()["groups"]
            for s in g["settings"] if s["modified"]}
check("Geaenderte Werte sind markiert",
      modified == {"max_spawns_per_day", "shiny_rate", "auto_battle_enabled"}, str(modified))

r = client.post("/api/admin/settings/reset", headers=ALICE, json={"group": "auto_battle"})
check("Gruppen-Reset setzt nur diese Gruppe zurueck",
      r.status_code == 200 and db.get_setting("auto_battle_enabled") is False
      and db.get_setting("max_spawns_per_day") == 99)
check("Unbekannte Gruppe -> 400", client.post(
    "/api/admin/settings/reset", headers=ALICE, json={"group": "nope"}).status_code == 400)
db.set_setting("starter_inventory", {"poke_ball": 1})
r = client.post("/api/admin/settings/reset", headers=ALICE, json={})
check("Alles zuruecksetzen", r.status_code == 200 and db.all_settings() == {
    **{key: value for key, value in db.all_settings().items() if key not in db.DEFAULT_SETTINGS},
    **db.DEFAULT_SETTINGS})
check("Nach dem Reset ist nichts mehr geaendert",
      not any(s["modified"] for g in r.json()["groups"] for s in g["settings"]))
check("Reset (Nicht-Admin) -> 403", client.post(
    "/api/admin/settings/reset", headers=BOB, json={}).status_code == 403)

# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
