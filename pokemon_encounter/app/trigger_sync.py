"""Automatische Trigger aus der Add-on-Konfiguration.

Der Nutzer waehlt im Konfigurations-Tab des Add-ons (Supervisor schreibt das
nach /data/options.json) Trigger-KATEGORIEN aus - z.B. "alle Bewegungsmelder"
- plus optionale Listen mit zusaetzlichen und auszuschliessenden Entity-IDs.
Dieses Modul gleicht diese Auswahl bei jedem Poll gegen die aktuellen
HA-Zustaende ab und haelt die Tabelle `trigger_entity` synchron:

* Neue Treffer werden mit Kategorie-Defaults eingefuegt (source='config').
* Bestehende Zeilen werden NIE veraendert - Anpassungen aus dem Trigger-Tab
  (Spawn-Chance, Typ-Bonus, aktiv/inaktiv, ...) bleiben erhalten.
* Faellt eine Entity aus der Auswahl (Kategorie abgewaehlt, ausgeschlossen,
  in HA geloescht), wird ihre 'config'-Zeile entfernt.
* Manuell angelegte Zeilen (source='manual') fasst der Abgleich nie an.

Filter gegen "Muell", abgeleitet aus einem echten Haushalt (siehe DOCS.md):
Konfigurations-/Diagnose-Entities (entity_category), ausgeblendete und
deaktivierte Entities sowie Gruppen (und Template-Binary-Sensoren, die
meist nur andere Sensoren zusammenfassen) - sonst loest ein Ereignis doppelt
aus. Explizit in `extra_entities` genannte IDs umgehen diese Filter.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db, spawn
from .config import settings

log = logging.getLogger(__name__)

# Reihenfolge = Anzeige-Reihenfolge in der Web-App.
# `spawn_chance` darf ein Dict pro Domain sein.
CATEGORIES: dict[str, dict[str, Any]] = {
    "motion": {
        "label": "Motion & Presence",
        "domains": {"binary_sensor"},
        "device_classes": {"motion", "occupancy", "presence"},
        "trigger_states": ["on"],
        # Haupt-Trigger ("durchs hohe Gras laufen").
        "spawn_chance": 0.25,
        "default": True,
    },
    "door_window": {
        "label": "Doors & Windows",
        "domains": {"binary_sensor"},
        "device_classes": {"door", "window", "opening", "garage_door"},
        # "on" = geoeffnet.
        "trigger_states": ["on"],
        "spawn_chance": 0.15,
        "default": True,
    },
    "lights_switches": {
        "label": "Lights & Switches",
        "domains": {"light", "switch"},
        "trigger_states": ["on"],
        # Lichter werden sehr oft geschaltet und bringen einen Elektro-Bonus
        # mit - niedrige Chance, damit die Warteschlange nicht mit
        # Elektro-Pokémon volllaeuft. Schalter sind oft Steckdosen/Geraete,
        # die noch haeufiger schalten -> noch etwas niedriger.
        "spawn_chance": {"light": 0.05, "switch": 0.03},
        "default": True,
    },
    "media_player": {
        "label": "Media Players",
        "domains": {"media_player"},
        "trigger_states": ["playing"],
        "spawn_chance": 0.10,
    },
    "vacuum": {
        "label": "Vacuums",
        "domains": {"vacuum"},
        "trigger_states": ["cleaning"],
        # Seltenes Ereignis (1-2x am Tag) -> darf sich lohnen.
        "spawn_chance": 0.50,
    },
    "climate": {
        "label": "Climate & Heating",
        "domains": {"climate"},
        "trigger_states": ["heat", "cool", "heat_cool", "auto", "dry", "fan_only"],
        "spawn_chance": 0.20,
    },
    "cover": {
        "label": "Blinds, Gates & Curtains",
        "domains": {"cover"},
        "trigger_states": ["open"],
        "spawn_chance": 0.10,
    },
    "lock": {
        "label": "Locks",
        "domains": {"lock"},
        "trigger_states": ["unlocked"],
        "spawn_chance": 0.15,
    },
    "fan": {
        "label": "Fans",
        "domains": {"fan"},
        "trigger_states": ["on"],
        "spawn_chance": 0.10,
    },
    "person": {
        "label": "People (zone changes)",
        "domains": {"person"},
        # Leer = jeder Zustandswechsel (Ankunft in/Verlassen einer Zone).
        "trigger_states": [],
        "spawn_chance": 0.30,
    },
}
DEFAULT_CATEGORIES = [key for key, value in CATEGORIES.items() if value.get("default")]

OTHER = "other"
OTHER_LABEL = "Other"
# Fuer Extras ohne passende Kategorie.
OTHER_DEFAULTS = {"trigger_states": ["on"], "spawn_chance": 0.20}

UNAVAILABLE_STATES = {"unavailable", "unknown", "", None}
ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

# Laufzeit-Status des letzten Abgleichs (fuer die Web-App).
_last_sync: dict[str, Any] = {"at": None, "ok": None, "error": None}
# entity_id -> Kategorie fuer ALLE HA-Entities beim letzten Abgleich; damit
# lassen sich auch manuelle Trigger in der Web-App gruppieren.
_known_categories: dict[str, str] = {}


# ------------------------------------------------------------------ Optionen

ENV_OVERRIDES = {
    "trigger_categories": "POKEMON_TRIGGER_CATEGORIES",
    "extra_entities": "POKEMON_EXTRA_ENTITIES",
    "exclude_entities": "POKEMON_EXCLUDE_ENTITIES",
}


def options_path() -> Path:
    override = os.getenv("POKEMON_OPTIONS_FILE")
    if override:
        return Path(override)
    # Supervisor schreibt die Add-on-Optionen nach /data/options.json
    # (= POKEMON_DATA_DIR im Add-on).
    return settings.data_dir / "options.json"


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        item = item.strip().lower()
        if item and item not in out:
            out.append(item)
    return out


def load_options() -> dict[str, Any]:
    """Liest die Trigger-Optionen aus options.json.

    Fehlt die Datei oder ein Schluessel, gelten die Defaults aus config.yaml.
    Eine vorhandene, aber LEERE Kategorie-Liste bedeutet bewusst: keine
    automatischen Trigger.
    """
    path = options_path()
    raw: dict[str, Any] = {}
    found = False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        found = isinstance(raw, dict)
        if not found:
            raw = {}
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log.warning("Add-on-Optionen %s nicht lesbar: %s", path, exc)

    # Standalone Docker (no Supervisor, no options.json): the same three options
    # as comma/space separated env vars. A set env var wins over the file, an
    # empty POKEMON_TRIGGER_CATEGORIES means "no automatic triggers".
    for key, env_name in ENV_OVERRIDES.items():
        value = os.getenv(env_name)
        if value is not None:
            raw[key] = [part for part in value.replace(",", " ").split() if part]
            found = True

    if "trigger_categories" in raw:
        categories = [c for c in _clean_list(raw.get("trigger_categories")) if c in CATEGORIES]
        unknown = [c for c in _clean_list(raw.get("trigger_categories")) if c not in CATEGORIES]
        if unknown:
            log.warning("Unbekannte Trigger-Kategorien ignoriert: %s", unknown)
    else:
        categories = list(DEFAULT_CATEGORIES)

    extras = []
    for entity_id in _clean_list(raw.get("extra_entities")):
        if ENTITY_ID_RE.match(entity_id):
            extras.append(entity_id)
        else:
            log.warning("extra_entities: ungueltige Entity-ID ignoriert: %r", entity_id)

    return {
        "found": found,
        "path": str(path),
        "categories": categories,
        "extra_entities": extras,
        # Muster mit * und ? erlaubt (fnmatch), z.B. "switch.*_container".
        "exclude_entities": _clean_list(raw.get("exclude_entities")),
    }


# ------------------------------------------------------------ Klassifikation

def classify(entity_id: str, device_class: str | None = None) -> str | None:
    """Kategorie einer Entity, unabhaengig davon, ob die Kategorie aktiv ist."""
    domain = entity_id.split(".", 1)[0]
    for key, category in CATEGORIES.items():
        if domain not in category["domains"]:
            continue
        classes = category.get("device_classes")
        if classes and device_class not in classes:
            continue
        return key
    return None


def category_label(key: str | None) -> str:
    if key in CATEGORIES:
        return CATEGORIES[key]["label"]
    return OTHER_LABEL


def category_for_row(row: dict[str, Any]) -> str:
    """Kategorie fuer die Anzeige einer trigger_entity-Zeile."""
    return (
        row.get("category")
        or _known_categories.get(row["entity_id"])
        or classify(row["entity_id"])
        or OTHER
    )


def defaults_for(entity_id: str, category: str | None, device_class: str | None) -> dict[str, Any]:
    """Startwerte fuer einen neu angelegten Trigger."""
    config = CATEGORIES.get(category or "", OTHER_DEFAULTS)
    chance = config["spawn_chance"]
    if isinstance(chance, dict):
        chance = chance.get(entity_id.split(".", 1)[0], min(chance.values()))
    return {
        "trigger_states": list(config["trigger_states"]),
        "spawn_chance": float(chance),
        "type_weights": spawn.default_type_weights_for(entity_id, device_class),
    }


def junk_reason(state: dict[str, Any], registry_entry: dict[str, Any] | None) -> str | None:
    """Warum eine Entity trotz passender Kategorie KEIN Trigger werden soll."""
    domain = state["entity_id"].split(".", 1)[0]
    attributes = state.get("attributes") or {}
    if registry_entry:
        if registry_entry.get("disabled_by"):
            return "deaktiviert"
        if registry_entry.get("hidden_by"):
            return "ausgeblendet"
        if registry_entry.get("entity_category"):
            return f"entity_category:{registry_entry['entity_category']}"
        platform = registry_entry.get("platform")
        if platform == "group":
            return "gruppe"
        if platform == "template" and domain == "binary_sensor":
            return "template"
    # Alte YAML-Gruppen / Licht-Gruppen ohne Registry-Eintrag.
    if isinstance(attributes.get("entity_id"), list):
        return "gruppe"
    return None


def _excluded(entity_id: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(entity_id, pattern) for pattern in patterns)


def resolve(
    states: list[dict[str, Any]],
    registry: list[dict[str, Any]],
    options: dict[str, Any],
) -> dict[str, Any]:
    """Welche Entities gehoeren laut Add-on-Konfiguration zu den Triggern?

    Rueckgabe: {"selected": {entity_id: info}, "skipped": Counter,
    "known": {entity_id: category}}. Auch gerade nicht verfuegbare Entities
    zaehlen zur Auswahl (damit ein kurzer Funkaussetzer keine Anpassungen
    loescht); eingefuegt werden sie aber erst, wenn sie verfuegbar sind.
    """
    active = set(options["categories"])
    excludes = options["exclude_entities"]
    registry_by_id = {entry["entity_id"]: entry for entry in registry if "entity_id" in entry}
    states_by_id = {state["entity_id"]: state for state in states if "entity_id" in state}

    selected: dict[str, dict[str, Any]] = {}
    skipped: Counter = Counter()
    known: dict[str, str] = {}

    for entity_id, state in states_by_id.items():
        attributes = state.get("attributes") or {}
        device_class = attributes.get("device_class")
        category = classify(entity_id, device_class)
        if category is None:
            continue
        known[entity_id] = category
        if category not in active:
            continue
        reason = junk_reason(state, registry_by_id.get(entity_id))
        if reason:
            skipped[reason] += 1
            continue
        if _excluded(entity_id, excludes):
            skipped["ausgeschlossen"] += 1
            continue
        selected[entity_id] = {
            "category": category,
            "device_class": device_class,
            "friendly_name": attributes.get("friendly_name") or entity_id,
            "available": state.get("state") not in UNAVAILABLE_STATES,
            "extra": False,
        }

    for entity_id in options["extra_entities"]:
        if _excluded(entity_id, excludes):
            skipped["ausgeschlossen"] += 1
            continue
        state = states_by_id.get(entity_id) or {}
        attributes = state.get("attributes") or {}
        device_class = attributes.get("device_class")
        selected[entity_id] = {
            "category": classify(entity_id, device_class) or OTHER,
            "device_class": device_class,
            "friendly_name": attributes.get("friendly_name") or entity_id,
            "available": True,  # explizit gewuenscht -> immer uebernehmen
            "extra": True,
        }

    return {"selected": selected, "skipped": skipped, "known": known}


# ------------------------------------------------------------------ Abgleich

def apply_selection(selected: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Schreibt die Auswahl in `trigger_entity` (siehe Modul-Docstring)."""
    existing = {
        row["entity_id"]: row["source"]
        for row in db.query("SELECT entity_id, source FROM trigger_entity")
    }
    added: list[str] = []
    removed: list[str] = []

    with db.cursor() as cur:
        for entity_id, info in selected.items():
            if entity_id in existing:
                continue  # manuell oder schon uebernommen -> nie ueberschreiben
            if not info["available"]:
                continue
            defaults = defaults_for(entity_id, info["category"], info["device_class"])
            cur.execute(
                """
                INSERT OR IGNORE INTO trigger_entity (
                    entity_id, friendly_name, enabled, trigger_states,
                    spawn_chance, type_weights, cooldown_minutes, source, category
                ) VALUES (?,?,1,?,?,?,0,'config',?)
                """,
                (
                    entity_id, info["friendly_name"],
                    json.dumps(defaults["trigger_states"]), defaults["spawn_chance"],
                    json.dumps(defaults["type_weights"]), info["category"],
                ),
            )
            if cur.rowcount:
                added.append(entity_id)

        for entity_id, source in existing.items():
            if source == "config" and entity_id not in selected:
                cur.execute(
                    "DELETE FROM trigger_entity WHERE entity_id = ? AND source = 'config'",
                    (entity_id,),
                )
                removed.append(entity_id)

    return {"added": added, "removed": removed}


async def sync(client: Any) -> dict[str, Any]:
    """Einmal abgleichen. Wirft bei HA-Fehlern - dann bleibt alles, wie es ist."""
    options = load_options()
    now = datetime.now(timezone.utc).isoformat()
    try:
        states = await client.list_states()
        if not states:
            # HA startet gerade o.ae. - lieber nichts entfernen.
            raise RuntimeError("HA liefert keine Zustaende")
        # Ohne Registry keine Muell-Filter -> lieber gar nicht abgleichen,
        # statt kurz hunderte Konfigurations-Schalter einzufuegen.
        registry = await client.list_entity_registry()
    except Exception as exc:
        _last_sync.update({"at": now, "ok": False, "error": str(exc)})
        raise

    result = resolve(states, registry, options)
    changes = apply_selection(result["selected"])
    _known_categories.clear()
    _known_categories.update(result["known"])

    per_category = Counter(info["category"] for info in result["selected"].values())
    _last_sync.clear()
    _last_sync.update(
        {
            "at": now,
            "ok": True,
            "error": None,
            "selected": len(result["selected"]),
            "per_category": dict(per_category),
            "skipped": dict(result["skipped"]),
            "added": len(changes["added"]),
            "removed": len(changes["removed"]),
        }
    )
    if changes["added"] or changes["removed"]:
        log.info(
            "Trigger-Abgleich: %s neu, %s entfernt (%s in Auswahl)",
            len(changes["added"]), len(changes["removed"]), len(result["selected"]),
        )
    return {**_last_sync, "changes": changes}


def status() -> dict[str, Any]:
    return dict(_last_sync)
