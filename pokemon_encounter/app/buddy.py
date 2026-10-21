"""Buddy: EIN ausgewaehltes Pokemon pro Nutzer, gespiegelt nach Home Assistant
und per gelaufener Strecke levelbar.

HA-Anbindung ohne Custom Integration: Der Add-on hat ueber den Supervisor-
Token (`homeassistant_api: true`) ohnehin Zugriff auf die Core-API und setzt
per `POST /api/states/sensor.pokemon_buddy_<nutzer>` einen Sensor:

* State = Level (numerisch -> Verlauf/Automationen moeglich)
* `entity_picture` = Sprite-URL (PokeAPI) - Tile-, Entity- und
  Picture-Entity-Karte zeigen das Bild direkt an, siehe DOCS.md.
* Attribute: Name, Art, Typen, Schritt-Fortschritt, ...

Einschraenkung dieses Wegs: Die Entity hat keine unique_id (nicht im UI
umbenennbar) und verschwindet nach einem HA-Neustart, bis der naechste Push
kommt - der Poller pusht bei jedem Durchlauf (Standard alle 5 Minuten).
Eine eigene Integration (oder MQTT-Discovery) wuerde das loesen, ist fuer
"Name + Level + Bild" aber unverhaeltnismaessig.

Schritte: Der Nutzer waehlt einen HA-Sensor (z.B. Schrittzaehler der
Companion App). Pro Poll wird die Differenz zum letzten Messwert gutge-
schrieben; `buddy_steps_per_level` Schritte (Standard 6500 ~ 5 km) ergeben
+1 Level. Distanz-Sensoren (m/km/mi/ft/yd) werden ueber eine feste
Schrittlaenge in Schritte umgerechnet - intern gibt es nur Schritte.
Faellt der Zaehler (Tagesreset um Mitternacht, Handy-Neustart), zaehlt der
neue Wert selbst als Differenz.
"""

from __future__ import annotations

import logging
import random
import re
import unicodedata
from typing import Any

from . import achievements, battle, breeding, db, economy, i18n, rewards
from .ha_client import HomeAssistantClient, HomeAssistantError

log = logging.getLogger(__name__)

ENTITY_PREFIX = "sensor.pokemon_buddy_"
# Mittlere Schrittlaenge fuer die Umrechnung von Distanz-Sensoren:
# 5 km / 0.77 m ~ 6500 Schritte = Standardwert von buddy_steps_per_level.
STRIDE_METERS = 0.77
METERS_PER_UNIT = {"m": 1.0, "km": 1000.0, "mi": 1609.344, "ft": 0.3048, "yd": 0.9144}
STEP_UNITS = {"steps", "step", "schritte"}
# Sprung zwischen zwei Messwerten, der nicht mehr plausibel ist (Sensor-Fehler,
# falsch umgerechnete Einheit) - wird verworfen statt dutzende Level zu geben.
MAX_STEPS_PER_READING = 60_000
MAX_LEVEL = battle.MAX_LEVEL
_SENSOR_ID = re.compile(r"^sensor\.[a-z0-9_]+$")


# ------------------------------------------------------------- Helfer

def entity_id_for(user: dict[str, Any]) -> str:
    """`sensor.pokemon_buddy_<slug>` aus dem HA-Nutzernamen (ASCII, a-z0-9_)."""
    raw = unicodedata.normalize("NFKD", user.get("name") or "")
    slug = re.sub(r"[^a-z0-9]+", "_", raw.encode("ascii", "ignore").decode().lower()).strip("_")
    if not slug:
        slug = re.sub(r"[^a-z0-9]+", "", str(user["id"]).lower())[:12] or "user"
    return ENTITY_PREFIX + slug


def valid_sensor_id(entity_id: str) -> bool:
    return bool(_SENSOR_ID.match(entity_id or ""))


def to_steps(value: Any, unit: str | None) -> float | None:
    """Sensorwert in Schritte umrechnen. None = nicht verwertbar
    ('unavailable', unbekannte Einheit, negativ)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number != number:  # NaN
        return None
    unit_key = (unit or "").strip().lower()
    if not unit_key or unit_key in STEP_UNITS:
        return number
    if unit_key in METERS_PER_UNIT:
        return number * METERS_PER_UNIT[unit_key] / STRIDE_METERS
    return None


def is_step_candidate(state: dict[str, Any]) -> bool:
    """Taugt eine HA-Entity als Schritt-/Distanzquelle? (fuer die Auswahl)"""
    entity_id = state.get("entity_id", "")
    if not entity_id.startswith("sensor."):
        return False
    attributes = state.get("attributes") or {}
    unit = (attributes.get("unit_of_measurement") or "").strip().lower()
    return (
        unit in STEP_UNITS
        or attributes.get("device_class") == "distance"
        or ("step" in entity_id and (not unit or unit in STEP_UNITS))
    )


def steps_per_level() -> int:
    return max(1, int(db.get_setting("buddy_steps_per_level",
                                     db.DEFAULT_SETTINGS["buddy_steps_per_level"])))


def _buddy_row(user: dict[str, Any]) -> dict[str, Any] | None:
    owned_id = user.get("buddy_pokemon_id")
    if owned_id is None:
        return None
    return db.query_one(
        "SELECT * FROM owned_pokemon WHERE id = ? AND user_id = ?", (owned_id, user["id"])
    )


# ------------------------------------------------------- Aenderungen

def set_buddy(user_id: str, owned_id: int | None) -> None:
    """Buddy setzen/entfernen. Angesparte Schritte gehoeren zum bisherigen
    Buddy und verfallen beim Wechsel (gelaufen wird mit einem Pokemon)."""
    if owned_id is not None:
        row = db.query_one("SELECT user_id FROM owned_pokemon WHERE id = ?", (owned_id,))
        if row is None or row["user_id"] != user_id:
            raise ValueError("not_owned")
    user = db.get_user(user_id) or {}
    if user.get("buddy_pokemon_id") == owned_id:
        return
    db.execute(
        "UPDATE app_user SET buddy_pokemon_id = ?, buddy_steps = 0 WHERE id = ?",
        (owned_id, user_id),
    )


def set_sensor(user_id: str, entity_id: str | None) -> None:
    """Schritt-Sensor setzen/entfernen. Der erste Messwert danach ist nur die
    Basis - es wird nichts rueckwirkend gutgeschrieben."""
    if entity_id is not None and not valid_sensor_id(entity_id):
        raise ValueError("invalid_sensor")
    db.execute(
        "UPDATE app_user SET buddy_sensor = ?, buddy_sensor_last = NULL WHERE id = ?",
        (entity_id, user_id),
    )


def apply_reading(user_id: str, steps: float) -> list[int]:
    """Neuen Sensorwert (in Schritten) verbuchen. Rueckgabe: erreichte Level.
    Die Schritte gehen auch an bruetende Eier (siehe breeding.py) - dafuer
    braucht es keinen Buddy."""
    user = db.get_user(user_id)
    if user is None:
        return []
    last = user.get("buddy_sensor_last")
    db.execute("UPDATE app_user SET buddy_sensor_last = ? WHERE id = ?", (steps, user_id))
    if last is None:
        return []
    # Zaehler zurueckgesetzt (Tageswechsel, Neustart): neuer Wert = Differenz.
    delta = steps - last if steps >= last else steps
    if delta <= 0 or delta > MAX_STEPS_PER_READING:
        if delta > MAX_STEPS_PER_READING:
            log.warning("Buddy: unplausibler Sprung von %.0f Schritten bei %s verworfen", delta, user_id)
        return []
    breeding.credit_steps(user_id, delta)
    row = _buddy_row(user)
    if row is None:
        return []
    steps_int = int(round(delta))
    roll_finds(user_id, steps_int)
    return _credit_steps(user, row, steps_int)


def roll_finds(user_id: str, steps: int, rng: random.Random | None = None) -> list[dict[str, str]]:
    """Every step has `buddy_find_chance` to turn up an item on the ground
    (a per-step probability, not a fixed interval). Finds go straight into the
    bag and into `buddy_find`. Rueckgabe: [{item_key, tier}]."""
    rng = rng or random
    chance = float(db.get_setting("buddy_find_chance", db.DEFAULT_SETTINGS["buddy_find_chance"]))
    pool = db.get_setting("buddy_find_pool", db.DEFAULT_SETTINGS["buddy_find_pool"])
    tiers = [(name, tier) for name, tier in (pool or {}).items()
             if tier.get("items") and float(tier.get("weight", 0)) > 0]
    if chance <= 0 or steps <= 0 or not tiers:
        return []
    count = sum(1 for _ in range(steps) if rng.random() < chance)
    found: list[dict[str, str]] = []
    for _ in range(count):
        name, tier = rng.choices(tiers, weights=[float(t["weight"]) for _, t in tiers])[0]
        found.append({"item_key": rng.choice(tier["items"]), "tier": name})
    for find in found:
        rewards.grant(user_id, [{"key": find["item_key"], "quantity": 1}])
        db.execute(
            "INSERT INTO buddy_find (user_id, item_key, tier, found_at) "
            "VALUES (?,?,?,datetime('now'))",
            (user_id, find["item_key"], find["tier"]),
        )
        log.info("Buddy von %s hat %s gefunden (%s)", user_id, find["item_key"], find["tier"])
    return found


def recent_finds(user: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
    lang = user.get("language") or "en"
    rows = db.query(
        "SELECT item_key, tier, found_at FROM buddy_find WHERE user_id = ? "
        "ORDER BY id DESC LIMIT ?", (user["id"], limit))
    return [r | {"label": economy.item_label(r["item_key"], lang)} for r in rows]


def _credit_steps(user: dict[str, Any], row: dict[str, Any], steps: int) -> list[int]:
    per_level = steps_per_level()
    banked = int(user.get("buddy_steps") or 0) + steps
    level = row["level"]
    gained: list[int] = []
    while banked >= per_level and level < MAX_LEVEL:
        banked -= per_level
        level += 1
        gained.append(level)
    if level >= MAX_LEVEL:
        banked = 0
    if gained:
        # Wie ein Sonderbonbon: EXP mindestens auf den Beginn des neuen Levels.
        species = db.query_one("SELECT growth_rate FROM species WHERE id = ?", (row["species_id"],)) or {}
        exp = max(row["exp"], battle.exp_for_level(level, species.get("growth_rate") or "medium_fast"))
        db.execute("UPDATE owned_pokemon SET level = ?, exp = ? WHERE id = ?", (level, exp, row["id"]))
        achievements.check_level100(
            user["id"], [{"owned_id": row["id"], "level_to": level}], user.get("language") or "en",
        )
    db.execute(
        "UPDATE app_user SET buddy_steps = ?, buddy_steps_total = buddy_steps_total + ? WHERE id = ?",
        (banked, steps, user["id"]),
    )
    return gained


# --------------------------------------------------------- Darstellung

def _species_name(species: dict[str, Any], lang: str) -> str:
    if lang == "de" and species.get("name_de"):
        return species["name_de"]
    return species.get("name_en") or species.get("name_de") or species["name"]


def status(user: dict[str, Any]) -> dict[str, Any]:
    """Buddy-Status fuer die Web-App und als Grundlage des HA-Zustands."""
    row = _buddy_row(user)
    per_level = steps_per_level()
    result: dict[str, Any] = {
        "entity_id": entity_id_for(user),
        "sensor": user.get("buddy_sensor"),
        "has_baseline": user.get("buddy_sensor_last") is not None,
        "steps": int(user.get("buddy_steps") or 0),
        "steps_per_level": per_level,
        "steps_total": int(user.get("buddy_steps_total") or 0),
        "pokemon": None,
        "finds": recent_finds(user),
    }
    if row is None:
        return result
    species = db.query_one("SELECT * FROM species WHERE id = ?", (row["species_id"],))
    sprite = species.get("sprite_url")
    if sprite and row.get("shiny"):
        sprite = sprite.replace("/sprites/pokemon/", "/sprites/pokemon/shiny/")
    result["pokemon"] = {
        "id": row["id"],
        "species_id": species["id"],
        "species": _species_name(species, user.get("language") or "en"),
        "name": row.get("nickname") or _species_name(species, user.get("language") or "en"),
        "level": row["level"],
        "shiny": bool(row.get("shiny")),
        "types": [t for t in (species["type1"], species.get("type2")) if t],
        "sprite_url": sprite,
        "artwork_url": (
            "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/"
            f"other/official-artwork/{'shiny/' if row.get('shiny') else ''}{species['id']}.png"
        ),
    }
    return result


def ha_state(user: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """(state, attributes) fuer den HA-Sensor - None ohne Buddy."""
    info = status(user)
    mon = info["pokemon"]
    if mon is None:
        return None
    lang = user.get("language") or "en"
    at_max = mon["level"] >= MAX_LEVEL
    return str(mon["level"]), {
        "friendly_name": i18n.t(lang, "buddy.ha_friendly_name",
                                user=user.get("display_name") or user.get("name") or "",
                                name=mon["name"]),
        "entity_picture": mon["sprite_url"],
        "icon": "mdi:pokeball",
        "state_class": "measurement",
        "pokemon_name": mon["name"],
        "species": mon["species"],
        "species_id": mon["species_id"],
        "level": mon["level"],
        "shiny": mon["shiny"],
        "types": mon["types"],
        "artwork_url": mon["artwork_url"],
        "steps": info["steps"],
        "steps_per_level": info["steps_per_level"],
        "steps_to_next_level": None if at_max else info["steps_per_level"] - info["steps"],
        "progress_percent": 100 if at_max else round(100 * info["steps"] / info["steps_per_level"]),
        "steps_total": info["steps_total"],
        "step_sensor": info["sensor"],
    }


# ------------------------------------------------------------ HA-Sync

async def push(client: HomeAssistantClient, user: dict[str, Any]) -> None:
    """Aktuellen Buddy-Zustand nach HA schreiben (ohne Buddy: Entity entfernen)."""
    payload = ha_state(user)
    if payload is None:
        await client.delete_state(entity_id_for(user))
    else:
        await client.set_state(entity_id_for(user), *payload)


async def sync(client: HomeAssistantClient) -> int:
    """Pro Poll: Schritt-Sensoren lesen, Level vergeben, Sensoren pushen.
    Rueckgabe: Anzahl insgesamt erreichter Level (fuer Log/Tests)."""
    users = db.query(
        "SELECT * FROM app_user WHERE buddy_pokemon_id IS NOT NULL OR buddy_sensor IS NOT NULL"
    )
    total = 0
    for user in users:
        try:
            if user.get("buddy_pokemon_id") is not None and _buddy_row(user) is None:
                # Buddy existiert nicht mehr (freigelassen/geloescht).
                set_buddy(user["id"], None)
            if user.get("buddy_sensor"):
                state = await client.get_state(user["buddy_sensor"])
                if state:
                    unit = (state.get("attributes") or {}).get("unit_of_measurement")
                    steps = to_steps(state.get("state"), unit)
                    if steps is not None:
                        gained = apply_reading(user["id"], steps)
                        if gained:
                            log.info("Buddy von %s erreicht Level %s", user["id"], gained[-1])
                        total += len(gained)
            fresh = db.get_user(user["id"])
            if fresh and user.get("buddy_pokemon_id") is not None:
                await push(client, fresh)
        except HomeAssistantError as exc:
            log.warning("Buddy-Sync fuer %s fehlgeschlagen: %s", user["id"], exc)
    return total
