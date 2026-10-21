"""Tests for the buddy Pokémon: helpers (entity id, unit conversion, sensor
candidates), choosing buddy / step sensor, levelling by steps, the Home
Assistant state (attributes, sync, push failures), the API endpoints and the
frontend wiring (buddy view, i18n keys, guide section).

Uses a fake HA client - no Home Assistant needed.

Run:  python3 tests/test_buddy.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-buddy-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import abilities, battle, buddy, db, economy, seed, spawn  # noqa: E402
from app.api import buddy as buddy_api  # noqa: E402
from app.api import config_routes  # noqa: E402
from app.ha_client import HomeAssistantError  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


def expect_error(label: str, code: int, fn, *args) -> None:
    try:
        result = fn(*args)
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    except HTTPException as exc:
        check(label, exc.status_code == code, f"{exc.status_code}: {exc.detail}")
    else:
        check(label, False, "no error")


class FakeHA:
    """Records what the add-on writes to HA and serves canned sensor states."""

    def __init__(self) -> None:
        self.states: dict[str, dict] = {}   # entity_id -> {"state", "attributes"} as HA would return
        self.pushed: dict[str, tuple[str, dict]] = {}
        self.deleted: list[str] = []
        self.fail = False

    async def set_state(self, entity_id, state, attributes):
        if self.fail:
            raise HomeAssistantError("HA down")
        self.pushed[entity_id] = (state, attributes)

    async def delete_state(self, entity_id):
        if self.fail:
            raise HomeAssistantError("HA down")
        self.deleted.append(entity_id)
        self.pushed.pop(entity_id, None)

    async def get_state(self, entity_id):
        return self.states.get(entity_id)

    async def list_states(self):
        if self.fail:
            raise HomeAssistantError("HA down")
        return [{"entity_id": key, **value} for key, value in self.states.items()]

    def set_sensor(self, entity_id, value, unit="steps", **attrs):
        self.states[entity_id] = {"state": str(value),
                                  "attributes": {"unit_of_measurement": unit, **attrs}}


# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()
abilities.seed_from_fallback()
alice = db.upsert_user("alice", "alice", "Alice")
bob = db.upsert_user("bob", "bob", "Bob")
PER_LEVEL = buddy.steps_per_level()

pika = db.query_one("SELECT * FROM species WHERE name = 'pikachu'")
karp = db.query_one("SELECT * FROM species WHERE name = 'magikarp'")


def give(user_id: str, species: dict, level: int, **extra) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "current_hp, caught_at, nickname) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, species["id"], level, battle.exp_for_level(level), extra.get("shiny", 0),
         json.dumps({key: 31 for key in spawn.STAT_KEYS}), "hardy",
         battle.stat_hp(species["base_hp"], 31, level), db._now_iso(), extra.get("nickname")),
    )


def user(user_id: str = "alice") -> dict:
    return db.get_user(user_id)


def mon(owned_id: int) -> dict:
    return db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))


a_pika = give("alice", pika, 10)
a_karp = give("alice", karp, 5, shiny=1, nickname="Goldie")
b_karp = give("bob", karp, 5)

# --------------------------------------------------------- Pure helpers
check("Entity id from the HA user name", buddy.entity_id_for({"id": "x", "name": "Alice"}) == "sensor.pokemon_buddy_alice")
check("Entity id: umlauts and separators become ASCII",
      buddy.entity_id_for({"id": "x", "name": "Jürgen M. Müller"}) == "sensor.pokemon_buddy_jurgen_m_muller",
      buddy.entity_id_for({"id": "x", "name": "Jürgen M. Müller"}))
check("Entity id: unusable name falls back to the user id",
      buddy.entity_id_for({"id": "AB-12-cd", "name": "日本"}) == "sensor.pokemon_buddy_ab12cd")
check("Entity id: everything missing falls back to 'user'",
      buddy.entity_id_for({"id": "--", "name": ""}) == "sensor.pokemon_buddy_user")

check("valid_sensor_id accepts sensor.*", buddy.valid_sensor_id("sensor.pixel_steps_sensor"))
for bad in ("", None, "light.kitchen", "sensor.", "sensor.Upper", "sensor.a b", "sensor.a/../b"):
    check(f"valid_sensor_id rejects {bad!r}", not buddy.valid_sensor_id(bad))

check("to_steps: plain number / steps unit", buddy.to_steps("1234", None) == 1234 and buddy.to_steps(50, "Steps") == 50)
check("to_steps: 5 km is about one level", abs(buddy.to_steps("5", "km") - 6493.5) < 1, buddy.to_steps("5", "km"))
check("to_steps: m / mi / ft / yd converted",
      abs(buddy.to_steps(770, "m") - 1000) < 1e-6 and buddy.to_steps(1, "mi") > 2000
      and buddy.to_steps(100, "ft") > 0 and buddy.to_steps(100, "yd") > 0)
check("to_steps: unavailable / unknown unit / negative / NaN are unusable",
      all(buddy.to_steps(v, u) is None for v, u in
          (("unavailable", "steps"), ("unknown", None), (5, "kcal"), (-3, "steps"), ("nan", "steps"), (None, None))))

candidates = [
    ({"entity_id": "sensor.pixel_steps_sensor", "attributes": {"unit_of_measurement": "steps"}}, True),
    ({"entity_id": "sensor.walk", "attributes": {"device_class": "distance", "unit_of_measurement": "km"}}, True),
    ({"entity_id": "sensor.iphone_steps", "attributes": {}}, True),
    ({"entity_id": "sensor.temperature", "attributes": {"unit_of_measurement": "°C"}}, False),
    ({"entity_id": "sensor.step_voltage", "attributes": {"unit_of_measurement": "V"}}, False),
    ({"entity_id": "light.steps", "attributes": {"unit_of_measurement": "steps"}}, False),
]
for state, expected in candidates:
    check(f"is_step_candidate({state['entity_id']}) == {expected}", buddy.is_step_candidate(state) is expected)

check("Default steps per level is 6500", PER_LEVEL == 6500, PER_LEVEL)

# ------------------------------------------------------- Choose a buddy
check("No buddy initially", buddy.status(user())["pokemon"] is None)
check("No HA state without a buddy", buddy.ha_state(user()) is None)
try:
    buddy.set_buddy("alice", b_karp)
    check("Foreign Pokémon can't be the buddy", False)
except ValueError:
    check("Foreign Pokémon can't be the buddy", user()["buddy_pokemon_id"] is None)
try:
    buddy.set_buddy("alice", 99999)
    check("Unknown Pokémon can't be the buddy", False)
except ValueError:
    check("Unknown Pokémon can't be the buddy", True)

buddy.set_buddy("alice", a_pika)
check("Buddy is stored", user()["buddy_pokemon_id"] == a_pika)
db.execute("UPDATE app_user SET buddy_steps = 500 WHERE id = 'alice'")
buddy.set_buddy("alice", a_pika)
check("Re-selecting the same buddy keeps banked steps", user()["buddy_steps"] == 500)
buddy.set_buddy("alice", a_karp)
check("Switching buddy drops banked steps", user()["buddy_pokemon_id"] == a_karp and user()["buddy_steps"] == 0)
buddy.set_buddy("alice", a_pika)
buddy.set_buddy("alice", None)
check("Buddy can be removed", user()["buddy_pokemon_id"] is None)
buddy.set_buddy("alice", a_pika)

# ------------------------------------------------------ Choose a sensor
try:
    buddy.set_sensor("alice", "light.kitchen")
    check("Non-sensor entity is rejected", False)
except ValueError:
    check("Non-sensor entity is rejected", user()["buddy_sensor"] is None)
buddy.set_sensor("alice", "sensor.alice_steps")
db.execute("UPDATE app_user SET buddy_sensor_last = 100 WHERE id = 'alice'")
buddy.set_sensor("alice", "sensor.alice_steps2")
check("Choosing a sensor resets the baseline", user()["buddy_sensor"] == "sensor.alice_steps2" and user()["buddy_sensor_last"] is None)
buddy.set_sensor("alice", "sensor.alice_steps")

# ------------------------------------------------- Levelling by steps
check("First reading is only the baseline", buddy.apply_reading("alice", 4000) == [] and user()["buddy_sensor_last"] == 4000)
check("Baseline credits nothing", mon(a_pika)["level"] == 10 and user()["buddy_steps"] == 0)
check("Small delta is banked without a level", buddy.apply_reading("alice", 4000 + PER_LEVEL - 100) == []
      and user()["buddy_steps"] == PER_LEVEL - 100 and mon(a_pika)["level"] == 10)
gained = buddy.apply_reading("alice", 4000 + PER_LEVEL + 50)
check("Reaching the threshold gives +1 level", gained == [11] and mon(a_pika)["level"] == 11, gained)
check("Remainder stays banked", user()["buddy_steps"] == 50, user()["buddy_steps"])
check("EXP is raised to the start of the new level", mon(a_pika)["exp"] >= battle.exp_for_level(11, "medium_fast"))
check("Steps total counts everything", user()["buddy_steps_total"] == PER_LEVEL + 50, user()["buddy_steps_total"])

base = user()["buddy_sensor_last"]
gained = buddy.apply_reading("alice", base + 2 * PER_LEVEL)
check("A big delta gives several levels at once", gained == [12, 13] and mon(a_pika)["level"] == 13, gained)

before = mon(a_pika)["level"]
banked = user()["buddy_steps"]
buddy.apply_reading("alice", 300)   # counter reset (midnight): the new value is the delta
check("Counter reset counts the new value as steps", user()["buddy_steps"] == banked + 300 and mon(a_pika)["level"] == before)
check("Baseline follows the reset", user()["buddy_sensor_last"] == 300)

banked = user()["buddy_steps"]
check("Unchanged reading credits nothing", buddy.apply_reading("alice", 300) == [] and user()["buddy_steps"] == banked)
check("Implausible jump is discarded", buddy.apply_reading("alice", 300 + buddy.MAX_STEPS_PER_READING + 1) == []
      and user()["buddy_steps"] == banked)
check("... but becomes the new baseline", user()["buddy_sensor_last"] == 300 + buddy.MAX_STEPS_PER_READING + 1)

db.execute("UPDATE owned_pokemon SET level = 99 WHERE id = ?", (a_pika,))
db.execute("UPDATE app_user SET buddy_steps = 0, buddy_sensor_last = 0 WHERE id = 'alice'")
gained = buddy.apply_reading("alice", 3 * PER_LEVEL)
check("Level is capped at 100", gained == [100] and mon(a_pika)["level"] == 100, gained)
check("No steps are banked at max level", user()["buddy_steps"] == 0)
db.execute("UPDATE owned_pokemon SET level = 10, exp = ? WHERE id = ?", (battle.exp_for_level(10), a_pika))

# Setting: steps per level is configurable
db.set_setting("buddy_steps_per_level", 1000)
db.execute("UPDATE app_user SET buddy_steps = 0, buddy_sensor_last = 0 WHERE id = 'alice'")
check("buddy_steps_per_level setting is honoured", buddy.apply_reading("alice", 2500) == [11, 12] and user()["buddy_steps"] == 500)
db.set_setting("buddy_steps_per_level", PER_LEVEL)
db.execute("UPDATE owned_pokemon SET level = 10 WHERE id = ?", (a_pika,))
db.execute("UPDATE app_user SET buddy_steps = 0 WHERE id = 'alice'")

# Steps also reach Eggs, with or without a buddy
credited: list[tuple[str, float]] = []
orig_credit = buddy.breeding.credit_steps
buddy.breeding.credit_steps = lambda uid, steps: credited.append((uid, steps)) or 0
try:
    buddy.apply_reading("alice", 2500 + 200)
    buddy.set_buddy("alice", None)
    buddy.apply_reading("alice", 2500 + 500)
finally:
    buddy.breeding.credit_steps = orig_credit
check("Steps go to Eggs with and without a buddy", credited == [("alice", 200), ("alice", 300)], credited)
check("Without a buddy no level or steps are banked", mon(a_pika)["level"] == 10 and user()["buddy_steps"] == 0)
buddy.set_buddy("alice", a_pika)
check("apply_reading for an unknown user is a no-op", buddy.apply_reading("nobody", 100) == [])

# ------------------------------------------------- Status / HA state
info = buddy.status(user())
check("status: pokemon block", info["pokemon"]["id"] == a_pika and info["pokemon"]["species"].lower() == "pikachu"
      and info["pokemon"]["level"] == 10 and info["pokemon"]["types"] == ["electric"], info["pokemon"])
check("status: entity id, sensor, baseline",
      info["entity_id"] == "sensor.pokemon_buddy_alice" and info["sensor"] == "sensor.alice_steps" and info["has_baseline"])
check("status: shows the artwork url", info["pokemon"]["artwork_url"].endswith(f"/official-artwork/{pika['id']}.png"))
state, attrs = buddy.ha_state(user())
check("HA state is the level", state == "10", state)
check("HA attributes carry name, sprite and progress",
      attrs["pokemon_name"].lower() == "pikachu"
      and attrs["entity_picture"] == pika["sprite_url"] and attrs["steps_per_level"] == PER_LEVEL
      and attrs["steps_to_next_level"] == PER_LEVEL - attrs["steps"] and attrs["step_sensor"] == "sensor.alice_steps", attrs)
check("HA friendly name mentions user and buddy", "Alice" in attrs["friendly_name"], attrs["friendly_name"])
db.execute("UPDATE app_user SET buddy_steps = ? WHERE id = 'alice'", (PER_LEVEL // 2,))
check("progress_percent reflects banked steps", buddy.ha_state(user())[1]["progress_percent"] == 50)

buddy.set_buddy("alice", a_karp)
karp_info = buddy.status(user())["pokemon"]
check("Nickname is used as buddy name", karp_info["name"] == "Goldie" and karp_info["species"] != "Goldie", karp_info)
check("Shiny buddy uses the shiny sprite and artwork",
      "/sprites/pokemon/shiny/" in (karp_info["sprite_url"] or "") and "/official-artwork/shiny/" in karp_info["artwork_url"], karp_info)
db.execute("UPDATE owned_pokemon SET level = 100 WHERE id = ?", (a_karp,))
max_attrs = buddy.ha_state(user())[1]
check("At max level: no steps to next level, 100 %", max_attrs["steps_to_next_level"] is None and max_attrs["progress_percent"] == 100)
db.execute("UPDATE owned_pokemon SET level = 5 WHERE id = ?", (a_karp,))
db.set_user_language("alice", "de")
de_name = buddy.status(user())["pokemon"]["species"]
check("German user gets the German species name", de_name == (karp.get("name_de") or de_name), de_name)
check("German friendly name", "Buddy von Alice" in buddy.ha_state(user())[1]["friendly_name"], buddy.ha_state(user())[1]["friendly_name"])
db.set_user_language("alice", "en")
buddy.set_buddy("alice", a_pika)

# ------------------------------------------------------------- HA sync
ha = FakeHA()
ha.set_sensor("sensor.alice_steps", 2000)
buddy.set_sensor("alice", "sensor.alice_steps")
levels = asyncio.run(buddy.sync(ha))
check("sync: first reading is the baseline, state is pushed", levels == 0
      and ha.pushed["sensor.pokemon_buddy_alice"][0] == str(mon(a_pika)["level"]))
ha.set_sensor("sensor.alice_steps", 2000 + PER_LEVEL)
levels = asyncio.run(buddy.sync(ha))
check("sync: credits the delta and pushes the new level", levels == 1
      and ha.pushed["sensor.pokemon_buddy_alice"][0] == "11" and mon(a_pika)["level"] == 11, levels)
ha.set_sensor("sensor.alice_steps", "unavailable")
check("sync: unavailable sensor is skipped", asyncio.run(buddy.sync(ha)) == 0 and mon(a_pika)["level"] == 11)
total_before = user()["buddy_steps_total"]
db.execute("UPDATE app_user SET buddy_sensor_last = 0 WHERE id = 'alice'")
ha.set_sensor("sensor.alice_steps", 3, unit="km")
asyncio.run(buddy.sync(ha))
walked = user()["buddy_steps_total"] - total_before
check("sync: distance sensors are converted (3 km ~ 3896 steps)", abs(walked - 3896) <= 1, walked)
del ha.states["sensor.alice_steps"]
check("sync: missing sensor entity doesn't crash", asyncio.run(buddy.sync(ha)) == 0)

ha.fail = True
try:
    asyncio.run(buddy.sync(ha))
    check("sync: HA errors are swallowed per user", True)
except Exception as exc:  # noqa: BLE001
    check("sync: HA errors are swallowed per user", False, exc)
ha.fail = False

give_bob = give("bob", pika, 20)
buddy.set_buddy("bob", give_bob)
asyncio.run(buddy.sync(ha))
check("sync: every buddy owner gets an entity", "sensor.pokemon_buddy_bob" in ha.pushed and "sensor.pokemon_buddy_alice" in ha.pushed)
db.execute("DELETE FROM owned_pokemon WHERE id = ?", (give_bob,))
asyncio.run(buddy.sync(ha))
check("sync: released buddy is cleared and its entity removed",
      user("bob")["buddy_pokemon_id"] is None and "sensor.pokemon_buddy_bob" in ha.deleted
      and "sensor.pokemon_buddy_bob" not in ha.pushed)

# ----------------------------------------------------------------- API
paths = {(method, route.path) for route in app.routes for method in getattr(route, "methods", [])}
for method, path in (("GET", "/api/buddy"), ("PUT", "/api/buddy"), ("PUT", "/api/buddy/sensor"),
                     ("GET", "/api/buddy/sensors")):
    check(f"Route {method} {path} is registered", (method, path) in paths)

fake = FakeHA()
buddy_api.ha_client = fake
alice_ctx = user()
check("GET /api/buddy mirrors status", buddy_api.get_buddy(alice_ctx)["pokemon"]["id"] == a_pika)

r = asyncio.run(buddy_api.set_buddy(buddy_api.BuddyIn(pokemon_id=a_karp), alice_ctx))
check("PUT /api/buddy sets the buddy and pushes to HA",
      r["pokemon"]["id"] == a_karp and r["pushed"] is True and "sensor.pokemon_buddy_alice" in fake.pushed, r["pushed"])
expect_error("PUT /api/buddy: foreign Pokémon -> 404", 404, buddy_api.set_buddy, buddy_api.BuddyIn(pokemon_id=b_karp), alice_ctx)
expect_error("PUT /api/buddy: unknown Pokémon -> 404", 404, buddy_api.set_buddy, buddy_api.BuddyIn(pokemon_id=99999), alice_ctx)
check("Failed PUT keeps the previous buddy", user()["buddy_pokemon_id"] == a_karp)

fake.fail = True
r = asyncio.run(buddy_api.set_buddy(buddy_api.BuddyIn(pokemon_id=a_pika), alice_ctx))
check("PUT /api/buddy stays saved when HA is unreachable", r["pokemon"]["id"] == a_pika and r["pushed"] is False)
fake.fail = False
r = asyncio.run(buddy_api.set_buddy(buddy_api.BuddyIn(pokemon_id=None), alice_ctx))
check("PUT /api/buddy null removes buddy and HA entity", r["pokemon"] is None
      and "sensor.pokemon_buddy_alice" in fake.deleted, r)
asyncio.run(buddy_api.set_buddy(buddy_api.BuddyIn(pokemon_id=a_pika), alice_ctx))

r = asyncio.run(buddy_api.set_sensor(buddy_api.SensorIn(entity_id="  sensor.x_steps "), alice_ctx))
check("PUT /api/buddy/sensor trims and stores", r["sensor"] == "sensor.x_steps" and r["has_baseline"] is False, r["sensor"])
expect_error("PUT /api/buddy/sensor: invalid id -> 400", 400, buddy_api.set_sensor, buddy_api.SensorIn(entity_id="light.a"), alice_ctx)
r = asyncio.run(buddy_api.set_sensor(buddy_api.SensorIn(entity_id=None), alice_ctx))
check("PUT /api/buddy/sensor null turns counting off", r["sensor"] is None)
r = asyncio.run(buddy_api.set_sensor(buddy_api.SensorIn(entity_id=""), alice_ctx))
check("PUT /api/buddy/sensor empty string turns counting off", r["sensor"] is None)

fake.set_sensor("sensor.b_steps", 10, friendly_name="B Phone Steps")
fake.set_sensor("sensor.a_walk", 3, unit="km", device_class="distance", friendly_name="a Walk")
fake.set_sensor("sensor.temp", 20, unit="°C", friendly_name="Temp")
listed = asyncio.run(buddy_api.list_step_sensors(alice_ctx))["sensors"]
check("GET /api/buddy/sensors: only step/distance sensors, sorted by name",
      [s["entity_id"] for s in listed] == ["sensor.a_walk", "sensor.b_steps"], listed)
check("GET /api/buddy/sensors: name, state and unit are included",
      listed[1] == {"entity_id": "sensor.b_steps", "name": "B Phone Steps", "state": "10", "unit": "steps"}, listed[1])
fake.fail = True
expect_error("GET /api/buddy/sensors: HA down -> 502", 502, buddy_api.list_step_sensors, alice_ctx)
fake.fail = False

# ------------------------------------------------------------- Setting
for good in (100, 6500, 1_000_000):
    try:
        config_routes._validate_buddy_steps({"buddy_steps_per_level": good})
        check(f"buddy_steps_per_level {good} is valid", True)
    except HTTPException:
        check(f"buddy_steps_per_level {good} is valid", False)
for bad in (99, 1_000_001, 6500.5, 0, -5):
    expect_error(f"buddy_steps_per_level {bad} is rejected", 400, config_routes._validate_buddy_steps, {"buddy_steps_per_level": bad})

# ---------------------------------------------- Frontend and i18n wiring
index = (ROOT / "pokemon_encounter" / "www" / "index.html").read_text(encoding="utf-8")
i18n_dir = ROOT / "pokemon_encounter" / "app" / "data" / "i18n"
catalogs = {lang: json.loads((i18n_dir / f"{lang}.json").read_text(encoding="utf-8")) for lang in ("en", "de")}

for needle, label in (("'/api/buddy'", "buddy endpoint"), ("'/api/buddy/sensor'", "sensor endpoint"),
                      ("'/api/buddy/sensors'", "sensor list endpoint"), ("renderBuddy", "buddy view"),
                      ("teamViewBuddy", "buddy sub-navigation"), ("pokeBuddy", "buddy button in the detail view"),
                      ("buddyRemove", "remove-buddy button"), ("guideSectionHtml('buddy'", "guide section")):
    check(f"Frontend uses the {label}", needle in index)

used = set(re.findall(r"tr\('(buddy\.[a-z_]+|team\.subnav_buddy)'", index))
check("Frontend references buddy i18n keys", len(used) >= 10, sorted(used))
for lang, catalog in catalogs.items():
    missing = sorted(key for key in used if key not in catalog)
    check(f"[{lang}] every buddy key used in index.html exists", not missing, missing)
    guide = [f"guide.buddy.title"] + [f"guide.buddy.p{i}" for i in (1, 2, 3)]
    check(f"[{lang}] guide section has title and 3 paragraphs", all(catalog.get(key) for key in guide))
    check(f"[{lang}] guide text has no unfilled {{placeholders}} or raw <tags>",
          not any(re.search(r"\{\w+\}|<[a-z]", catalog[key]) for key in guide))
    for key in ("buddy.ha_friendly_name", "settings.buddy_steps_per_level", "error.buddy_invalid_sensor"):
        check(f"[{lang}] {key} exists", key in catalog)
check("Buddy i18n keys exist in both languages",
      {k for k in catalogs["en"] if k.startswith(("buddy.", "guide.buddy."))}
      == {k for k in catalogs["de"] if k.startswith(("buddy.", "guide.buddy."))})

docs = (ROOT / "pokemon_encounter" / "DOCS.md").read_text(encoding="utf-8")
check("DOCS.md documents the buddy sensor and its UI", "sensor.pokemon_buddy_" in docs and "Team → Buddy" in docs)
readme = (ROOT / "README.md").read_text(encoding="utf-8")
check("README documents the buddy", "**Buddy**" in readme and "sensor.pokemon_buddy_" in readme)

# ---- buddy finds: per-step chance, weighted pool, bag + log
import random  # noqa: E402

def bag(item: str, uid: str = "alice") -> int:
    row = db.query_one("SELECT quantity FROM inventory WHERE item_key = ? AND user_id = ?", (item, uid))
    return row["quantity"] if row else 0

check("buddy_find_chance defaults to 1/3000 per step", abs(db.DEFAULT_SETTINGS["buddy_find_chance"] - 1 / 3000) < 1e-8)
pool = db.DEFAULT_SETTINGS["buddy_find_pool"]
check("default pool: common + rare items as specified",
      pool["common"]["items"] == ["poke_ball", "great_ball", "potion", "super_potion", "ultra_ball"]
      and pool["rare"]["items"] == ["rare_candy", "revive", "max_revive"])
check("every pool item is a known item", all(
    i in set(battle.BALLS) | set(economy.catalog_by_key()) for t in pool.values() for i in t["items"]))
check("no steps / zero chance -> no finds", buddy.roll_finds("alice", 0) == []
      and (db.set_setting("buddy_find_chance", 0), buddy.roll_finds("alice", 5000))[1] == [])
db.set_setting("buddy_find_chance", 1.0)
ALL = ("poke_ball", "great_ball", "potion", "super_potion", "ultra_ball", "rare_candy", "revive", "max_revive")
bag_before = sum(bag(i) for i in ALL)
log_before = len(db.query("SELECT 1 FROM buddy_find WHERE user_id = 'alice'"))
found = buddy.roll_finds("alice", 40, random.Random(1))
check("chance 1.0 finds one item per step", len(found) == 40, len(found))
check("finds land in the bag and the log",
      sum(bag(i) for i in ALL) - bag_before == 40
      and len(db.query("SELECT 1 FROM buddy_find WHERE user_id = 'alice'")) - log_before == 40
      and len(buddy.status(user())["finds"]) == 10)
db.set_setting("buddy_find_pool", {"rare": {"weight": 1, "items": ["rare_candy"]}})
rc = bag("rare_candy")
check("custom pool is honoured", [f["item_key"] for f in buddy.roll_finds("alice", 3)] == ["rare_candy"] * 3
      and bag("rare_candy") == rc + 3)
db.set_setting("buddy_find_chance", 1 / 3000)
db.set_setting("buddy_find_pool", db.DEFAULT_SETTINGS["buddy_find_pool"])
hits = sum(len(buddy.roll_finds("alice", 3000, random.Random(n))) for n in range(100))
check("1/3000 per step averages ~1 find per 3000 steps", 60 <= hits <= 140, hits)
expect_error("find chance > 1 is rejected", 400, config_routes._validate_buddy_finds, {"buddy_find_chance": 2})
expect_error("unknown pool item is rejected", 400, config_routes._validate_buddy_finds,
             {"buddy_find_pool": {"x": {"weight": 1, "items": ["nope"]}}})
config_routes._validate_buddy_finds({"buddy_find_chance": 0.001, "buddy_find_pool": pool})
for lang in ("en", "de"):
    cat = json.loads((ROOT / "pokemon_encounter" / "app" / "data" / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
    check(f"[{lang}] find i18n keys exist", all(k in cat for k in (
        "buddy.finds_title", "buddy.finds_hint", "buddy.finds_none", "buddy.found_rare",
        "settings.buddy_find_chance", "settings.buddy_find_pool")))

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(" -", label)
    sys.exit(1)
print("All buddy tests passed.")
