"""Evolutions by item (stones, linking cord) and by level.

Data: `evolution` table, seeded from PokeAPI evolution chains
(`/evolution-chain/{id}` -> `evolution_details`). Seeding runs in the
background, is idempotent and independent from Pokedex seeding - so it also
works on an existing production DB whose `species` table has long been filled
(`seed.ensure_seeded` skips everything there). Whether it has already run is
recorded in `seed_meta` - an interrupted run is retried on next startup. Without
PokeAPI, falls back to the small `data/fallback_evolutions.json`.

Trade evolutions without held item (Kadabra, Machoke, ...) are mapped to the
linking cord (`linking_cord`, as in Legends: Arceus) - so it works without a
trade partner. Trades WITH held item (e.g. Onix + Metal Coat) get the held item
as `item_key`; these are (not yet) in the shop.

Level evolutions (`trigger_kind = 'level-up'` with `min_level`) do NOT happen
automatically on level up: when a Pokemon reaches the level, the Pokemon menu
(Team tab) shows an "Evolve" button, and the player decides themselves
(`evolve_by_level`, POST /api/team/{id}/evolve). This allows delaying an
evolution - like the B button in the main games. Friendship evolutions are turned into level evolutions at a fixed level
(`FRIENDSHIP_LEVEL_*`, the usual fan-game approach); other level evolutions
without `min_level` (location, moves, ...) are (not yet) supported.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from . import battle, db, god_mode, i18n
from .economy import EconomyError, catalog_by_key, now_iso

log = logging.getLogger(__name__)

POKEAPI = "https://pokeapi.co/api/v2"
DATA_DIR = Path(__file__).parent / "data"

# Increment to reseed after an update (e.g., new generation).
SEED_VERSION = "2"
SEED_META_KEY = "evolutions_pokeapi"

LINKING_CORD = "linking_cord"
ITEM_TRIGGERS = ("use-item", "trade")
# Regional forms have the same Dex number as the normal form, but evolve
# differently (e.g. Alola Vulpix + Ice Stone). We don't model forms - so these
# entries are skipped.
REGIONAL_FORM_SUFFIXES = ("-alola", "-galar", "-hisui", "-paldea")
POKEAPI_GENDER = {1: "female", 2: "male"}
# Friendship evolutions (Pichu, Golbat, Eevee -> Espeon, ...) have no level in
# the main games and the game has no friendship stat. Like most fan games
# (Radical Red, Unbound, PokeRogue), they evolve at a fixed level instead: a
# base form earlier than a Pokemon that is already an evolution itself.
FRIENDSHIP_LEVEL_BASE = 20
FRIENDSHIP_LEVEL_EVOLVED = 35

# Nincada -> Ninjask/Shedinja (Gen 3): PokeAPI's evolution chain lists Shedinja
# as a second, independent level-20 target next to Ninjask - but in the main
# games it is never a choice. Nincada always becomes Ninjask; Shedinja is a
# bonus companion that additionally appears, taken from an empty Poke Ball,
# IF the party still has a free slot at that moment (otherwise no Shedinja at
# all - it is not queued or boxed later). We don't model the "spare Poke Ball
# in the bag" requirement from the main games - the free party slot alone
# decides, since this app has no concept of "spending" a ball outside of
# catching.
NINCADA_SPECIES_ID = 290
NINJASK_SPECIES_ID = 291
SHEDINJA_SPECIES_ID = 292

_task: asyncio.Task | None = None


# ------------------------------------------------------------------- Parsing

def _id_from_url(url: str) -> int:
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def _item_key(name: str) -> str:
    return name.replace("-", "_")


def rows_from_chain(chain_doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Creates rows for `evolution` from a PokeAPI evolution chain.

    Multiple `evolution_details` (different games) with the same (from, to,
    trigger, item) are consolidated - the first one wins. If PokeAPI marks a
    method as `is_default` (the one of the newest game), only those are used:
    older methods (Magneton: level up at a magnetic field, Eevee -> Leafeon:
    level up near a moss rock) would otherwise show up next to the current one
    (Thunder Stone / Leaf Stone) as a second "level up" route.
    Friendship evolutions become level evolutions (see FRIENDSHIP_LEVEL_*).
    """
    rows: dict[tuple, dict[str, Any]] = {}

    def walk(node: dict[str, Any], has_pre_evolution: bool = False) -> None:
        from_id = _id_from_url(node["species"]["url"])
        for child in node.get("evolves_to") or []:
            to_id = _id_from_url(child["species"]["url"])
            details = child.get("evolution_details") or []
            current = [det for det in details if det.get("is_default")]
            for det in current or details:
                form = ((det.get("required_pokemon_form") or {}).get("name") or "")
                if form.endswith(REGIONAL_FORM_SUFFIXES):
                    continue
                trigger = ((det.get("trigger") or {}).get("name") or "unknown")
                item_key = ""
                detail: dict[str, Any] = {}
                if trigger == "use-item" and det.get("item"):
                    item_key = _item_key(det["item"]["name"])
                elif trigger == "trade":
                    held = det.get("held_item")
                    item_key = _item_key(held["name"]) if held else LINKING_CORD
                    if det.get("trade_species"):
                        detail["trade_species"] = det["trade_species"]["name"]
                elif det.get("held_item"):
                    detail["held_item"] = _item_key(det["held_item"]["name"])
                if det.get("gender") in POKEAPI_GENDER:
                    detail["gender"] = POKEAPI_GENDER[det["gender"]]
                if det.get("time_of_day"):
                    detail["time_of_day"] = det["time_of_day"]
                min_level = det.get("min_level")
                if trigger == "level-up" and det.get("min_happiness") and not min_level:
                    min_level = FRIENDSHIP_LEVEL_EVOLVED if has_pre_evolution else FRIENDSHIP_LEVEL_BASE
                    detail["friendship"] = True
                key = (from_id, to_id, trigger, item_key)
                if key not in rows:
                    rows[key] = {
                        "from": from_id, "to": to_id, "trigger": trigger,
                        "item_key": item_key, "min_level": min_level,
                        "detail": detail,
                    }
            walk(child, True)

    walk(chain_doc["chain"])
    return list(rows.values())


# ------------------------------------------------------------------ Seeding

def _write_rows(rows: list[dict[str, Any]], source: str, overwrite: bool) -> int:
    """Writes all rows in ONE transaction."""
    sql = """
        INSERT INTO evolution (from_species_id, to_species_id, trigger_kind, item_key,
                               min_level, detail, source)
        VALUES (?,?,?,?,?,?,?)
    """
    if overwrite:
        sql += """
        ON CONFLICT(from_species_id, to_species_id, trigger_kind, item_key) DO UPDATE SET
            min_level = excluded.min_level, detail = excluded.detail, source = excluded.source
        """
    else:
        sql = sql.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)
    params = [
        (row["from"], row["to"], row["trigger"], row.get("item_key") or "",
         row.get("min_level"), json.dumps(row.get("detail") or {}), source)
        for row in rows
    ]
    with db.cursor() as cur:
        cur.executemany(sql, params)
    return len(params)


def _drop_stale_rows(rows: list[dict[str, Any]]) -> None:
    """After a COMPLETE PokeAPI run: removes PokeAPI rows that the current data
    no longer contains (outdated evolution methods of earlier seed versions)."""
    keep = {(r["from"], r["to"], r["trigger"], r.get("item_key") or "") for r in rows}
    stale = [
        (row["from_species_id"], row["to_species_id"], row["trigger_kind"], row["item_key"])
        for row in db.query(
            "SELECT from_species_id, to_species_id, trigger_kind, item_key "
            "FROM evolution WHERE source = 'pokeapi'")
        if (row["from_species_id"], row["to_species_id"], row["trigger_kind"], row["item_key"]) not in keep
    ]
    with db.cursor() as cur:
        cur.executemany(
            "DELETE FROM evolution WHERE from_species_id = ? AND to_species_id = ? "
            "AND trigger_kind = ? AND item_key = ?", stale)


def seed_from_fallback() -> int:
    """Provided item/trade evolutions. Doesn't overwrite anything."""
    path = DATA_DIR / "fallback_evolutions.json"
    if not path.exists():
        return 0
    rows = json.loads(path.read_text(encoding="utf-8"))["evolutions"]
    return _write_rows(rows, "fallback", overwrite=False)


def pokeapi_seeded() -> bool:
    row = db.query_one("SELECT value FROM seed_meta WHERE key = ?", (SEED_META_KEY,))
    return bool(row and row["value"] == SEED_VERSION)


async def seed_from_pokeapi(concurrency: int = 6, retries: int = 2) -> int:
    """Loads all evolution chains. Only a COMPLETE run is recorded in
    `seed_meta`; the rows themselves are written even on partial success (better
    than nothing), the next startup will retry."""
    async with httpx.AsyncClient(timeout=20) as client:
        listing = (await client.get(f"{POKEAPI}/evolution-chain/?limit=10000")).json()
        urls = [entry["url"] for entry in listing.get("results", [])]
        if not urls:
            return 0

        sem = asyncio.Semaphore(concurrency)
        rows: list[dict[str, Any]] = []
        failures = 0

        async def fetch(url: str) -> None:
            nonlocal failures
            for attempt in range(retries + 1):
                try:
                    async with sem:
                        response = await client.get(url)
                    response.raise_for_status()
                    rows.extend(rows_from_chain(response.json()))
                    return
                except Exception as exc:  # noqa: BLE001 - Network is best effort
                    if attempt == retries:
                        failures += 1
                        log.debug("Evolution chain %s failed: %s", url, exc)
                    else:
                        await asyncio.sleep(1 + attempt)

        await asyncio.gather(*(fetch(url) for url in urls))

    written = _write_rows(rows, "pokeapi", overwrite=True) if rows else 0
    if failures == 0 and written:
        _drop_stale_rows(rows)
        db.execute(
            "INSERT INTO seed_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SEED_META_KEY, SEED_VERSION),
        )
    log.info(
        "Evolution chains seeded: %s entries from %s chains (%s errors)",
        written, len(urls), failures,
    )
    return written


async def ensure_seeded() -> None:
    """Background seeding on startup. Never raises."""
    try:
        seed_from_fallback()
        if pokeapi_seeded():
            return
        from .seed import reachable  # local import: seed.py stays unchanged

        if not await reachable():
            log.warning("PokeAPI nicht erreichbar - nutze Fallback-Entwicklungen")
            return
        await seed_from_pokeapi()
    except Exception as exc:  # noqa: BLE001
        log.warning("Seeding der Entwicklungen fehlgeschlagen: %s", exc)


def start_background_seed() -> None:
    """Starts `ensure_seeded` as a task and holds a reference to it
    (otherwise the task could be garbage collected)."""
    global _task
    _task = asyncio.create_task(ensure_seeded())


# --------------------------------------------------------------- Application

def _display_name(row: dict[str, Any], prefix: str = "") -> str:
    return row.get(f"{prefix}name_en") or row.get(f"{prefix}name_de") or row.get(f"{prefix}name") or "?"


def _condition_ok(owned: dict[str, Any], detail: dict[str, Any]) -> bool:
    gender = detail.get("gender")
    return not gender or owned.get("gender") == gender


def _item_evolutions(from_species_id: int, item_key: str | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT e.*, ts.name AS to_name, ts.name_de AS to_name_de, ts.name_en AS to_name_en,
               ts.sprite_url AS to_sprite_url
        FROM evolution e JOIN species ts ON ts.id = e.to_species_id
        WHERE e.from_species_id = ? AND e.item_key != ''
          AND e.trigger_kind IN ('use-item', 'trade')
    """
    params: tuple = (from_species_id,)
    if item_key is not None:
        sql += " AND e.item_key = ?"
        params += (item_key,)
    rows = db.query(sql + " ORDER BY e.to_species_id", params)
    for row in rows:
        row["detail"] = json.loads(row["detail"] or "{}")
    return rows


def usable_items_by_owned(user_id: str) -> dict[str, list[dict[str, Any]]]:
    """Which of my own Pokemon can evolve with which item?

    Only items listed in the catalog as `evolution`. Result:
    {item_key: [{owned_id, name, level, to_species_id, to_name, ...}]}.
    """
    evo_items = {k for k, item in catalog_by_key().items() if item["category"] == "evolution"}
    rows = db.query(
        """
        SELECT o.id AS owned_id, o.level, o.nickname, o.gender, o.party_slot,
               fs.id AS from_species_id, fs.name AS from_name, fs.name_de AS from_name_de,
               fs.name_en AS from_name_en, fs.sprite_url AS from_sprite_url,
               e.item_key, e.detail, e.to_species_id,
               ts.name AS to_name, ts.name_de AS to_name_de, ts.name_en AS to_name_en,
               ts.sprite_url AS to_sprite_url
        FROM owned_pokemon o
        JOIN species fs ON fs.id = o.species_id
        JOIN evolution e ON e.from_species_id = o.species_id
             AND e.item_key != '' AND e.trigger_kind IN ('use-item', 'trade')
        JOIN species ts ON ts.id = e.to_species_id
        WHERE o.user_id = ?
        ORDER BY o.party_slot IS NULL, o.party_slot, o.id, e.to_species_id
        """,
        (user_id,),
    )
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["item_key"] not in evo_items:
            continue
        if not _condition_ok(row, json.loads(row["detail"] or "{}")):
            continue
        result.setdefault(row["item_key"], []).append({
            "owned_id": row["owned_id"],
            "name": row["nickname"] or _display_name(row, "from_"),
            "level": row["level"],
            "in_party": row["party_slot"] is not None,
            "sprite_url": row["from_sprite_url"],
            "to_species_id": row["to_species_id"],
            "to_name": _display_name(row, "to_"),
            "to_sprite_url": row["to_sprite_url"],
        })
    return result


def item_evolutions(owned: dict[str, Any], user_id: str) -> list[dict[str, Any]]:
    """Item evolutions this owned Pokemon could perform RIGHT NOW: the player
    already holds a matching evolution item in the bag. Like level
    evolutions, applying it is a deliberate step (Pokemon menu / bag), never
    automatic - this only flags that it is POSSIBLE (Team tab badge, "Evolve"
    button in the Pokemon summary, see `use_item`)."""
    catalog = catalog_by_key()
    evo_items = {k for k, item in catalog.items() if item["category"] == "evolution"}
    inventory = {
        row["item_key"]
        for row in db.query(
            "SELECT item_key FROM inventory WHERE user_id = ? AND quantity > 0", (user_id,)
        )
    }
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for row in _item_evolutions(owned["species_id"]):
        item_key = row["item_key"]
        if item_key not in evo_items or item_key not in inventory:
            continue
        if not _condition_ok(owned, row["detail"]):
            continue
        key = (item_key, row["to_species_id"])
        if key in result:
            continue
        item = catalog[item_key]
        result[key] = {
            "item_key": item_key,
            "item_label": item.get("label"),
            "item_label_de": item.get("label_de"),
            "to_species_id": row["to_species_id"],
            "to_name": _display_name(row, "to_"),
            "to_name_de": row["to_name_de"],
            "to_name_en": row["to_name_en"],
            "to_sprite_url": row["to_sprite_url"],
        }
    return list(result.values())


def line_for(species_id: int) -> dict[str, Any]:
    """Complete connected evolution line of a species (for Pokedex and Pokemon
    detail view) - all ancestors and descendants, transitively."""
    rows = db.query(
        "SELECT * FROM evolution WHERE from_species_id IN (SELECT id FROM species) "
        "AND to_species_id IN (SELECT id FROM species)"
    )
    connected = {species_id}
    changed = True
    while changed:
        changed = False
        for edge in rows:
            if edge["from_species_id"] in connected or edge["to_species_id"] in connected:
                before = len(connected)
                connected.update((edge["from_species_id"], edge["to_species_id"]))
                changed = changed or len(connected) != before
    species_rows = db.query(
        f"SELECT id, name, name_de, name_en, sprite_url FROM species WHERE id IN ({','.join('?' * len(connected))})",
        tuple(connected),
    )
    return {
        "species": sorted(species_rows, key=lambda item: item["id"]),
        "transitions": [
            {
                "from_species_id": edge["from_species_id"],
                "to_species_id": edge["to_species_id"],
                "trigger": edge["trigger_kind"],
                "item": edge["item_key"] or None,
                "min_level": edge["min_level"],
                "conditions": json.loads(edge["detail"] or "{}"),
            }
            for edge in rows
            if edge["from_species_id"] in connected and edge["to_species_id"] in connected
        ],
    }


# Time of day for level evolutions with `time_of_day` (e.g. Eevee ->
# Espeon/Umbreon day/night). Local time of the add-on; 6-18 = day.
DAY_START_HOUR = 6
NIGHT_START_HOUR = 18


def current_time_of_day(now: datetime | None = None) -> str:
    hour = (now or datetime.now().astimezone()).hour
    return "day" if DAY_START_HOUR <= hour < NIGHT_START_HOUR else "night"


def _level_condition_ok(owned: dict[str, Any], detail: dict[str, Any],
                        time_of_day: str | None = None) -> bool:
    """Like `_condition_ok`, plus conditions that occur in level evolutions:
    held item and time of day. Unknown times of day (e.g. "dusk") count as not
    met. Anything that `rows_from_chain` doesn't store (weather, move, stats,
    ...) is not checked."""
    if not _condition_ok(owned, detail):
        return False
    held = detail.get("held_item")
    if held and owned.get("held_item") != held:
        return False
    wanted = detail.get("time_of_day")
    return not wanted or wanted == (time_of_day or current_time_of_day())


def level_evolutions(owned: dict[str, Any], time_of_day: str | None = None) -> list[dict[str, Any]]:
    """Level evolutions that an own Pokemon CAN perform NOW (level reached,
    conditions met, target species in Pokedex).

    Multiple targets are possible (e.g. Mankey -> Primeape or variants, whose
    stat conditions we don't model) - then the player chooses. Empty list =
    no level evolution available.
    """
    # God mode (app/god_mode.py): every evolution of the species, whatever
    # its trigger (level, item, trade ...) and conditions.
    free = god_mode.active(owned.get("user_id"))
    rows = db.query(
        f"""
        SELECT e.to_species_id, e.min_level, e.detail,
               ts.name AS to_name, ts.name_de AS to_name_de, ts.name_en AS to_name_en,
               ts.sprite_url AS to_sprite_url
        FROM evolution e JOIN species ts ON ts.id = e.to_species_id
        WHERE e.from_species_id = ?
          {"" if free else "AND e.trigger_kind = 'level-up' AND e.min_level IS NOT NULL AND e.min_level <= ?"}
        ORDER BY e.to_species_id, e.min_level
        """,
        (owned["species_id"],) if free else (owned["species_id"], owned["level"]),
    )
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row["to_species_id"] in result:
            continue
        # Shedinja is never a target to choose - see NINCADA_SPECIES_ID above.
        if (owned["species_id"] == NINCADA_SPECIES_ID
                and row["to_species_id"] == SHEDINJA_SPECIES_ID):
            continue
        if not free and not _level_condition_ok(owned, json.loads(row["detail"] or "{}"), time_of_day):
            continue
        result[row["to_species_id"]] = {
            "to_species_id": row["to_species_id"],
            "to_name": _display_name(row, "to_"),
            "to_name_de": row["to_name_de"],
            "to_name_en": row["to_name_en"],
            "to_sprite_url": row["to_sprite_url"],
            "min_level": row["min_level"],
        }
    return list(result.values())


def stage_of(species_id: int) -> int:
    """Evolution stage of a species: 1 = base form (`evolves_from IS NULL`),
    2 = first evolution, 3 = second evolution. Follows `species.evolves_from`;
    a missing parent row ends the walk (counts what is known)."""
    stage = 1
    seen = {species_id}
    row = db.query_one("SELECT evolves_from FROM species WHERE id = ?", (species_id,))
    while row and row["evolves_from"] is not None and row["evolves_from"] not in seen:
        stage += 1
        seen.add(row["evolves_from"])
        row = db.query_one("SELECT evolves_from FROM species WHERE id = ?", (row["evolves_from"],))
    return stage


def _evolve(owned: dict[str, Any], to_species_id: int, lang: str,
            item_key: str | None = None, item_label: str = "") -> dict[str, Any]:
    """Changes the species of an own Pokemon (shared for item and level
    evolution): consumes the item if needed, recalculates stats (HP ratio
    preserved) and records the new species as caught in the Pokedex - all in
    one transaction."""
    user_id = owned["user_id"]
    owned_id = owned["id"]
    old_species = db.query_one("SELECT * FROM species WHERE id = ?", (owned["species_id"],))
    new_species = db.query_one("SELECT * FROM species WHERE id = ?", (to_species_id,))
    ivs = json.loads(owned["ivs"] or "{}")
    # EVs (see app/evs.py) belong to the Pokemon, not the species - they stay
    # untouched and count for the new stats right away.
    evs = battle._as_evs(owned.get("evs"))
    level = owned["level"]
    old_max = battle.stat_hp(old_species["base_hp"], ivs.get("hp", 15), level, evs.get("hp", 0),
                             species_id=old_species["id"])
    new_max = battle.stat_hp(new_species["base_hp"], ivs.get("hp", 15), level, evs.get("hp", 0),
                             species_id=new_species["id"])
    current_hp = owned["current_hp"]
    if current_hp is not None:
        if current_hp <= 0:
            new_hp = 0
        else:
            new_hp = max(1, min(new_max, round(current_hp / max(1, old_max) * new_max)))
    else:
        new_hp = None  # NULL = full HP, as before

    now = now_iso()
    with db.cursor() as cur:
        if item_key is not None:
            cur.execute(
                "UPDATE inventory SET quantity = quantity - 1 "
                "WHERE item_key = ? AND user_id = ? AND quantity > 0",
                (item_key, user_id),
            )
            if cur.rowcount != 1:
                raise EconomyError(409, i18n.t(lang, "error.item_not_in_bag", item=item_label))
        cur.execute(
            "UPDATE owned_pokemon SET species_id = ?, current_hp = ? "
            "WHERE id = ? AND user_id = ? AND species_id = ?",
            (new_species["id"], new_hp, owned_id, user_id, owned["species_id"]),
        )
        if cur.rowcount != 1:
            raise EconomyError(409, i18n.t(lang, "error.pokemon_changed"))
        cur.execute(
            """
            INSERT INTO dex_entry (species_id, user_id, seen_count, caught_count, shiny_caught,
                                    first_seen_at, first_caught_at)
            VALUES (?, ?, 1, 1, ?, ?, ?)
            ON CONFLICT(species_id, user_id) DO UPDATE SET
                seen_count = MAX(dex_entry.seen_count, 1),
                caught_count = caught_count + 1,
                shiny_caught = MAX(dex_entry.shiny_caught, excluded.shiny_caught),
                first_seen_at = COALESCE(dex_entry.first_seen_at, excluded.first_seen_at),
                first_caught_at = COALESCE(dex_entry.first_caught_at, excluded.first_caught_at)
            """,
            (new_species["id"], user_id, int(owned.get("shiny") or 0), now, now),
        )

    stats = {"hp": new_max}
    for key in ("atk", "def", "spa", "spd", "spe"):
        stats[key] = battle.stat_other(new_species[f"base_{key}"], ivs.get(key, 15), level,
                                       evs.get(key, 0))
    return {
        "ok": True,
        "owned_id": owned_id,
        "item_key": item_key,
        "from": {"id": old_species["id"], "name": _display_name(old_species)},
        "to": {"id": new_species["id"], "name": _display_name(new_species),
               "sprite_url": new_species.get("sprite_url")},
        "nickname": owned["nickname"],
        "current_hp": new_hp if new_hp is not None else new_max,
        "max_hp": new_max,
        "stats": stats,
    }


def _spawn_shedinja(owned: dict[str, Any]) -> dict[str, Any] | None:
    """Nincada -> Ninjask special case: adds a Shedinja to the party in the
    same step, if a slot is still free at that moment. Mirrors `_evolve`'s
    bookkeeping (Pokedex entry) for the new Pokemon, but never touches an
    item and never fails the evolution itself - no free slot simply means no
    Shedinja, same as in the main games."""
    from . import breeding  # local import: breeding does not import evolution (avoids a cycle)
    from . import spawn  # local import: spawn does not import evolution either

    user_id = owned["user_id"]
    slot = breeding.free_party_slot(user_id)
    if slot is None:
        return None
    species = db.query_one("SELECT * FROM species WHERE id = ?", (SHEDINJA_SPECIES_ID,))
    if species is None:
        return None  # Shedinja not seeded (yet) - Ninjask still evolved normally

    now = now_iso()
    level = owned["level"]
    owned_id = db.execute(
        """
        INSERT INTO owned_pokemon (
            user_id, species_id, level, exp, shiny, ivs, nature, gender, ability,
            party_slot, caught_at, hp_updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            user_id, SHEDINJA_SPECIES_ID, level,
            battle.exp_for_level(level, species.get("growth_rate", "medium_fast")),
            owned["shiny"], owned["ivs"], owned["nature"],
            None,  # Shedinja is genderless, like Ninjask's other half
            spawn.roll_ability(SHEDINJA_SPECIES_ID),
            slot, now, now,
        ),
    )
    db.execute(
        """
        INSERT INTO dex_entry (species_id, user_id, seen_count, caught_count, shiny_caught,
                                first_seen_at, first_caught_at)
        VALUES (?, ?, 1, 1, ?, ?, ?)
        ON CONFLICT(species_id, user_id) DO UPDATE SET
            seen_count = MAX(dex_entry.seen_count, 1),
            caught_count = caught_count + 1,
            shiny_caught = MAX(dex_entry.shiny_caught, excluded.shiny_caught),
            first_seen_at = COALESCE(dex_entry.first_seen_at, excluded.first_seen_at),
            first_caught_at = COALESCE(dex_entry.first_caught_at, excluded.first_caught_at)
        """,
        (SHEDINJA_SPECIES_ID, user_id, int(owned["shiny"] or 0), now, now),
    )
    ivs = json.loads(owned["ivs"] or "{}")
    max_hp = battle.stat_hp(species["base_hp"], ivs.get("hp", 15), level,
                            species_id=SHEDINJA_SPECIES_ID)
    return {
        "owned_id": owned_id,
        "species_id": SHEDINJA_SPECIES_ID,
        "name": _display_name(species),
        "sprite_url": species.get("sprite_url"),
        "level": level,
        "max_hp": max_hp,
    }


def _own_or_404(user_id: str, owned_id: int, lang: str) -> dict[str, Any]:
    owned = db.query_one(
        "SELECT * FROM owned_pokemon WHERE id = ? AND user_id = ?", (owned_id, user_id)
    )
    if owned is None:
        raise EconomyError(404, i18n.t(lang, "error.pokemon_not_found"))
    return owned


def use_item(user_id: str, owned_id: int, item_key: str) -> dict[str, Any]:
    """Applies an evolution item to an own Pokemon.

    Checks ownership, item and conditions; the species change itself (including
    item consumption) runs through `_evolve`.
    """
    lang = db.user_language(user_id)
    owned = _own_or_404(user_id, owned_id, lang)

    item = catalog_by_key().get(item_key)
    if item is None or item["category"] != "evolution":
        raise EconomyError(400, i18n.t(lang, "error.evolution_item_not_usable"))
    item_label = (item.get("label_de") if lang == "de" else None) or item["label"]

    candidates = [
        row for row in _item_evolutions(owned["species_id"], item_key)
        if _condition_ok(owned, row["detail"])
    ]
    if not candidates:
        pending = db.query_one(
            "SELECT 1 AS x FROM evolution e LEFT JOIN species s ON s.id = e.to_species_id "
            "WHERE e.from_species_id = ? AND e.item_key = ? AND s.id IS NULL",
            (owned["species_id"], item_key),
        )
        if pending:
            raise EconomyError(503, i18n.t(lang, "error.evolution_target_not_ready"))
        raise EconomyError(400, i18n.t(lang, "error.item_no_effect", item=item_label))
    return _evolve(owned, candidates[0]["to_species_id"], lang, item_key, item_label)


def evolve_by_level(user_id: str, owned_id: int, to_species_id: int | None = None) -> dict[str, Any]:
    """Level evolution, triggered by the player in the Pokemon menu.

    Without `to_species_id` only allowed if exactly ONE target is possible.
    No item consumption; conditions see `level_evolutions`.
    """
    lang = db.user_language(user_id)
    owned = _own_or_404(user_id, owned_id, lang)
    candidates = level_evolutions(owned)
    if to_species_id is not None:
        candidates = [c for c in candidates if c["to_species_id"] == to_species_id]
    if not candidates:
        raise EconomyError(400, i18n.t(lang, "error.cannot_evolve_now"))
    if len(candidates) > 1:
        raise EconomyError(400, i18n.t(lang, "error.evolution_choose_target"))
    to_species_id = candidates[0]["to_species_id"]
    result = _evolve(owned, to_species_id, lang)
    result["trigger"] = "level-up"
    if owned["species_id"] == NINCADA_SPECIES_ID and to_species_id == NINJASK_SPECIES_ID:
        result["shedinja"] = _spawn_shedinja(owned)
    return result


def evolve_many_by_level(user_id: str, owned_ids: list[int]) -> dict[str, Any]:
    """Bulk level evolution (box selection): evolves every Pokemon that has
    exactly ONE level evolution available right now - never consumes items and
    never picks between several targets. Everything else is skipped, not an
    error. Each evolution is its own transaction (independent of the others)."""
    lang = db.user_language(user_id)
    evolved: list[dict[str, Any]] = []
    skipped: list[int] = []
    for owned_id in owned_ids:
        owned = _own_or_404(user_id, owned_id, lang)
        candidates = level_evolutions(owned)
        if len(candidates) != 1:
            skipped.append(owned_id)
            continue
        to_species_id = candidates[0]["to_species_id"]
        try:
            result = _evolve(owned, to_species_id, lang)
        except EconomyError:
            skipped.append(owned_id)
            continue
        result["trigger"] = "level-up"
        if owned["species_id"] == NINCADA_SPECIES_ID and to_species_id == NINJASK_SPECIES_ID:
            result["shedinja"] = _spawn_shedinja(owned)
        evolved.append(result)
    return {"ok": True, "evolved": evolved, "skipped": skipped}
