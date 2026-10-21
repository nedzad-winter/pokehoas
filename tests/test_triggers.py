"""Tests for automatic triggers from add-on configuration.

Runs without Home Assistant with a simulated state list based on a real
household (groups, switch-as-light helpers, config LEDs, thermostat window
detection, ...).

Call: python3 tests/test_triggers.py
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
from urllib.parse import urlencode

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-trigger-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"
OPTIONS = Path(TMP) / "options.json"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


# ------------------------------------------------------ Migration Old DB
# Existing production DB: trigger_entity without `source`/`category`, with
# a manually created trigger including user tuning.
VORRAUM = "binary_sensor.bewegungssensor_vorraum_occupancy"
legacy = sqlite3.connect(Path(TMP) / "pokemon.sqlite3")
legacy.executescript(
    """
    CREATE TABLE trigger_entity (
        entity_id       TEXT PRIMARY KEY,
        friendly_name   TEXT,
        enabled         INTEGER NOT NULL DEFAULT 1,
        trigger_states  TEXT NOT NULL DEFAULT '["on"]',
        spawn_chance    REAL NOT NULL DEFAULT 0.25,
        type_weights    TEXT NOT NULL DEFAULT '{}',
        cooldown_minutes INTEGER NOT NULL DEFAULT 0,
        last_spawn_at   TEXT
    );
    """
)
legacy.execute(
    "INSERT INTO trigger_entity VALUES (?,?,?,?,?,?,?,?)",
    (VORRAUM, "Motion Sensor - Foyer Occupancy", 1, '["on"]', 0.4,
     '{"bug": 2.0}', 5, "2026-09-20T10:00:00+02:00"),
)
legacy.commit()
legacy.close()

from app import db, seed, spawn, trigger_sync  # noqa: E402
from app.api import config_routes  # noqa: E402
from app.ha_client import HomeAssistantClient, HomeAssistantError, chunk_entity_ids  # noqa: E402

db.init_db()
db.init_db()  # idempotent
seed.seed_from_fallback()

vorraum_before = {
    "entity_id": VORRAUM, "friendly_name": "Motion Sensor - Foyer Occupancy",
    "enabled": 1, "trigger_states": '["on"]', "spawn_chance": 0.4,
    "type_weights": '{"bug": 2.0}', "cooldown_minutes": 5,
    "last_spawn_at": "2026-09-20T10:00:00+02:00",
}


def row(entity_id: str) -> dict | None:
    return db.query_one("SELECT * FROM trigger_entity WHERE entity_id = ?", (entity_id,))


migrated = row(VORRAUM)
check("Migration: existing trigger becomes 'manual'", migrated["source"] == "manual")
check(
    "Migration: existing trigger tuning unchanged",
    all(migrated[k] == v for k, v in vorraum_before.items()),
    str(migrated),
)
cols = {c["name"] for c in db.connect().execute("PRAGMA table_info(trigger_entity)").fetchall()}
check("Migration: source/category columns present", {"source", "category"} <= cols)


# ------------------------------------------------------------ Fake HA
def st(eid, state="off", device_class=None, name=None, **attrs):
    attributes = {"friendly_name": name or eid, **attrs}
    if device_class:
        attributes["device_class"] = device_class
    return {"entity_id": eid, "state": state, "attributes": attributes}


def reg(entity_id, platform="mqtt", category=None, hidden=None, disabled=None):
    return {"entity_id": entity_id, "platform": platform, "entity_category": category,
            "hidden_by": hidden, "disabled_by": disabled}


STATES = [
    st(VORRAUM, "on", "occupancy", "Motion Sensor - Foyer Occupancy"),
    st("binary_sensor.bewegungssensor_kuche_occupancy", "off", "occupancy"),
    st("binary_sensor.kamera_garten_motion", "off", "motion"),
    st("binary_sensor.praesenz_buero", "off", "presence"),
    st("binary_sensor.bewegung_im_haus", "on", "motion",
       entity_id=[VORRAUM, "binary_sensor.bewegungssensor_kuche_occupancy"]),
    st("binary_sensor.fenstersensor_klo_contact", "off", "door"),
    st("binary_sensor.garage", "off", "garage_door"),
    st("binary_sensor.offene_fenster_tueren", "off", "door"),               # template
    st("binary_sensor.thermostat_kuche_window_open", "off", "window"),     # diagnostic
    st("binary_sensor.waschmaschine_problem", "off", "problem"),           # no category
    st("light.licht_kuche", "off", name="Kitchen Light"),                     # switch_as_x
    st("switch.shelly1_5562fa", "off", name="Kitchen Light"),                 # hidden (integration)
    st("light.ap_eg_led", "off"),                                           # config
    st("light.diele", "unavailable"),                                       # group
    st("light.halbstock_links", "unavailable"),                             # tuya, offline
    st("light.yaml_lampe", "on"),                                           # no registry
    st("switch.steckdose_pc", "on"),
    st("switch.steckdose_waschmaschine", "on"),                             # hidden (user)
    st("switch.overseerr_container", "on", "switch"),
    st("switch.alexa_do_not_disturb", "off"),                              # config
    st("media_player.wohnzimmer", "idle"),
    st("vacuum.roborock", "docked"),
    st("sensor.temperatur", "21.5"),
]
REGISTRY = [
    reg(VORRAUM), reg("binary_sensor.bewegungssensor_kuche_occupancy"),
    reg("binary_sensor.kamera_garten_motion", "blink"),
    reg("binary_sensor.praesenz_buero"),
    reg("binary_sensor.bewegung_im_haus", "group"),
    reg("binary_sensor.fenstersensor_klo_contact"),
    reg("binary_sensor.garage"),
    reg("binary_sensor.offene_fenster_tueren", "template"),
    reg("binary_sensor.thermostat_kuche_window_open", category="diagnostic"),
    reg("light.licht_kuche", "switch_as_x"),
    reg("switch.shelly1_5562fa", "shelly", hidden="integration"),
    reg("light.ap_eg_led", "unifi", category="config"),
    reg("light.diele", "group"),
    reg("light.halbstock_links", "tuya"),
    reg("switch.steckdose_pc"),
    reg("switch.steckdose_waschmaschine", hidden="user"),
    reg("switch.overseerr_container", "portainer"),
    reg("switch.alexa_do_not_disturb", "alexa_media", category="config"),
    reg("switch.deaktiviert", disabled="user"),
    reg("media_player.wohnzimmer", "sonos"),
    reg("vacuum.roborock", "roborock"),
]


class FakeClient:
    def __init__(self, states, registry):
        self.states = states
        self.registry = registry
        self.registry_error = False

    async def list_states(self):
        return [json.loads(json.dumps(s)) for s in self.states]

    async def list_entity_registry(self):
        if self.registry_error:
            raise HomeAssistantError("WebSocket broken")
        return self.registry


def set_state(client, entity_id, state):
    for s in client.states:
        if s["entity_id"] == entity_id:
            s["state"] = state


def config_ids() -> set[str]:
    return {r["entity_id"] for r in db.query("SELECT entity_id FROM trigger_entity WHERE source='config'")}


def write_options(**options):
    OPTIONS.write_text(json.dumps({"log_level": "info", **options}), encoding="utf-8")


def sync(client):
    return asyncio.run(trigger_sync.sync(client))


client = FakeClient(STATES, REGISTRY)

# ------------------------------------------------------- Read Options
opts = trigger_sync.load_options()
check("Without options.json, default categories apply",
      opts["categories"] == ["motion", "door_window", "lights_switches"] and not opts["found"])

write_options(trigger_categories=["motion", "door_window", "lights_switches", "bogus"],
              extra_entities=["sensor.temperatur", "not_a_valid_name"],
              exclude_entities=[" Switch.*_Container "])
opts = trigger_sync.load_options()
check("Unknown category is ignored", "bogus" not in opts["categories"])
check("Invalid extra ID is ignored", opts["extra_entities"] == ["sensor.temperatur"])
check("Exclusion pattern normalized", opts["exclude_entities"] == ["switch.*_container"])

# ----------------------------------------------------------- First Sync
result = sync(client)
expected = {
    "binary_sensor.bewegungssensor_kuche_occupancy",
    "binary_sensor.kamera_garten_motion",
    "binary_sensor.praesenz_buero",
    "binary_sensor.fenstersensor_klo_contact",
    "binary_sensor.garage",
    "light.licht_kuche",
    "light.yaml_lampe",
    "switch.steckdose_pc",
    "sensor.temperatur",
}
check("Sync takes exactly the matching entities", config_ids() == expected,
      f"too many: {sorted(config_ids() - expected)}, missing: {sorted(expected - config_ids())}")
check("Skip reasons are counted",
      result["skipped"].get("gruppe") == 2 and result["skipped"].get("ausgeschlossen") == 1
      and result["skipped"].get("entity_category:config") == 2
      and result["skipped"].get("entity_category:diagnostic") == 1
      and result["skipped"].get("ausgeblendet") == 2 and result["skipped"].get("template") == 1,
      str(result["skipped"]))
check("Offline light is in the selection but not yet created",
      # +1 offline light, +1 manual foyer sensor (matches "motion", stays manual)
      row("light.halbstock_links") is None and result["selected"] == len(expected) + 2,
      f"{result['selected']} in selection")

check("Manual trigger stays manual and unchanged",
      all(row(VORRAUM)[k] == v for k, v in vorraum_before.items()) and row(VORRAUM)["source"] == "manual")

light = row("light.licht_kuche")
check("Light: category + electric bonus + low chance",
      light["category"] == "lights_switches"
      and json.loads(light["type_weights"]).get("electric", 0) > 1
      and light["spawn_chance"] == 0.05 and light["friendly_name"] == "Kitchen Light")
check("Switch: even lower chance", row("switch.steckdose_pc")["spawn_chance"] == 0.03)
check("Motion: chance 0.25, state on",
      row("binary_sensor.kamera_garten_motion")["spawn_chance"] == 0.25
      and json.loads(row("binary_sensor.kamera_garten_motion")["trigger_states"]) == ["on"])
check("Door: chance 0.15", row("binary_sensor.fenstersensor_klo_contact")["spawn_chance"] == 0.15)
check("Extra without category lands under 'other'", row("sensor.temperatur")["category"] == "other")

# ---------------------------------------------------- Tuning is Preserved
db.execute("UPDATE trigger_entity SET spawn_chance = 0.9, enabled = 0, trigger_states = '[\"off\"]' "
           "WHERE entity_id = 'light.licht_kuche'")
sync(client)
light = row("light.licht_kuche")
check("Re-syncing doesn't overwrite user tuning",
      light["spawn_chance"] == 0.9 and light["enabled"] == 0 and light["trigger_states"] == '["off"]')

# Offline light is added once it's reachable.
set_state(client, "light.halbstock_links", "off")
sync(client)
check("Reachable entity is adopted", row("light.halbstock_links") is not None)

# Brief radio outage doesn't remove anything.
set_state(client, "light.halbstock_links", "unavailable")
sync(client)
check("Unavailable entity is not removed", row("light.halbstock_links") is not None)

# ------------------------------------------------------ HA Error: do nothing
before = config_ids()
client.registry_error = True
try:
    sync(client)
    check("Registry error raises", False)
except HomeAssistantError:
    check("Registry error raises", True)
check("Registry error changes nothing", config_ids() == before)
check("Status reports the error", trigger_sync.status()["ok"] is False)
client.registry_error = False

empty = FakeClient([], REGISTRY)
try:
    sync(empty)
except Exception:  # noqa: BLE001
    pass
check("Empty state list removes nothing", config_ids() == before)

# ------------------------------------------------------ Exclusion / Deselection
write_options(trigger_categories=["motion", "door_window", "lights_switches"],
              extra_entities=["sensor.temperatur"],
              exclude_entities=["switch.*_container", "light.yaml_*", VORRAUM])
sync(client)
check("Excluded config entity is removed", row("light.yaml_lampe") is None)
check("Exclusion doesn't affect manual triggers", row(VORRAUM) is not None)

write_options(trigger_categories=["motion", "media_player"], extra_entities=["sensor.temperatur"])
sync(client)
ids = config_ids()
check("Deselected categories are removed",
      not any(i.startswith(("light.", "switch.", "binary_sensor.fenster", "binary_sensor.garage")) for i in ids),
      str(sorted(ids)))
check("Newly selected category is adopted",
      row("media_player.wohnzimmer") is not None
      and json.loads(row("media_player.wohnzimmer")["trigger_states"]) == ["playing"])
check("Extra is retained despite deselection", "sensor.temperatur" in ids)

write_options(trigger_categories=[], extra_entities=[])
sync(client)
check("Empty category list = no automatic triggers", config_ids() == set())
check("... manual trigger remains anyway", row(VORRAUM) is not None and row(VORRAUM)["enabled"] == 1)

# Manual trigger + matching category: no duplicate, no adoption.
OPTIONS.unlink()
sync(client)
check("Manual trigger doesn't become 'config'", row(VORRAUM)["source"] == "manual"
      and row(VORRAUM)["spawn_chance"] == 0.4)

# -------------------------------------------------------------- API
rows = config_routes.list_triggers()
by_id = {r["entity_id"]: r for r in rows}
check("API: manual trigger is grouped by category",
      by_id[VORRAUM]["category"] == "motion" and by_id[VORRAUM]["source"] == "manual")
check("API: config trigger with label",
      by_id["light.licht_kuche"]["category_label"] == "Lights & Switches")

try:
    config_routes.delete_trigger("light.licht_kuche")
    check("API: config trigger deletion is rejected", False)
except HTTPException as exc:
    check("API: config trigger deletion is rejected", exc.status_code == 409)

cfg = config_routes.trigger_config()
check("API: active categories", [c["key"] for c in cfg["categories"] if c["active"]]
      == ["motion", "door_window", "lights_switches"])

light_ids = [r["entity_id"] for r in rows if r["category"] == "lights_switches"]
config_routes.bulk_enable_triggers(config_routes.TriggerBulkIn(entity_ids=light_ids, enabled=False))
check("API: bulk disable",
      all(row(i)["enabled"] == 0 for i in light_ids) and row(VORRAUM)["enabled"] == 1)

# Manual save (PUT) of a config trigger doesn't change the source.
config_routes.upsert_trigger("switch.steckdose_pc", config_routes.TriggerIn(
    entity_id="switch.steckdose_pc", spawn_chance=0.5))
check("API: save keeps source='config'", row("switch.steckdose_pc")["source"] == "config"
      and row("switch.steckdose_pc")["spawn_chance"] == 0.5)

# ------------------------------------------------ Spawns from Sync Triggers
# Gespawnt wird in die Warteschlange je Nutzer - ohne registrierten Nutzer
# gibt es also nichts zu spawnen (siehe spawn.spawn_users).
db.upsert_user("alice", "Alice")
db.set_setting("global_cooldown_minutes", 0)
db.execute("UPDATE trigger_entity SET spawn_chance = 1.0, enabled = 1 WHERE entity_id = 'light.licht_kuche'")
db.execute("UPDATE trigger_entity SET trigger_states = '[\"on\"]' WHERE entity_id = 'light.licht_kuche'")
now = datetime.now(timezone.utc)
created = spawn.process_events(
    [{"entity_id": "light.licht_kuche", "state": "on", "last_changed": now}],
    db.query("SELECT * FROM trigger_entity WHERE enabled = 1"), now=now,
)
check("Sync trigger creates encounter", created == 1)
check("Encounter landet in der Warteschlange des Nutzers", db.query_one(
    "SELECT user_id FROM encounter ORDER BY id DESC LIMIT 1")["user_id"] == "alice")

# ------------------------------------------------------ Admin "trigger now"
# The admin panel's manual "Trigger now" button (POST /api/triggers/{id}/fire)
# reuses process_events(force=True) so an admin can test a trigger's setup
# right away, without waiting for spawn_chance's dice roll or a real HA event.
try:
    config_routes.fire_trigger("light.does_not_exist")
    check("Fire: unknown entity -> 404", False)
except HTTPException as exc:
    check("Fire: unknown entity -> 404", exc.status_code == 404)

db.execute("UPDATE trigger_entity SET enabled = 0 WHERE entity_id = 'light.licht_kuche'")
try:
    config_routes.fire_trigger("light.licht_kuche")
    check("Fire: disabled trigger -> 409", False)
except HTTPException as exc:
    check("Fire: disabled trigger -> 409", exc.status_code == 409)

# spawn_chance=0 and an active cooldown would normally block a real event -
# force=True must ignore both.
db.execute(
    """UPDATE trigger_entity SET enabled = 1, spawn_chance = 0.0, cooldown_minutes = 999,
       last_spawn_at = ? WHERE entity_id = 'light.licht_kuche'""",
    (datetime.now(timezone.utc).isoformat(),),
)
before_count = db.query_one("SELECT COUNT(*) AS n FROM encounter")["n"]
result = config_routes.fire_trigger("light.licht_kuche")
after_count = db.query_one("SELECT COUNT(*) AS n FROM encounter")["n"]
check("Fire: ignores spawn_chance and entity cooldown", result == {"ok": True, "created": 1})
check("Fire: encounter actually created", after_count == before_count + 1)

# Per-user daily cap is a player protection, not a device property - it still
# applies even to a forced trigger.
db.set_setting("max_spawns_per_day", after_count)
result = config_routes.fire_trigger("light.licht_kuche")
check("Fire: still respects the daily cap", result == {"ok": False, "created": 0})
db.set_setting("max_spawns_per_day", 0)

# -------------------------------------------------- History in Chunks
many = [f"switch.steckdose_nummer_{i:04d}_im_keller" for i in range(600)] + ["switch.steckdose_nummer_0000_im_keller"]
chunks = chunk_entity_ids(many)
check("Chunking: each ID exactly once", sum(len(c) for c in chunks) == 600 and len({e for c in chunks for e in c}) == 600)
check("Chunking: multiple requests", len(chunks) > 1, f"{len(chunks)} requests")
since = now - timedelta(minutes=10)
max_url = max(
    len(f"/api/history/period/{since.isoformat()}?" + urlencode(
        {"filter_entity_id": ",".join(c), "minimal_response": "", "significant_changes_only": "0",
         "end_time": now.isoformat()}))
    for c in chunks
)
check("Chunking: each URL well below HA's 8 KB limit", max_url < 6000, f"max {max_url} characters")
check("Chunking: small list = one request", len(chunk_entity_ids(["light.a", "light.b"])) == 1)

calls = []


async def fake_get(path, params=None):
    ids = params["filter_entity_id"].split(",")
    calls.append(ids)
    out = []
    for n, entity_id in enumerate(ids):
        # First entry = state at start (before `since`, will be discarded).
        t = now - timedelta(minutes=9) + timedelta(seconds=(hash(entity_id) % 500))
        out.append([
            {"entity_id": entity_id, "state": "off", "last_changed": (since - timedelta(minutes=1)).isoformat()},
            {"state": "on", "last_changed": t.isoformat()},
        ])
    return out

ha = HomeAssistantClient(base_url="http://example", token="x")
ha._get = fake_get  # type: ignore[method-assign]
events = asyncio.run(ha.fetch_state_changes(many, since=since, until=now))
check("History: all chunks queried", len(calls) == len(chunks))
check("History: flat list, one event per entity", len(events) == 600
      and len({e["entity_id"] for e in events}) == 600)
check("History: chronologically sorted",
      all(a["last_changed"] <= b["last_changed"] for a, b in zip(events, events[1:])))


async def failing_get(path, params=None):
    raise HomeAssistantError("boom")

ha._get = failing_get  # type: ignore[method-assign]
try:
    asyncio.run(ha.fetch_state_changes(many, since=since))
    check("History: error in a chunk raises", False)
except HomeAssistantError:
    check("History: error in a chunk raises", True)

check("WebSocket URL from Supervisor URL",
      HomeAssistantClient(base_url="http://supervisor/core", token="x")._ws_url()
      == "ws://supervisor/core/websocket")

# -------------------------------------------------------------------- End
print()
if failures:
    print(f"{len(failures)} test(s) failed: {failures}")
    sys.exit(1)
print("All tests passed.")
