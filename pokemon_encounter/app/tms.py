"""TMs (technical machines): teach a move that the normal learnset never offers.

A Pokemon naturally learns only its level-up moves (`moves.py`, real learnsets
from the games). A TM teaches ITS move to a COMPATIBLE Pokemon (`compatible`:
the species' real TM learnset), as soon as the Pokemon reaches the move's
`min_level` (the level gate stays, so a TM never hands a level 5 Pokemon a
110 BP attack).

TMs are single use - teaching consumes one from the bag (like generations 1-4,
which is also where the numbering in `data/tms.json` comes from). Every TM
comes from a gym leader (`rewards.py`) on first clear, and a TM awarded by
several gyms across the regions is worth having more than once. TMs with a
`price` in `data/tms.json` are buyable as well: the shop follows the Scarlet/
Violet TM list (every SV TM whose move exists in `data/moves.json`) plus the
TMs older games sold in their department stores. Many of them have no gym at
all and can only be bought; the few without a price stay gym-only rewards.

Taught moves live in `owned_pokemon.taught_moves` (JSON list of move keys, see
`db._migrate_tms`) and are simply added to the pool the player picks their four
battle moves from (`moves.learned_moves`).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException

from . import db, god_mode, healing, held_items, i18n, moves as moves_mod, seed

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"

# Bag/shop category of every TM (see `catalog_entries`).
CATEGORY = "tm"

_data: dict[str, Any] | None = None
_by_key: dict[str, dict[str, Any]] | None = None


def _load() -> dict[str, Any]:
    global _data
    if _data is None:
        _data = json.loads((DATA_DIR / "tms.json").read_text(encoding="utf-8"))
    return _data


def all_tms() -> dict[str, dict[str, Any]]:
    """TM key -> {key, number, move} (catalog order = TM number)."""
    global _by_key
    if _by_key is None:
        _by_key = {tm["key"]: tm for tm in _load()["tms"]}
    return _by_key


def get(tm_key: str | None) -> dict[str, Any] | None:
    return all_tms().get(tm_key or "")


def is_tm(item_key: str | None) -> bool:
    return get(item_key) is not None


def move_key(tm_key: str) -> str:
    return all_tms()[tm_key]["move"]


def min_level(tm_key: str) -> int:
    return int(moves_mod.get(move_key(tm_key))["min_level"])


def move_name(tm_key: str, lang: str = "en") -> str:
    move = moves_mod.get(move_key(tm_key))
    return (move.get("name_de") if lang == "de" else None) or move["name"]


def label(tm_key: str, lang: str = "en") -> str:
    """e.g. "TM33 Rock Tomb" - the number is display only, the item key is
    the move (TM numbers differ per generation, see data/tms.json)."""
    tm = all_tms()[tm_key]
    return i18n.t(lang, "item.tm_label", number=f"{tm['number']:02d}",
                  move=move_name(tm_key, lang))


def sprite_url(tm_key: str) -> str:
    """PokeAPI has one TM disc icon per type - the TM shows its move's type."""
    return held_items.sprite_url_for(f"tm_{moves_mod.get(move_key(tm_key))['type']}")


def description(tm_key: str, lang: str = "en") -> str:
    return i18n.t(lang, "item.tm_description",
                  move=move_name(tm_key, lang), level=min_level(tm_key))


def catalog_entries() -> list[dict[str, Any]]:
    """TMs in the shape of a `data/shop.json` item, so bag and battle log get
    their labels from the same place as every other item (`economy.catalog`).
    The ones with a `price` in `data/tms.json` are buyable; the rest are
    `buyable: false` (gym rewards only)."""
    return [
        {
            "key": tm["key"],
            "category": CATEGORY,
            "label": label(tm["key"]),
            "label_de": label(tm["key"], "de"),
            "price": tm.get("price"),
            "buyable": tm.get("price") is not None,
            "description": description(tm["key"]),
            "description_de": description(tm["key"], "de"),
            "sprite_url": sprite_url(tm["key"]),
            "number": tm["number"],
            "move": tm["move"],
        }
        for tm in all_tms().values()
    ]


# ------------------------------------------------------------- Gym rewards

def for_gym(trainer_id: str) -> str | None:
    """The TM this gym leader awards (None for everyone who awards none:
    Elite Four, champions, and Alola, which has trials instead of gyms)."""
    entry = _load()["gym_tms"].get(trainer_id)
    return entry["tm"] if entry else None


def canonical_for_gym(trainer_id: str) -> str | None:
    """The TM this gym leader hands out in the main games - documentation only
    (the move itself may be a substitute, see data/tms.json)."""
    entry = _load()["gym_tms"].get(trainer_id)
    return entry["canonical"] if entry else None


# --------------------------------------------------------- Taught moves

def taught_moves(owned: dict[str, Any]) -> list[str]:
    """Moves this Pokemon learned from TMs (unknown keys are ignored, so a
    move removed from data/moves.json can never break a save)."""
    try:
        keys = json.loads(owned.get("taught_moves") or "null") or []
    except ValueError:
        return []
    known = moves_mod.all_moves()
    return [key for key in keys if isinstance(key, str) and key in known]


def learnable_moves(species: dict[str, Any]) -> set[str] | None:
    """Move keys the species learns from a TM/HM in the main series
    (`species.tm_moves`, from PokeAPI). None = not fetched yet."""
    raw = species.get("tm_moves")
    if raw is None:
        return None
    try:
        keys = json.loads(raw)
    except ValueError:
        return None
    return {key for key in keys if isinstance(key, str)} if isinstance(keys, list) else None


def compatible(species: dict[str, Any], tm_key: str) -> bool:
    """Can this species learn the TM's move at all (level and known moves aside)?

    Real data, as in the games: if the move is a TM in the species' learnset
    generation (Scarlet/Violet, or the newest game the species was in - see
    `moves.tm_moves`), the species must learn it by TM there.

    For a move that is no TM in that generation, the species' PokeAPI TM
    learnset of any game counts (`species.tm_moves`, backfilled by
    `start_background_backfill`). A type-based approximation stands in until
    that data has arrived (offline start, fallback Pokedex) and for the
    substitute TMs whose move is no TM in the main series (`type_based` in
    `data/tms.json`; rules in `compatibility`): the move's type must be one of
    the species' own types or an affinity of one of them, and a damaging move
    needs `min_offense` base stat in the stat it uses. Mythical Pokemon learn
    everything in that mode."""
    move = move_key(tm_key)
    if tm_key not in _load()["type_based"]:
        entry = moves_mod.learnset(species)
        if entry is not None and moves_mod.is_machine_move(move, entry["gen"]):
            return move in entry["tm"]
        learnset = learnable_moves(species)
        if learnset is not None:
            return move in learnset
    return _compatible_by_type(species, tm_key)


def _compatible_by_type(species: dict[str, Any], tm_key: str) -> bool:
    if species.get("is_mythical"):
        return True
    rules = _load()["compatibility"]
    move = moves_mod.get(move_key(tm_key))
    types = [t for t in (species.get("type1"), species.get("type2")) if t]
    allowed = set(types) | {"normal"}
    for own_type in types:
        allowed.update(rules["type_affinity"].get(own_type, ()))
    if move["type"] not in allowed:
        return False
    if move["power"] is None:
        return True
    stat = species.get("base_atk", 0) if move["category"] == "physical" else species.get("base_spa", 0)
    return stat >= rules["min_offense"]


def teachable(owned: dict[str, Any], tm_key: str) -> bool:
    """False if the Pokemon is too low level, cannot learn the move (see
    `compatible`) or already knows it (from its own learnset or an earlier TM).

    Moves taught before the compatibility rules existed stay in `taught_moves`
    and stay usable - only new teaching is restricted."""
    move = move_key(tm_key)
    free = god_mode.active(owned.get("user_id"))  # god mode: no level / type rules
    if owned["level"] < min_level(tm_key) and not free:
        return False
    if move in taught_moves(owned):
        return False
    species = db.query_one("SELECT * FROM species WHERE id = ?", (owned["species_id"],))
    return (species is not None and (free or compatible(species, tm_key))
            and move not in moves_mod.owned_learned_moves(species, owned))


def targets_for(user_id: str, tm_key: str) -> list[dict[str, Any]]:
    """Own Pokemon this TM would actually teach something to (bag selection),
    team first - same shape as `economy.rare_candy_targets`."""
    if not is_tm(tm_key):
        return []
    rows = db.query(
        "SELECT * FROM owned_pokemon WHERE user_id = ? "
        "ORDER BY party_slot IS NULL, party_slot, level DESC, id",
        (user_id,),
    )
    targets = []
    for row in rows:
        if not teachable(row, tm_key):
            continue
        species = db.query_one("SELECT * FROM species WHERE id = ?", (row["species_id"],))
        targets.append({
            "owned_id": row["id"],
            "name": row.get("nickname") or species.get("name_en") or species["name"],
            "level": row["level"],
            "in_party": row["party_slot"] is not None,
        })
    return targets


def teach(user_id: str, owned_id: int, tm_key: str) -> dict[str, Any]:
    """Teach the TM's move to one own (compatible) Pokemon, consuming the TM.

    The move is added to `taught_moves` and, if the Pokemon still has a free
    slot, straight into its four selected moves - otherwise the player swaps it
    in via the move list (Team tab), like any other learned move."""
    lang = db.user_language(user_id)
    # Teaching rewrites the selected moves - during a battle those are locked
    # (the battle holds a snapshot of the team, see api/team.set_moves).
    if healing.has_ongoing_battle(user_id):
        raise HTTPException(status_code=409,
                            detail=i18n.t(lang, "error.team_locked_in_battle"))
    tm = get(tm_key)
    if tm is None:
        raise HTTPException(status_code=404,
                            detail=i18n.t(lang, "error.unknown_item", item=tm_key))
    owned = db.query_one(
        "SELECT * FROM owned_pokemon WHERE id = ? AND user_id = ?", (owned_id, user_id)
    )
    if owned is None:
        raise HTTPException(status_code=404, detail=i18n.t(lang, "error.pokemon_not_found"))

    move = tm["move"]
    species = db.query_one("SELECT * FROM species WHERE id = ?", (owned["species_id"],))
    name = owned.get("nickname") or species.get("name_en") or species["name"]
    free = god_mode.active(user_id)  # god mode: no level / type rules
    if owned["level"] < min_level(tm_key) and not free:
        raise HTTPException(
            status_code=400,
            detail=i18n.t(lang, "error.tm_level_too_low", name=name,
                          move=move_name(tm_key, lang), level=min_level(tm_key)),
        )
    if not compatible(species, tm_key) and not free:
        raise HTTPException(
            status_code=400,
            detail=i18n.t(lang, "error.tm_incompatible", name=name,
                          move=move_name(tm_key, lang)),
        )
    if not teachable(owned, tm_key):
        raise HTTPException(
            status_code=409,
            detail=i18n.t(lang, "error.tm_already_known", name=name,
                          move=move_name(tm_key, lang)),
        )

    taught = taught_moves(owned) + [move]
    selected = json.loads(owned.get("selected_moves") or "null") or []
    if not selected:
        # No manual selection yet: the Pokemon fights with its generated
        # moveset (battle.combatant_from_owned) - make that explicit first,
        # so the TM move can be added without silently dropping one.
        selected = moves_mod.moveset_for(species, owned["level"])
    added_to_selection = len(selected) < moves_mod.MAX_MOVES
    if added_to_selection:
        selected = selected + [move]

    with db.cursor() as cur:
        cur.execute(
            "UPDATE inventory SET quantity = quantity - 1 "
            "WHERE item_key = ? AND user_id = ? AND quantity > 0",
            (tm_key, user_id),
        )
        if cur.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail=i18n.t(lang, "error.item_not_in_bag", item=label(tm_key, lang)),
            )
        cur.execute(
            "UPDATE owned_pokemon SET taught_moves = ?, selected_moves = ? WHERE id = ?",
            (json.dumps(taught), json.dumps(selected), owned_id),
        )

    message_key = "tm.result" if added_to_selection else "tm.result_not_selected"
    return {
        "ok": True,
        "owned_id": owned_id,
        "item_key": tm_key,
        "move": move,
        "selected": added_to_selection,
        "message": i18n.t(lang, message_key, name=name, move=move_name(tm_key, lang)),
    }


# -------------------------------------------------------- PokeAPI backfill
# TM learnsets come from `/pokemon/{id}` (learn method `machine`) - same
# pattern as `breeding.backfill`. New species seeded from PokeAPI already
# carry them (`seed._fetch_one`); this fills in databases seeded before.
_task: "asyncio.Task | None" = None


def _species_missing_learnset() -> list[int]:
    return [row["id"] for row in db.query("SELECT id FROM species WHERE tm_moves IS NULL")]


async def backfill(concurrency: int = 8) -> int:
    missing = _species_missing_learnset()
    if not missing or not await seed.reachable():
        return 0
    updated = 0
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=20) as client:
        async def worker(dex_id: int) -> None:
            nonlocal updated
            try:
                async with sem:
                    response = await client.get(f"{seed.POKEAPI}/pokemon/{dex_id}")
                    response.raise_for_status()
                    doc = response.json()
            except Exception:  # noqa: BLE001 - network is best effort
                return
            if "moves" not in doc:
                return
            db.execute("UPDATE species SET tm_moves = ? WHERE id = ?",
                       (json.dumps(seed.tm_moves_from_pokemon_doc(doc)), dex_id))
            updated += 1

        await asyncio.gather(*(worker(dex_id) for dex_id in missing))
    return updated


async def _ensure_backfilled(after: "asyncio.Task | None" = None) -> None:
    if after is not None:
        try:
            await after
        except Exception:  # noqa: BLE001
            pass
    try:
        count = await backfill()
        if count:
            log.info("TM learnsets backfilled: %s species", count)
    except Exception as exc:  # noqa: BLE001
        log.warning("TM learnset backfill failed: %s", exc)


def start_background_backfill(after: "asyncio.Task | None" = None) -> None:
    global _task
    _task = asyncio.create_task(_ensure_backfilled(after))
