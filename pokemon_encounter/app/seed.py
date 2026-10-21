"""Pokédex-Seeding.

Beim ersten Start wird der Pokédex aus der PokeAPI geladen und in SQLite
abgelegt. Danach laeuft alles offline. Eine neue Generation nachzuziehen ist
ein erneuter Aufruf von `seed_species()` mit hoeherem `limit` - deshalb ist
`INSERT ... ON CONFLICT DO UPDATE` und kein Truncate im Spiel.

Wenn die PokeAPI nicht erreichbar ist, faellt das Add-on auf den kleinen
mitgelieferten Datensatz in `data/fallback_species.json` zurueck, damit das
Spiel trotzdem startet.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx

from . import db

log = logging.getLogger(__name__)

POKEAPI = "https://pokeapi.co/api/v2"
DATA_DIR = Path(__file__).parent / "data"

# Generationsgrenzen nach National-Dex-Nummer. Neue Generationen hier ergaenzen.
GENERATION_BOUNDS = [
    (1, 1, 151), (2, 152, 251), (3, 252, 386), (4, 387, 493),
    (5, 494, 649), (6, 650, 721), (7, 722, 809), (8, 810, 905),
    (9, 906, 1025),
]

STAT_KEYS = {
    "hp": "base_hp", "attack": "base_atk", "defense": "base_def",
    "special-attack": "base_spa", "special-defense": "base_spd", "speed": "base_spe",
}

# PokeAPI stat name -> key of the EV dicts (same keys as `battle.stat_hp` users).
EV_KEYS = {
    "hp": "hp", "attack": "atk", "defense": "def",
    "special-attack": "spa", "special-defense": "spd", "speed": "spe",
}


def generation_for(dex_id: int) -> int:
    for gen, lo, hi in GENERATION_BOUNDS:
        if lo <= dex_id <= hi:
            return gen
    return GENERATION_BOUNDS[-1][0]


def species_count() -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM species")
    return row["n"] if row else 0


def _upsert(record: dict) -> None:
    db.execute(
        """
        INSERT INTO species (
            id, name, name_de, name_en, type1, type2, generation, catch_rate, base_exp,
            growth_rate, base_hp, base_atk, base_def, base_spa, base_spd,
            base_spe, is_legendary, is_mythical, evolves_from, sprite_url, tm_moves,
            ev_yield
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, name_de=excluded.name_de, name_en=excluded.name_en,
            type1=excluded.type1, type2=excluded.type2,
            generation=excluded.generation, catch_rate=excluded.catch_rate,
            base_exp=excluded.base_exp, growth_rate=excluded.growth_rate,
            base_hp=excluded.base_hp, base_atk=excluded.base_atk,
            base_def=excluded.base_def, base_spa=excluded.base_spa,
            base_spd=excluded.base_spd, base_spe=excluded.base_spe,
            is_legendary=excluded.is_legendary, is_mythical=excluded.is_mythical,
            evolves_from=excluded.evolves_from, sprite_url=excluded.sprite_url,
            tm_moves=COALESCE(excluded.tm_moves, species.tm_moves),
            ev_yield=COALESCE(excluded.ev_yield, species.ev_yield)
        """,
        (
            record["id"], record["name"], record.get("name_de"), record.get("name_en"),
            record["type1"], record.get("type2"), record["generation"],
            record["catch_rate"], record["base_exp"], record["growth_rate"],
            record["base_hp"], record["base_atk"], record["base_def"],
            record["base_spa"], record["base_spd"], record["base_spe"],
            int(record.get("is_legendary", 0)), int(record.get("is_mythical", 0)),
            record.get("evolves_from"), record.get("sprite_url"),
            record.get("tm_moves"),
            _ev_yield_json(record.get("ev_yield")),
        ),
    )


def _ev_yield_json(ev_yield: dict | str | None) -> str | None:
    """Full six-key JSON for `species.ev_yield`; None (= unknown, keeps a
    stored value) when the record carries no yield. Accepts the JSON string of
    a species row as well, so rows read from the DB can be upserted again."""
    if ev_yield is None:
        return None
    if isinstance(ev_yield, str):
        ev_yield = json.loads(ev_yield)
    return json.dumps({key: max(0, int(ev_yield.get(key) or 0))
                       for key in EV_KEYS.values()})


def ev_yield_from_pokemon_doc(pokemon: dict) -> dict[str, int]:
    """Effort points a defeated member of the species gives: the `effort`
    of each entry in a `/pokemon/{id}` document's `stats` (e.g. Pikachu:
    speed 2), keyed like the EV dicts (`hp`, `atk`, `def`, `spa`, `spd`, `spe`)."""
    return {
        EV_KEYS[entry["stat"]["name"]]: int(entry.get("effort") or 0)
        for entry in pokemon.get("stats") or []
        if entry["stat"]["name"] in EV_KEYS
    }


def tm_moves_from_pokemon_doc(pokemon: dict) -> list[str]:
    """Moves a `/pokemon/{id}` document lists with learn method `machine`
    (TM/HM, any version group), as sorted move keys (`body-slam` ->
    `body_slam`, the spelling of data/moves.json)."""
    return sorted({
        entry["move"]["name"].replace("-", "_")
        for entry in pokemon.get("moves") or []
        if any(d["move_learn_method"]["name"] == "machine"
               for d in entry.get("version_group_details") or [])
    })


async def _fetch_one(client: httpx.AsyncClient, dex_id: int) -> dict | None:
    try:
        pokemon = (await client.get(f"{POKEAPI}/pokemon/{dex_id}")).json()
        species = (await client.get(f"{POKEAPI}/pokemon-species/{dex_id}")).json()
    except Exception as exc:  # noqa: BLE001 - Netzwerk ist best effort
        log.warning("PokeAPI-Abruf fuer #%s fehlgeschlagen: %s", dex_id, exc)
        return None

    types = sorted(pokemon["types"], key=lambda t: t["slot"])
    stats = {s["stat"]["name"]: s["base_stat"] for s in pokemon["stats"]}
    names = species.get("names", [])
    name_de = next((n["name"] for n in names if n["language"]["name"] == "de"), None)
    name_en = next((n["name"] for n in names if n["language"]["name"] == "en"), None)
    evolves_from = species.get("evolves_from_species") or {}
    evolves_from_id = None
    if evolves_from.get("url"):
        evolves_from_id = int(evolves_from["url"].rstrip("/").rsplit("/", 1)[-1])

    record = {
        "id": dex_id,
        "name": pokemon["name"],
        "name_de": name_de,
        "name_en": name_en,
        "type1": types[0]["type"]["name"],
        "type2": types[1]["type"]["name"] if len(types) > 1 else None,
        "generation": generation_for(dex_id),
        "catch_rate": species.get("capture_rate", 45),
        "base_exp": pokemon.get("base_experience") or 64,
        "growth_rate": (species.get("growth_rate") or {}).get("name", "medium-fast")
            .replace("-", "_"),
        "is_legendary": species.get("is_legendary", False),
        "is_mythical": species.get("is_mythical", False),
        "evolves_from": evolves_from_id,
        "sprite_url": (pokemon.get("sprites") or {}).get("front_default"),
        # Same document already carries the learnset - no extra request.
        "tm_moves": json.dumps(tm_moves_from_pokemon_doc(pokemon)),
        "ev_yield": ev_yield_from_pokemon_doc(pokemon),
    }
    for api_key, col in STAT_KEYS.items():
        record[col] = stats.get(api_key, 50)
    return record


async def reachable() -> bool:
    """Kurzer Probe-Request, damit wir nicht 1000x ins Leere laufen."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{POKEAPI}/pokemon/1")
            return response.status_code == 200
    except Exception:  # noqa: BLE001
        return False


async def seed_species(limit: int = 1025, concurrency: int = 8) -> int:
    """Laedt Spezies 1..limit. Idempotent, kann fuer neue Generationen erneut laufen."""
    if not await reachable():
        log.warning("PokeAPI nicht erreichbar - ueberspringe Online-Seeding")
        return 0

    seeded = 0
    failures = 0
    sem = asyncio.Semaphore(concurrency)
    aborted = asyncio.Event()

    async with httpx.AsyncClient(timeout=20) as client:
        async def worker(dex_id: int) -> None:
            nonlocal seeded, failures
            if aborted.is_set():
                return
            async with sem:
                record = await _fetch_one(client, dex_id)
            if record:
                _upsert(record)
                seeded += 1
            else:
                failures += 1
                # Reissleine, falls die API mitten im Lauf wegbricht.
                if failures > 25 and seeded == 0:
                    aborted.set()

        await asyncio.gather(*(worker(i) for i in range(1, limit + 1)))

    log.info("Pokédex geseedet: %s Eintraege (%s Fehler)", seeded, failures)
    return seeded


def seed_from_fallback() -> int:
    path = DATA_DIR / "fallback_species.json"
    if not path.exists():
        return 0
    records = json.loads(path.read_text(encoding="utf-8"))
    for record in records:
        record.setdefault("generation", generation_for(record["id"]))
        _upsert(record)
    log.info("Fallback-Pokédex geladen: %s Eintraege", len(records))
    return len(records)


async def ensure_seeded(limit: int = 1025) -> None:
    if species_count() > 0:
        return
    log.info("Pokédex ist leer - seede aus PokeAPI ...")
    count = 0
    try:
        count = await seed_species(limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("PokeAPI nicht erreichbar (%s)", exc)
    if count == 0:
        seed_from_fallback()


# ------------------------------------------------------- name_en-Nachtrag
# Fuer Produktions-DBs, die schon VOR der i18n-Umstellung vollstaendig
# geseedet waren: `ensure_seeded` oben tut dort nichts mehr (species_count()
# > 0), also braucht `name_en` einen eigenen Nachtrag - Muster wie
# `abilities._species_missing_abilities`/`ensure_seeded`.
_name_en_task: "asyncio.Task | None" = None


def _species_missing_name_en() -> list[int]:
    """Species the background backfill has to re-fetch: no English name or no
    EV yield yet (rows from before those columns existed, offline fallback)."""
    return [row["id"] for row in db.query(
        "SELECT id FROM species WHERE name_en IS NULL OR ev_yield IS NULL")]


async def backfill_name_en(concurrency: int = 8) -> int:
    missing = _species_missing_name_en()
    if not missing or not await reachable():
        return 0
    updated = 0
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=20) as client:
        async def worker(dex_id: int) -> None:
            nonlocal updated
            async with sem:
                record = await _fetch_one(client, dex_id)
            if record:
                _upsert(record)
                updated += 1

        await asyncio.gather(*(worker(dex_id) for dex_id in missing))
    log.info("name_en nachgetragen: %s von %s Spezies", updated, len(missing))
    return updated


async def ensure_name_en_backfilled(after: "asyncio.Task | None" = None) -> None:
    if after is not None:
        try:
            await after
        except Exception:  # noqa: BLE001
            pass
    try:
        await backfill_name_en()
    except Exception as exc:  # noqa: BLE001
        log.warning("name_en-Nachtrag fehlgeschlagen: %s", exc)


def start_name_en_backfill(after: "asyncio.Task | None" = None) -> None:
    global _name_en_task
    _name_en_task = asyncio.create_task(ensure_name_en_backfilled(after))
