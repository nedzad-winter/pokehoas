"""Configuration endpoints.

All game configuration is maintained here - not in the add-on configuration.
The user selects in the web app from the entity list in Home Assistant what can
trigger a spawn, and sets the associated type bonus. Changes take effect
immediately, without restart.

Exception: WHICH categories automatically become triggers (motion, doors/windows,
lights/switches, ...) is in the add-on configuration - see trigger_sync.py.
The web app just displays this choice; individual triggers can still be fine-
tuned here.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..admin import require_admin
from .. import battle, db, economy, rarity, spawn, trigger_sync
from ..auth import get_current_user
from ..ha_client import HomeAssistantError
from ..runtime import ha_client, poller

router = APIRouter(prefix="/api", tags=["config"])

# Domains that make sense as spawn triggers.
SUGGESTED_DOMAINS = (
    "binary_sensor", "light", "switch", "sensor", "media_player",
    "vacuum", "climate", "cover", "lock", "person", "device_tracker", "zone",
)


class TriggerIn(BaseModel):
    entity_id: str
    friendly_name: str | None = None
    enabled: bool = True
    trigger_states: list[str] = Field(default_factory=lambda: ["on"])
    spawn_chance: float = 0.25
    type_weights: dict[str, float] = Field(default_factory=dict)
    cooldown_minutes: int = 0


class TriggerBulkIn(BaseModel):
    entity_ids: list[str]
    enabled: bool


class ZoneBiomeIn(BaseModel):
    zone_entity_id: str
    biome: str
    enabled: bool = True


# ------------------------------------------------------------- HA-Entities

@router.get("/ha/entities", dependencies=[Depends(require_admin)])
async def list_ha_entities(domain: str | None = None, q: str | None = None) -> dict[str, Any]:
    """Entity-Liste aus HA, damit die App eine Auswahl anbieten kann."""
    try:
        states = await ha_client.list_states()
    except HomeAssistantError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    configured = {row["entity_id"] for row in db.query("SELECT entity_id FROM trigger_entity")}
    items = []
    for state in states:
        entity_id = state["entity_id"]
        entity_domain = entity_id.split(".", 1)[0]
        if entity_domain not in SUGGESTED_DOMAINS:
            continue
        if domain and entity_domain != domain:
            continue
        attributes = state.get("attributes") or {}
        name = attributes.get("friendly_name") or entity_id
        if q and q.lower() not in f"{entity_id} {name}".lower():
            continue
        device_class = attributes.get("device_class")
        category = trigger_sync.classify(entity_id, device_class)
        defaults = trigger_sync.defaults_for(entity_id, category, device_class)
        items.append(
            {
                "entity_id": entity_id,
                "friendly_name": name,
                "domain": entity_domain,
                "device_class": device_class,
                "state": state.get("state"),
                "already_configured": entity_id in configured,
                "suggested_type_weights": defaults["type_weights"],
                "suggested_trigger_states": defaults["trigger_states"],
                "suggested_spawn_chance": defaults["spawn_chance"],
                "category": category or trigger_sync.OTHER,
            }
        )
    items.sort(key=lambda item: item["friendly_name"].lower())
    return {"count": len(items), "entities": items}


# -------------------------------------------------------------- Trigger

@router.get("/triggers")
def list_triggers() -> list[dict[str, Any]]:
    rows = db.query("SELECT * FROM trigger_entity ORDER BY entity_id")
    for row in rows:
        row["trigger_states"] = json.loads(row["trigger_states"])
        row["type_weights"] = json.loads(row["type_weights"])
        row["enabled"] = bool(row["enabled"])
        row["source"] = row.get("source") or "manual"
        row["category"] = trigger_sync.category_for_row(row)
        row["category_label"] = trigger_sync.category_label(row["category"])
    return rows


@router.get("/triggers/config")
def trigger_config() -> dict[str, Any]:
    """Nur lesend: welche Kategorien die Add-on-Konfiguration gerade aktiviert."""
    options = trigger_sync.load_options()
    active = set(options["categories"])
    return {
        "categories": [
            {"key": key, "label": value["label"], "active": key in active}
            for key, value in trigger_sync.CATEGORIES.items()
        ],
        "other_label": trigger_sync.OTHER_LABEL,
        "extra_entities": options["extra_entities"],
        "exclude_entities": options["exclude_entities"],
        "options_found": options["found"],
        "last_sync": trigger_sync.status(),
    }


@router.post("/triggers/sync", dependencies=[Depends(require_admin)])
async def trigger_sync_now() -> dict[str, Any]:
    """Abgleich mit der Add-on-Konfiguration sofort ausfuehren."""
    try:
        result = await trigger_sync.sync(ha_client)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Sync failed: {exc}") from exc
    return result


@router.post("/triggers/bulk", dependencies=[Depends(require_admin)])
def bulk_enable_triggers(payload: TriggerBulkIn) -> dict[str, Any]:
    """Mehrere Trigger auf einmal (de)aktivieren - z.B. eine ganze Kategorie."""
    changed = 0
    with db.cursor() as cur:
        for entity_id in payload.entity_ids:
            cur.execute(
                "UPDATE trigger_entity SET enabled = ? WHERE entity_id = ?",
                (int(payload.enabled), entity_id),
            )
            changed += cur.rowcount
    return {"ok": True, "changed": changed}


@router.put("/triggers/{entity_id}", dependencies=[Depends(require_admin)])
def upsert_trigger(entity_id: str, payload: TriggerIn) -> dict[str, Any]:
    db.execute(
        """
        INSERT INTO trigger_entity (
            entity_id, friendly_name, enabled, trigger_states,
            spawn_chance, type_weights, cooldown_minutes
        ) VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(entity_id) DO UPDATE SET
            friendly_name = excluded.friendly_name,
            enabled = excluded.enabled,
            trigger_states = excluded.trigger_states,
            spawn_chance = excluded.spawn_chance,
            type_weights = excluded.type_weights,
            cooldown_minutes = excluded.cooldown_minutes
        """,
        (
            entity_id, payload.friendly_name, int(payload.enabled),
            json.dumps(payload.trigger_states), payload.spawn_chance,
            json.dumps(payload.type_weights), payload.cooldown_minutes,
        ),
    )
    return {"ok": True, "entity_id": entity_id}


@router.post("/triggers/{entity_id}/fire", dependencies=[Depends(require_admin)])
def fire_trigger(entity_id: str) -> dict[str, Any]:
    """Manually fires one configured trigger right now - a "trigger now" button
    for testing a trigger's setup without waiting for the real Home Assistant
    event. Reuses spawn.process_events with `force=True`, so it bypasses only
    the entity's own cooldown and `spawn_chance` roll; per-user daily cap and
    global cooldown still apply, same as a real event (see process_events)."""
    trigger = db.query_one("SELECT * FROM trigger_entity WHERE entity_id = ?", (entity_id,))
    if trigger is None:
        raise HTTPException(status_code=404, detail="Unknown trigger")
    if not trigger["enabled"]:
        raise HTTPException(status_code=409, detail="Trigger is disabled")

    wanted_states = json.loads(trigger.get("trigger_states") or '["on"]')
    state = wanted_states[0] if wanted_states else "on"
    created = spawn.process_events(
        [{"entity_id": entity_id, "state": state}], [trigger], force=True
    )
    return {"ok": created > 0, "created": created}


@router.delete("/triggers/{entity_id}", dependencies=[Depends(require_admin)])
def delete_trigger(entity_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT source FROM trigger_entity WHERE entity_id = ?", (entity_id,))
    if row and row.get("source") == "config":
        # Wuerde beim naechsten Abgleich ohnehin wieder angelegt.
        raise HTTPException(
            status_code=409,
            detail=(
                "This trigger comes from the add-on configuration. Disable it "
                "here, or add it to exclude_entities in the add-on configuration "
                "to remove it permanently."
            ),
        )
    db.execute("DELETE FROM trigger_entity WHERE entity_id = ?", (entity_id,))
    return {"ok": True}


# ---------------------------------------------------------------- Biome

@router.get("/biomes")
def list_biomes() -> dict[str, Any]:
    data = spawn.biome_data()["biomes"]
    return {
        key: {"label": value.get("label", key), "type_weights": value.get("type_weights", {})}
        for key, value in data.items()
    }


@router.get("/zones")
def list_zone_biomes() -> list[dict[str, Any]]:
    return db.query("SELECT * FROM zone_biome ORDER BY zone_entity_id")


@router.put("/zones/{zone_entity_id:path}", dependencies=[Depends(require_admin)])
def upsert_zone_biome(zone_entity_id: str, payload: ZoneBiomeIn) -> dict[str, Any]:
    if payload.biome not in spawn.biome_data()["biomes"]:
        raise HTTPException(status_code=400, detail=f"Unknown biome: {payload.biome}")
    db.execute(
        """
        INSERT INTO zone_biome (zone_entity_id, biome, enabled) VALUES (?,?,?)
        ON CONFLICT(zone_entity_id) DO UPDATE SET
            biome = excluded.biome, enabled = excluded.enabled
        """,
        (zone_entity_id, payload.biome, int(payload.enabled)),
    )
    return {"ok": True}


@router.delete("/zones/{zone_entity_id:path}", dependencies=[Depends(require_admin)])
def delete_zone_biome(zone_entity_id: str) -> dict[str, Any]:
    db.execute("DELETE FROM zone_biome WHERE zone_entity_id = ?", (zone_entity_id,))
    return {"ok": True}


# ------------------------------------------------------------- Settings

@router.get("/settings")
def get_settings() -> dict[str, Any]:
    return db.all_settings()


def _same_setting_type(default: Any, value: Any) -> bool:
    # bool ist in Python ein int - deshalb bool zuerst und getrennt pruefen.
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(default, bool) and isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float))
    return isinstance(value, type(default))


@router.patch("/settings", dependencies=[Depends(require_admin)])
def patch_settings(payload: dict[str, Any]) -> dict[str, Any]:
    known = set(db.DEFAULT_SETTINGS)
    unknown = set(payload) - known
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown settings: {sorted(unknown)}")
    for key, value in payload.items():
        if not _same_setting_type(db.DEFAULT_SETTINGS[key], value):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid value for {key}: expected "
                       f"{type(db.DEFAULT_SETTINGS[key]).__name__}, got {type(value).__name__}",
            )
    _validate_iv_repeat_catch(payload)
    _validate_rarity_weights(payload)
    _validate_maxed_ivs_spawn_weight_factor(payload)
    _validate_pvp_money(payload)
    _validate_buddy_steps(payload)
    _validate_buddy_finds(payload)
    _validate_breeding(payload)
    _validate_wild_levels(payload)
    _validate_ev_limits(payload)
    _validate_generation_perks(payload)
    for key, value in payload.items():
        db.set_setting(key, value)
    return db.all_settings()


def _validate_rarity_weights(payload: dict[str, Any]) -> None:
    """Spawn-Gewichte je Seltenheits-Stufe: >= 0, und mindestens eine Stufe
    muss ein Gewicht > 0 behalten - sonst gaebe es keine Spawns mehr."""
    keys = [rarity.setting_key(tier) for tier in rarity.TIERS]
    if not any(key in payload for key in keys):
        return
    merged = {key: payload.get(key, db.get_setting(key, db.DEFAULT_SETTINGS[key]))
              for key in keys}
    for key, value in merged.items():
        if float(value) < 0 or float(value) > 100:
            raise HTTPException(status_code=400,
                                detail=f"Invalid value for {key}: must be between 0 and 100")
    if not any(float(value) > 0 for value in merged.values()):
        raise HTTPException(
            status_code=400,
            detail="At least one rarity weight must be greater than 0",
        )


def _validate_maxed_ivs_spawn_weight_factor(payload: dict[str, Any]) -> None:
    """Spawn-Gewichtsfaktor fuer IV-maximierte Arten: 0 (nie mehr) bis 1 (kein Abzug)."""
    key = "maxed_ivs_spawn_weight_factor"
    if key in payload and not 0 <= float(payload[key]) <= 1:
        raise HTTPException(status_code=400,
                            detail=f"Invalid value for {key}: must be between 0 and 1")


def _validate_generation_perks(payload: dict[str, Any]) -> None:
    """Per-generation Pokedex perks (achievements.GEN_CAUGHT_PREFIX): the shiny
    factor must not lower the chance (>= 1)."""
    key = "gen_caught_shiny_factor"
    if key in payload and float(payload[key]) < 1:
        raise HTTPException(status_code=400, detail=f"Invalid value for {key}: must be >= 1")


def _validate_pvp_money(payload: dict[str, Any]) -> None:
    """Preisgeld pro Gegner-Level im PvP: 0 (aus) oder positiv."""
    key = "pvp_money_per_level"
    if key in payload and float(payload[key]) < 0:
        raise HTTPException(status_code=400,
                            detail=f"Invalid value for {key}: must be >= 0")


def _validate_buddy_steps(payload: dict[str, Any]) -> None:
    """Schritte pro Buddy-Level: ganze Zahl 100-1000000."""
    key = "buddy_steps_per_level"
    if key in payload:
        value = payload[key]
        if float(value) != int(value) or not 100 <= int(value) <= 1_000_000:
            raise HTTPException(status_code=400,
                                detail=f"Invalid value for {key}: must be a whole number 100-1000000")


def _validate_buddy_finds(payload: dict[str, Any]) -> None:
    """Find chance per step 0-1; pool = {tier: {weight > 0, items: [known item keys]}}."""
    if "buddy_find_chance" in payload:
        value = payload["buddy_find_chance"]
        if isinstance(value, bool) or not 0 <= float(value) <= 1:
            raise HTTPException(status_code=400,
                                detail="Invalid value for buddy_find_chance: must be between 0 and 1")
    if "buddy_find_pool" in payload:
        pool = payload["buddy_find_pool"]
        known = set(battle.BALLS) | set(economy.catalog_by_key())
        ok = isinstance(pool, dict) and all(
            isinstance(t, dict) and isinstance(t.get("items"), list)
            and isinstance(t.get("weight"), (int, float)) and t["weight"] >= 0
            and all(i in known for i in t["items"])
            for t in pool.values())
        if not ok:
            raise HTTPException(status_code=400, detail="Invalid value for buddy_find_pool")


def _validate_wild_levels(payload: dict[str, Any]) -> None:
    """Wild level range: whole numbers 1-MAX_LEVEL, min <= max (the cap is 100)."""
    keys = ("wild_level_min", "wild_level_max")
    if not any(key in payload for key in keys):
        return
    merged = {key: payload.get(key, db.get_setting(key, db.DEFAULT_SETTINGS[key]))
              for key in keys}
    for key, value in merged.items():
        if isinstance(value, bool) or float(value) != int(value) \
                or not 1 <= int(value) <= battle.MAX_LEVEL:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid value for {key}: must be a whole number 1-{battle.MAX_LEVEL}")
    if int(merged[keys[0]]) > int(merged[keys[1]]):
        raise HTTPException(status_code=400,
                            detail="wild_level_min must not be greater than wild_level_max")


def _validate_breeding(payload: dict[str, Any]) -> None:
    """Zucht: Minuten bis zum Ei 1-100000, passive Ei-Schritte pro Stunde
    0 (nur Schritt-Sensor) bis 100000."""
    for key, low in (("daycare_egg_minutes", 1), ("egg_passive_steps_per_hour", 0)):
        if key in payload:
            value = payload[key]
            if isinstance(value, bool) or float(value) != int(value) \
                    or not low <= int(value) <= 100_000:
                raise HTTPException(status_code=400,
                                    detail=f"Invalid value for {key}: must be a whole number {low}-100000")


def _validate_ev_limits(payload: dict[str, Any]) -> None:
    """EV-Grenzen (siehe app/evs.py): ganze Zahlen 0-1530 (6 x 255), und die
    Gesamtgrenze darf nicht unter der Grenze pro Wert liegen - sonst waere
    kein einzelner Wert je auszubauen."""
    keys = ("ev_max_per_stat", "ev_max_total")
    if not any(key in payload for key in keys):
        return
    merged = {key: payload.get(key, db.get_setting(key, db.DEFAULT_SETTINGS[key]))
              for key in keys}
    for key, value in merged.items():
        if isinstance(value, bool) or float(value) != int(value) or not 0 <= int(value) <= 1530:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid value for {key}: must be a whole number 0-1530")
    if int(merged[keys[0]]) > int(merged[keys[1]]):
        raise HTTPException(
            status_code=400,
            detail="ev_max_per_stat must not be greater than ev_max_total",
        )


def _validate_iv_repeat_catch(payload: dict[str, Any]) -> None:
    """IV-Zuwachs bei Wiederholungsfang: ganze Zahlen, 1 <= min <= max <= 31."""
    keys = ("iv_repeat_catch_min_gain", "iv_repeat_catch_max_gain")
    if not any(key in payload for key in keys):
        return
    merged = {key: payload.get(key, db.get_setting(key, db.DEFAULT_SETTINGS[key]))
              for key in keys}
    for key, value in merged.items():
        if float(value) != int(value) or not 1 <= int(value) <= 31:
            raise HTTPException(status_code=400,
                                detail=f"Invalid value for {key}: must be a whole number 1-31")
    if int(merged[keys[0]]) > int(merged[keys[1]]):
        raise HTTPException(
            status_code=400,
            detail="iv_repeat_catch_min_gain must not be greater than iv_repeat_catch_max_gain",
        )


# --------------------------------------------------------------- Status

@router.get("/status")
def status(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    # Pro Nutzer: die Warteschlange gehoert ihm (siehe app/api/encounters.py),
    # der Zaehler im Kopf muss also zur eigenen Liste passen. Offene Encounter
    # (waiting/active), die dieser Nutzer schon erledigt hat, zaehlen nicht mit.
    counts = {
        row["status"]: row["n"]
        for row in db.query(
            "SELECT e.status, COUNT(*) AS n FROM encounter e "
            "LEFT JOIN encounter_user eu ON eu.encounter_id = e.id AND eu.user_id = ? "
            "WHERE (e.user_id IS NULL OR e.user_id = ?) "
            "AND (e.status NOT IN ('waiting', 'active') OR eu.outcome IS NULL) "
            "GROUP BY e.status",
            (current_user["id"], current_user["id"]),
        )
    }
    species = db.query_one("SELECT COUNT(*) AS n FROM species")
    # Pro Nutzer: eigener Pokédex-Fortschritt, nicht der des ganzen Haushalts.
    dex = db.query_one(
        "SELECT COUNT(DISTINCT species_id) AS n FROM dex_entry WHERE caught_count > 0 AND user_id = ?",
        (current_user["id"],),
    )
    return {
        "poller": poller.status(),
        "encounters": counts,
        "species_in_dex": species["n"] if species else 0,
        "caught_species": dex["n"] if dex else 0,
        "users": len(db.list_users()),
        "triggers": len(db.query("SELECT 1 FROM trigger_entity WHERE enabled = 1")),
    }


@router.post("/poll")
async def poll_now() -> dict[str, Any]:
    """Manuell pollen - praktisch beim Einrichten, um nicht 5 Minuten zu warten."""
    created = await poller.run_once()
    return {"created": created, "poller": poller.status()}
