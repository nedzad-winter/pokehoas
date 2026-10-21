"""Abilities: catalog (`data/abilities.json`) plus mapping of species ->
possible ability from PokeAPI.

The catalog part is deliberately structured like `held_items.py`: each
implemented ability has an `effect` type. The battle engine (`battle.py`) does not check effects
itself, but fires events on the event hub (`battle_events.py`) - status
immunity, contact status, type immunity, STAB, stat and damage modifiers,
end of turn. The handler per `(event, effect)` lives in `ability_effects.py`;
a new ability needs a catalog entry here plus a handler there.

PokeAPI knows far more abilities than this battle engine actually implements.
The catalog still lists every main-series ability (`scripts/build_ability_catalog.py`
fills in names and descriptions from PokeAPI): the ones without an `effect`
carry `inactive` instead - why they do nothing here (double battles only,
weather, overworld, a missing battle mechanic, or simply not done yet), shown
to players instead of a bare "no effect" note. An ability without effect, or
one missing from the catalog entirely (a brand-new one), must be treated
everywhere as a pure no-op, never as an error - see `seed_from_pokeapi`
below, which stores the complete, real mapping.

Which species can have which abilities comes from PokeAPI (`/api/v2/pokemon/{id}`,
field "abilities": each entry with `slot` 1/2 = regular, 3 = always the hidden,
`is_hidden` confirms it) and is stored in the `species_ability` table (slot 0/1
= regular, 2 = hidden - see `db.py`). Seeding follows the same pattern as
`evolution.py`: in the background, idempotent, INDEPENDENT from Pokedex seeding
(also runs on an existing production DB after whose `species` has long been filled,
but whose `species_ability` is still empty), with small offline fallback in
`data/fallback_abilities.json` for the supplied mini-Pokedex. Deliberately a
separate HTTP request instead of an extension of `seed.py`'s request of the same
route: this keeps the ability seeding completely independent and runnable (even
on a DB that was long ago seeded without ability data) - the double request per
species on the first run of a FRESH DB is the price.

Which ability an INDIVIDUAL Pokemon has is rolled once during spawning or
starter selection (`spawn.roll_ability`) and never changes afterwards - unlike
status, it doesn't live in the `battle_session` state, but directly in
`encounter.ability` / `owned_pokemon.ability` and is read fresh from the DB
each time `combatant_from_encounter`/`combatant_from_owned` is called (like
`held_item`).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from . import db

log = logging.getLogger(__name__)

POKEAPI = "https://pokeapi.co/api/v2"
DATA_DIR = Path(__file__).parent / "data"

# Increment to reseed after an update.
SEED_VERSION = "1"
SEED_META_KEY = "abilities_pokeapi"

HIDDEN_SLOT = 2

_abilities: dict[str, dict[str, Any]] | None = None
_task: asyncio.Task | None = None


# --------------------------------------------------------------- Catalog

def catalog() -> dict[str, dict[str, Any]]:
    global _abilities
    if _abilities is None:
        raw = json.loads((DATA_DIR / "abilities.json").read_text(encoding="utf-8"))
        _abilities = {key: {"key": key, **value} for key, value in raw.items()}
    return _abilities


def get(key: str | None) -> dict[str, Any] | None:
    if not key:
        return None
    return catalog().get(key)


def effect_of(key: str | None) -> str | None:
    """Battle effect type, None for abilities without one (see `inactive`)."""
    ability = get(key)
    return ability.get("effect") if ability else None


def label(key: str | None, lang: str = "en") -> str | None:
    """Display name. Unlike `held_items.label` (whose catalog is ALWAYS
    complete), this falls back to a readable version of the raw PokeAPI key
    if the ability is not in the catalog at all (e.g. a brand-new one PokeAPI
    added after `scripts/build_ability_catalog.py` last ran).

    `lang`: for server-side generated text (battle log) that needs the
    right language before the response to the frontend - the frontend
    itself continues to choose via `describe()`s name/name_de fields."""
    if not key:
        return None
    ability = get(key)
    if ability:
        return (ability.get("name_de") if lang == "de" else None) or ability["name"]
    return key.replace("_", " ").title()


def describe(key: str | None) -> dict[str, Any] | None:
    """Display info for the frontend (name + description), even for an ability
    without a battle effect (see `label`). `name` is English (catalog text
    or readable PokeAPI key as fallback), `name_de` is the German translation -
    the frontend chooses based on the currently set language. `inactive` is
    set for abilities without a battle effect: why they do nothing here
    (i18n `ability.inactive.<reason>`)."""
    if not key:
        return None
    ability = get(key)
    return {
        "key": key,
        "name": label(key),
        "name_de": ability.get("name_de") if ability else None,
        "description": ability.get("description") if ability else None,
        "description_de": ability.get("description_de") if ability else None,
        "inactive": ability.get("inactive") if ability else None,
    }


# ------------------------------------------------------- Species Mapping

def _slot_for(entry: dict[str, Any]) -> int:
    if entry.get("is_hidden"):
        return HIDDEN_SLOT
    return min(max(int(entry.get("slot") or 1) - 1, 0), 1)


def rows_from_pokemon(species_id: int, pokemon_doc: dict[str, Any]) -> list[tuple[int, str, int]]:
    """Converts the `abilities` field from `/api/v2/pokemon/{id}` into rows
    for `species_ability`."""
    rows = []
    for entry in pokemon_doc.get("abilities") or []:
        name = ((entry.get("ability") or {}).get("name") or "").strip()
        if not name:
            continue
        rows.append((species_id, name.replace("-", "_"), _slot_for(entry)))
    return rows


def _write_rows(rows: list[tuple[int, str, int]]) -> int:
    if not rows:
        return 0
    with db.cursor() as cur:
        cur.executemany(
            "INSERT OR IGNORE INTO species_ability (species_id, ability_key, slot) VALUES (?,?,?)",
            rows,
        )
    return len(rows)


def seed_from_fallback() -> int:
    """Supplied mappings for the small offline Pokedex. Does not overwrite anything."""
    path = DATA_DIR / "fallback_abilities.json"
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = [
        (int(species_id), entry["ability"], int(entry["slot"]))
        for species_id, entries in data.items()
        for entry in entries
    ]
    return _write_rows(rows)


def pokeapi_seeded() -> bool:
    row = db.query_one("SELECT value FROM seed_meta WHERE key = ?", (SEED_META_KEY,))
    return bool(row and row["value"] == SEED_VERSION)


def _species_missing_abilities() -> list[int]:
    """All species IDs without an entry in `species_ability` - also works
    on a long-since seeded production DB, not just an empty table (see module docstring)."""
    rows = db.query(
        "SELECT id FROM species WHERE id NOT IN (SELECT DISTINCT species_id FROM species_ability)"
    )
    return [row["id"] for row in rows]


async def seed_from_pokeapi(concurrency: int = 8, retries: int = 2) -> int:
    """Downloads `/api/v2/pokemon/{id}` for each species without an ability entry."""
    species_ids = _species_missing_abilities()
    if not species_ids:
        return 0

    sem = asyncio.Semaphore(concurrency)
    rows: list[tuple[int, str, int]] = []
    failures = 0

    async with httpx.AsyncClient(timeout=20) as client:
        async def fetch(species_id: int) -> None:
            nonlocal failures
            for attempt in range(retries + 1):
                try:
                    async with sem:
                        response = await client.get(f"{POKEAPI}/pokemon/{species_id}")
                    response.raise_for_status()
                    rows.extend(rows_from_pokemon(species_id, response.json()))
                    return
                except Exception as exc:  # noqa: BLE001 - Network is best effort
                    if attempt == retries:
                        failures += 1
                        log.debug("Abilities for #%s failed: %s", species_id, exc)
                    else:
                        await asyncio.sleep(1 + attempt)

        await asyncio.gather(*(fetch(sid) for sid in species_ids))

    written = _write_rows(rows)
    if failures == 0:
        db.execute(
            "INSERT INTO seed_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SEED_META_KEY, SEED_VERSION),
        )
    log.info(
        "Abilities seeded: %s rows for %s of %s species (%s failures)",
        written, len(species_ids) - failures, len(species_ids), failures,
    )
    return written


async def ensure_seeded(after: "asyncio.Task | None" = None) -> None:
    """Background seeding on startup. Never raises.

    `after`: optional task to wait for first before checking which species
    still lack abilities (see `_species_missing_abilities`). On a FRESH DB,
    Pokedex seeding also runs in the background during the same app start -
    without waiting for it, this check would incorrectly treat a still-empty
    `species` table as "nothing is missing" and seed nothing. An error there
    is deliberately only logged, not propagated - `seed.py` already reports
    it itself, and our own check below works fine either way with a (still
    empty) table.
    """
    if after is not None:
        try:
            await after
        except Exception as exc:  # noqa: BLE001
            log.debug("Wait for Pokedex seeding failed: %s", exc)
    try:
        seed_from_fallback()
        if pokeapi_seeded() and not _species_missing_abilities():
            return
        from .seed import reachable  # local import: seed.py stays unchanged

        if not await reachable():
            log.warning("PokeAPI not reachable - using fallback abilities")
            return
        await seed_from_pokeapi()
    except Exception as exc:  # noqa: BLE001
        log.warning("Ability seeding failed: %s", exc)


def start_background_seed(after: "asyncio.Task | None" = None) -> None:
    """Starts `ensure_seeded` as a task and keeps a reference to it
    (otherwise it can be garbage collected)."""
    global _task
    _task = asyncio.create_task(ensure_seeded(after))
