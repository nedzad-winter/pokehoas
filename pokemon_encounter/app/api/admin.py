"""Admin/Dev panel.

All endpoints here are behind `require_admin` (see `app/admin.py`) -
non-admins get 403, regardless of whether the tab is visible in the frontend.

Scope:
* Spawn Pokémon: into the shared queue (every player, see spawn_encounter -
  restricted to species the real spawn engine could produce) or directly into
  a player's team/box (including Pokédex entry "caught", unrestricted).
* Edit teams: level / EXP, IVs, EVs, nature, nickname, shiny, gender,
  ability, held item, moves (any move - unlearnable ones are granted as egg
  moves), species (in place), favorite / locked, HP / full heal, team slot,
  release.
* Edit accounts: reset starter choice, inventory, Pokédex (per species or in
  bulk), achievements incl. gym badges (grant with or without reward, revoke),
  wipe save data, plus editable `app_user` columns (`EDITABLE_USER_FIELDS`:
  money, every player preference and god mode - admins only, see
  app/god_mode.py).
* Raw data: every table holding per-player rows (owner column `user_id`,
  plus `app_user` and `trade`, see RAW_OWNER_COLUMNS), row by row - the
  fallback for everything without a dedicated editor.
* Species lookup (debugging): base stats, rarity tier and spawn weight,
  evolutions, abilities, biomes, IV floor - straight from the database.
* Game settings: grouped view with defaults, reset to defaults (all / per group).
  Saving values still goes through `PATCH /api/settings` (validation lives there).
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import admin as admin_auth
from .. import abilities, achievements, battle, breeding, db, economy, held_items, i18n, rarity, spawn
from .. import evs as evs_mod
from .. import moves as moves_mod

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(admin_auth.require_admin)],
)

PARTY_SIZE = 6
GENDERS = ("male", "female")
ITEM_KEY_RE = re.compile(r"^[a-z0-9_\-]{1,64}$")

# Freely editable columns of `app_user` for the account editor. New column
# (e.g. from economy) -> add here, done. Fields that don't (yet) exist in the
# DB are automatically hidden and rejected. The label is the i18n key
# `admin.field.<column>`.
#   type:    "int" | "str" | "bool" | "choice"
#   options: allowed values of a "choice" field (callable: evaluated per request)
#   reset:   value to reset the field to on "wipe save data" (preferences have
#            none - wiping save data keeps how the player likes to play).
EDITABLE_USER_FIELDS: dict[str, dict[str, Any]] = {
    "money": {"type": "int", "min": 0, "max": 99_999_999, "reset": 0},
    # Admin accounts only, see patch_user and app/god_mode.py.
    "god_mode": {"type": "bool", "reset": 0},
    "language": {"type": "choice", "options": lambda: list(i18n.available_languages())},
    "region_lock": {"type": "choice", "options": lambda: list(db.REGION_LOCK_MODES)},
    "battle_style": {"type": "choice", "options": lambda: list(db.BATTLE_STYLES)},
    "gym_difficulty": {"type": "choice", "options": lambda: list(db.GYM_DIFFICULTY_MODES)},
    "animation_speed": {"type": "choice", "options": lambda: list(db.ANIMATION_SPEEDS)},
    "auto_heal": {"type": "bool"},
    "auto_buy_potions": {"type": "bool"},
    "auto_rules_enabled": {"type": "bool"},
    "auto_rules_daily_cap": {"type": "int", "min": 0, "max": 99_999},
    "show_team_ivs": {"type": "bool"},
    "box_item_return": {"type": "bool"},
    "auto_lock_first_catch": {"type": "bool"},
    "auto_lock_shiny": {"type": "bool"},
    "auto_lock_living_dex": {"type": "bool"},
    "buddy_steps": {"type": "int", "min": 0, "max": 99_999_999},
    "buddy_steps_total": {"type": "int", "min": 0, "max": 99_999_999},
}

# Grouping of `db.DEFAULT_SETTINGS` for the admin settings view. A setting that
# isn't listed here still shows up (group "other"), so a new setting is never
# invisible - list it here to give it a proper home.
SETTING_GROUPS: dict[str, tuple[str, ...]] = {
    "spawns": (
        "retention_days", "global_cooldown_minutes", "max_spawns_per_day",
        "poll_lookback_minutes", "wild_level_min", "wild_level_max", "shiny_rate",
        "shiny_charm_multiplier", "shiny_charm_seen_factor", "shiny_charm_caught_factor",
        "living_dex_shiny_rate", "gen_caught_shiny_factor",
        "hidden_ability_chance", "maxed_ivs_spawn_weight_factor",
    ),
    "rarity": tuple(rarity.setting_key(tier) for tier in rarity.TIERS),
    "auto_battle": ("auto_battle_enabled",),
    "economy": (
        "starting_money", "money_base", "money_per_level", "money_rarity_factor",
        "money_base_exp_factor", "money_legendary_multiplier", "money_shiny_multiplier",
        "money_catch_multiplier", "money_battle_multiplier", "shop_price_multiplier",
        "trainer_money_per_level", "pvp_money_per_level",
    ),
    "battle": (
        "starter_level", "starter_inventory", "trainer_exp_multiplier",
        "iv_repeat_catch_enabled", "iv_repeat_catch_min_gain", "iv_repeat_catch_max_gain",
        "heal_regen_percent_per_hour", "heal_team_cooldown_minutes", "battle_timeout_minutes",
        "ev_max_per_stat", "ev_max_total", "ev_battle_gain_enabled",
    ),
    "activity": ("buddy_steps_per_level", "buddy_find_chance", "buddy_find_pool", "daycare_egg_minutes", "egg_passive_steps_per_hour"),
}
OTHER_SETTINGS_GROUP = "other"

# These columns are part of identity and are never changed via the editor
# or reset on deletion.
CORE_USER_COLUMNS = {"id", "name", "display_name", "created_at", "last_seen_at"}


# ------------------------------------------------------------------ Modelle

class SpawnIn(BaseModel):
    species_id: int
    level: int = Field(ge=1, le=100)
    shiny: bool = False
    nature: str | None = None
    gender: str | None = None
    ivs: dict[str, int] | None = None


class GiveIn(SpawnIn):
    nickname: str | None = Field(default=None, max_length=40)
    placement: Literal["party", "box"] = "party"


class PokemonPatch(BaseModel):
    level: int | None = Field(default=None, ge=1, le=100)
    ivs: dict[str, int] | None = None
    nature: str | None = None
    nickname: str | None = Field(default=None, max_length=40)
    shiny: bool | None = None
    gender: str | None = None
    current_hp: int | None = Field(default=None, ge=0)
    full_heal: bool = False
    party_slot: int | None = Field(default=None, ge=1, le=PARTY_SIZE)
    # EXP wins over `level` when both are sent: the level follows the EXP.
    exp: int | None = Field(default=None, ge=0)
    evs: dict[str, int] | None = None
    # "" / None = no ability / no held item.
    ability: str | None = None
    held_item: str | None = None
    favorite: bool | None = None
    locked: bool | None = None
    # Change the species in place (stats recomputed, HP share kept, Pokédex
    # entry "caught") - also covers evolving without the usual conditions.
    species_id: int | None = None
    # Battle moves, 1-4 keys of data/moves.json; [] = standard moveset again.
    # Moves the Pokémon could never learn are granted (see _grant_moves).
    moves: list[str] | None = Field(default=None, max_length=moves_mod.MAX_MOVES)


class DexEntryIn(BaseModel):
    seen_count: int = Field(ge=0, le=999_999)
    caught_count: int = Field(ge=0, le=999_999)
    shiny_caught: bool = False


class DexBulkIn(BaseModel):
    # none = remove, seen / caught / shiny (= caught incl. shiny) for every
    # species, or only those of one generation.
    state: Literal["none", "seen", "caught", "shiny"]
    generation: int | None = Field(default=None, ge=1)


class AchievementIn(BaseModel):
    unlocked: bool
    # Unlocking pays out the achievement's item reward (as when earned) if set.
    reward: bool = False


class AchievementBulkIn(AchievementIn):
    keys: list[str] = Field(min_length=1, max_length=5000)


class StarterResetIn(BaseModel):
    # True: previous team moves completely to the box.
    # False: ALL of the player's Pokémon are deleted.
    keep_pokemon: bool = True


class InventoryIn(BaseModel):
    quantity: int = Field(ge=0, le=99_999)


class SettingsResetIn(BaseModel):
    # None = every setting; otherwise only the settings of this group
    # (a key of SETTING_GROUPS or "other").
    group: str | None = None


class RawRowIn(BaseModel):
    # Column -> value; strings are converted to the column's declared type,
    # null / "" (in a nullable column) stores NULL.
    values: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------ Helpers

def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _species_or_404(species_id: int) -> dict[str, Any]:
    species = db.query_one("SELECT * FROM species WHERE id = ?", (species_id,))
    if species is None:
        raise HTTPException(status_code=404, detail=f"Species #{species_id} not in the Pokédex")
    return species


def _user_or_404(user_id: str) -> dict[str, Any]:
    user = db.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _pokemon_or_404(pokemon_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (pokemon_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Pokémon not found")
    return row


def _wild_spawnable_ids() -> set[int]:
    """Species a real trigger event could ever produce for the shared/admin
    queue (see spawn.pick_species, `user_id=None`): base forms only, generation
    unlocked for at least one registered player (union of all players' pools,
    as documented for household-wide encounters in README.md). Used to keep
    the admin "spawn to queue" button from offering species the spawn engine
    itself could never roll - direct gifts (`give_pokemon` below) are a
    separate, deliberately unrestricted tool and are not affected."""
    return {row["id"] for row in spawn._spawn_pool(None)}


def _check_nature(nature: str | None) -> None:
    if nature is not None and nature not in spawn.NATURES:
        raise HTTPException(status_code=400, detail=f"Unknown nature: {nature}")


def _gender_for(species_id: int, requested: str | None, fallback: str | None) -> str | None:
    """Genderless species never get a gender, whatever the admin asked for."""
    if species_id in spawn.GENDERLESS_SPECIES:
        return None
    return requested or fallback


def _check_gender(gender: str | None) -> None:
    if gender is not None and gender not in GENDERS:
        raise HTTPException(status_code=400, detail=f"Unknown gender: {gender}")


def _merge_ivs(base: dict[str, int], patch: dict[str, int] | None) -> dict[str, int]:
    merged = {key: int(base.get(key, 0)) for key in spawn.STAT_KEYS}
    for key, value in (patch or {}).items():
        if key not in spawn.STAT_KEYS:
            raise HTTPException(status_code=400, detail=f"Unknown IV stat: {key}")
        if not isinstance(value, int) or not 0 <= value <= 31:
            raise HTTPException(status_code=400, detail=f"IV {key} must be between 0 and 31")
        merged[key] = value
    return merged


def _max_hp(species: dict[str, Any], ivs: dict[str, int], level: int,
            evs: dict[str, int] | None = None) -> int:
    return battle.stat_hp(species["base_hp"], ivs.get("hp", 0), level,
                          (evs or {}).get("hp", 0), species_id=species.get("id"))


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(value or "null")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _level_for_exp(exp: int, growth: str) -> int:
    level = 1
    while level < 100 and battle.exp_for_level(level + 1, growth) <= exp:
        level += 1
    return level


def _check_evs(base: dict[str, int], patch: dict[str, int]) -> dict[str, int]:
    """EVs may go up to the per-stat cap (at least the games' 255); the total
    budget isn't enforced - the admin may deliberately exceed it, and evs.py
    never takes points away anyway (see evs.as_evs)."""
    per_stat = max(255, evs_mod.limits()[0])
    merged = dict(base)
    for key, value in patch.items():
        if key not in evs_mod.STAT_KEYS:
            raise HTTPException(status_code=400, detail=f"Unknown EV stat: {key}")
        if not isinstance(value, int) or not 0 <= value <= per_stat:
            raise HTTPException(status_code=400, detail=f"EV {key} must be between 0 and {per_stat}")
        merged[key] = value
    return merged


def _known_ability(key: str) -> bool:
    return key in abilities.catalog() or db.query_one(
        "SELECT 1 FROM species_ability WHERE ability_key = ?", (key,)
    ) is not None


def _grant_moves(row: dict[str, Any], species: dict[str, Any], keys: list[str]) -> list[str]:
    """`egg_moves` of `row` plus every key in `keys` the Pokémon couldn't pick
    on its own: egg moves are known from level 1 regardless of species and
    level (moves.learned_moves), so a move the admin hands out stays in the
    player's own move pool and can be re-selected later."""
    learned = set(moves_mod.owned_learned_moves(species, row))
    egg = _json_list(row.get("egg_moves"))
    return egg + [key for key in keys if key not in learned and key not in egg]


def _decorate_owned(row: dict[str, Any], species: dict[str, Any] | None) -> dict[str, Any]:
    ivs = battle._as_ivs(row["ivs"])
    evs = evs_mod.as_evs(row.get("evs"))
    row = dict(row)
    row["ivs"] = ivs
    row["evs"] = evs
    row["shiny"] = bool(row["shiny"])
    row["favorite"] = bool(row.get("favorite"))
    row["locked"] = bool(row.get("locked"))
    row["species"] = species
    # Chosen battle moves ([] = standard moveset of the level) and the pool the
    # player could pick from themselves (level-up, TM and egg moves).
    row["moves"] = _json_list(row.get("selected_moves"))
    if species:
        row["learned_moves"] = moves_mod.owned_learned_moves(species, row)
        row["max_hp"] = _max_hp(species, ivs, row["level"], evs)
        row["stats"] = {"hp": row["max_hp"]} | {
            key: battle.stat_other(species[f"base_{key}"], ivs.get(key, 0), row["level"],
                                   evs.get(key, 0))
            for key in spawn.STAT_KEYS if key != "hp"
        }
    return row


def _existing_user_columns() -> set[str]:
    return {row["name"] for row in db.query("PRAGMA table_info(app_user)")}


def _editable_fields() -> dict[str, dict[str, Any]]:
    """EDITABLE_USER_FIELDS that exist in the DB, `options` resolved (JSON-ready)."""
    columns = _existing_user_columns()
    return {
        key: {**spec, **({"options": spec["options"]()} if callable(spec.get("options")) else {})}
        for key, spec in EDITABLE_USER_FIELDS.items()
        if key in columns and key not in CORE_USER_COLUMNS
    }


def _coerce_field(key: str, spec: dict[str, Any], value: Any) -> Any:
    if value is None:
        if spec.get("nullable"):
            return None
        raise HTTPException(status_code=400, detail=f"{key} must not be empty")
    kind = spec.get("type", "str")
    try:
        if kind == "int":
            if isinstance(value, bool):
                raise ValueError
            value = int(value)
            if "min" in spec and value < spec["min"] or "max" in spec and value > spec["max"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"{key} must be between {spec.get('min')} and {spec.get('max')}",
                )
        elif kind == "bool":
            if not isinstance(value, bool):
                raise ValueError
            value = int(value)
        elif kind == "choice":
            if value not in spec.get("options", ()):
                raise HTTPException(status_code=400, detail=f"Invalid value for {key}: {value}")
        else:
            value = str(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid value for {key}") from exc
    return value


def _tables(cur) -> list[str]:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return [row["name"] for row in cur.fetchall()]


def _columns(cur, table: str) -> dict[str, dict[str, Any]]:
    cur.execute(f'PRAGMA table_info("{table}")')
    return {row["name"]: row for row in cur.fetchall()}


def _referencing_columns(cur, target: str) -> list[tuple[str, str, bool]]:
    """(table, column, NOT NULL) of all foreign keys to `target`.

    Generic rather than hardcoded, so later tables (e.g. held items,
    battle state) are automatically cleaned up on deletion.
    """
    found = []
    for table in _tables(cur):
        cur.execute(f'PRAGMA foreign_key_list("{table}")')
        fks = [fk for fk in cur.fetchall() if fk["table"] == target]
        if not fks:
            continue
        columns = _columns(cur, table)
        for fk in fks:
            column = fk["from"]
            found.append((table, column, bool(columns.get(column, {}).get("notnull"))))
    return found


def _delete_rows(cur, target: str, where: str, params: tuple) -> int:
    """Delete rows (`<target> WHERE <where>`) along with references to them:
    referencing rows are deleted (NOT NULL foreign key) or have the key set to
    NULL. References are assumed to point at the rowid (`id INTEGER PRIMARY
    KEY`), which holds for every table something points at.

    Must run within `db.cursor()` (one transaction, no further db.query
    calls - the lock is not reentrant).
    """
    subquery = f'SELECT rowid FROM "{target}" WHERE {where}'
    for table, column, notnull in _referencing_columns(cur, target):
        if table == target:
            continue
        if notnull:
            cur.execute(f'DELETE FROM "{table}" WHERE "{column}" IN ({subquery})', params)
        else:
            cur.execute(
                f'UPDATE "{table}" SET "{column}" = NULL WHERE "{column}" IN ({subquery})', params
            )
    cur.execute(f'DELETE FROM "{target}" WHERE {where}', params)
    return cur.rowcount


def _delete_owned(cur, where: str, params: tuple) -> int:
    """Delete Pokémon (`owned_pokemon WHERE <where>`) along with references."""
    return _delete_rows(cur, "owned_pokemon", where, params)


def _free_party_slot(user_id: str) -> int | None:
    # Eier im Team zaehlen mit (siehe breeding.free_party_slot).
    return breeding.free_party_slot(user_id)


def _mark_caught(user_id: str, species_id: int, shiny: bool = False) -> None:
    now = _now()
    db.execute(
        """
        INSERT INTO dex_entry (species_id, user_id, seen_count, caught_count, shiny_caught,
                                first_seen_at, first_caught_at)
        VALUES (?, ?, 1, 1, ?, ?, ?)
        ON CONFLICT(species_id, user_id) DO UPDATE SET
            seen_count = MAX(dex_entry.seen_count, 1),
            caught_count = dex_entry.caught_count + 1,
            shiny_caught = MAX(dex_entry.shiny_caught, excluded.shiny_caught),
            first_seen_at = COALESCE(dex_entry.first_seen_at, excluded.first_seen_at),
            first_caught_at = COALESCE(dex_entry.first_caught_at, excluded.first_caught_at)
        """,
        (species_id, user_id, int(shiny), now, now),
    )


# --------------------------------------------------------------- Master data

@router.get("/meta")
def meta() -> dict[str, Any]:
    starter_inventory = db.get_setting("starter_inventory", {}) or {}
    catalog = economy.catalog_by_key()
    item_keys = set(battle.BALLS) | set(starter_inventory) | set(catalog) | {
        row["item_key"] for row in db.query("SELECT DISTINCT item_key FROM inventory")
    }
    return {
        "natures": spawn.NATURES,
        "stat_keys": spawn.STAT_KEYS,
        "genders": list(GENDERS),
        "party_size": PARTY_SIZE,
        "balls": battle.BALLS,
        "item_keys": sorted(item_keys),
        # Label + category per known item (inventory grouping in the editor).
        "items": {
            key: {
                "label": item.get("label"), "label_de": item.get("label_de"),
                "category": item.get("category") or "other",
            }
            for key, item in catalog.items()
        },
        "editable_user_fields": _editable_fields(),
        # Pickers of the Pokémon editor (labels: name / name_de like the catalogs).
        "moves": [
            {key: move.get(key) for key in ("key", "name", "name_de", "type", "power", "category")}
            for move in sorted(moves_mod.all_moves().values(), key=lambda m: m["name"])
        ],
        "abilities": [
            {"key": key, "name": a.get("name"), "name_de": a.get("name_de")}
            for key, a in sorted(abilities.catalog().items(), key=lambda kv: kv[1].get("name") or kv[0])
        ],
        "held_items": [
            {"key": key, "name": h.get("name"), "name_de": h.get("name_de")}
            for key, h in sorted(held_items.catalog().items(), key=lambda kv: kv[1].get("name") or kv[0])
        ],
        "ev_max_per_stat": max(255, evs_mod.limits()[0]),
        "generations": achievements.generations(),
    }


@router.get("/species")
def search_species(q: str = "", limit: int = 25) -> list[dict[str, Any]]:
    """Search by Dex number, English or German name."""
    limit = max(1, min(limit, 100))
    q = q.strip().lstrip("#")
    columns = "id, name, name_de, name_en, type1, type2, generation, growth_rate, sprite_url"
    if not q:
        return db.query(f"SELECT {columns} FROM species ORDER BY id LIMIT ?", (limit,))
    if q.isdigit():
        return db.query(f"SELECT {columns} FROM species WHERE id = ?", (int(q),))

    needle = q.casefold()
    hits = []
    for row in db.query(f"SELECT {columns} FROM species ORDER BY id"):
        names = [n.casefold() for n in (row["name"], row["name_de"], row["name_en"]) if n]
        if any(needle in n for n in names):
            # Exact matches first, then prefix, then substring.
            rank = 0 if needle in names else 1 if any(n.startswith(needle) for n in names) else 2
            hits.append((rank, row["id"], row))
    hits.sort(key=lambda hit: (hit[0], hit[1]))
    return [row for _, _, row in hits[:limit]]


@router.get("/species/random")
def random_species() -> dict[str, Any]:
    """One species for the spawn menu shortcut, rolled via the real wild spawn
    logic (`spawn.pick_species`, `user_id=None`) - same tier/rarity weights,
    base forms only, union of all players' unlocked generations a real
    trigger event would use, so whatever this returns is always a valid
    choice for POST /encounters below too. Must be declared before
    /species/{species_id} so "random" isn't swallowed as a bad id."""
    species = spawn.pick_species(user_id=None)
    if species is None:
        raise HTTPException(status_code=404, detail="No wild-spawnable species available yet")
    columns = ("id", "name", "name_de", "name_en", "type1", "type2",
               "generation", "growth_rate", "sprite_url")
    return {key: species[key] for key in columns}


@router.get("/species/{species_id}")
def species_info(species_id: int) -> dict[str, Any]:
    """Everything the database knows about one species (debugging aid).

    Shows the raw row plus the derived values the game computes from it: rarity
    tier (and why), spawn weight, IV floor, evolutions, abilities and biomes.
    The spawn share only considers the rarity weight - trigger type bonus,
    biome and league progress multiply on top at spawn time.
    """
    species = _species_or_404(species_id)
    tier = rarity.tier_for(species)
    multipliers = rarity.tier_multipliers()
    maxed_ivs_factor = spawn._maxed_ivs_spawn_factor()

    def _brief(row_id: int) -> dict[str, Any] | None:
        return db.query_one(
            "SELECT id, name, name_de, name_en, sprite_url FROM species WHERE id = ?", (row_id,)
        )

    weight = spawn._rarity_weight(species, multipliers, maxed_ivs_factor)
    pool_weight = sum(
        spawn._rarity_weight(row, multipliers, maxed_ivs_factor)
        for row in db.query("SELECT * FROM species")
    )
    tier_reason = (
        "mythical" if species.get("is_mythical")
        else "legendary" if species.get("is_legendary")
        else "ultra_beast" if species_id in rarity.ULTRA_BEAST_IDS
        else "paradox" if species_id in rarity.PARADOX_IDS
        else "pseudo_legendary" if species_id in rarity.PSEUDO_LEGENDARY_IDS
        else "starter" if species_id in rarity.STARTER_IDS
        else "base_form_low_bst" if tier == rarity.COMMON
        else "evolution_or_high_bst"
    )
    evolutions = db.query(
        "SELECT * FROM evolution WHERE from_species_id = ? OR to_species_id = ? "
        "ORDER BY from_species_id, to_species_id",
        (species_id, species_id),
    )
    return {
        "species": species,
        "base_stats": {key: species[f"base_{key}"] for key in spawn.STAT_KEYS},
        "base_stat_total": rarity.base_stat_total(species),
        "rarity": {
            "tier": tier,
            "reason": tier_reason,
            "tier_weight": multipliers.get(tier),
            "catch_factor": round(0.6 + 0.4 * float(species["catch_rate"]) / 255.0, 4),
            "ivs_maxed": economy.species_ivs_maxed(species),
            "spawn_weight": round(weight, 4),
            "spawn_share_percent": round(100 * weight / pool_weight, 4) if pool_weight else 0.0,
        },
        "min_ivs": economy.species_min_ivs(species_id),
        "evolves_from": _brief(species["evolves_from"]) if species.get("evolves_from") else None,
        "evolutions": [
            {
                "from": _brief(edge["from_species_id"]),
                "to": _brief(edge["to_species_id"]),
                "trigger": edge["trigger_kind"],
                "item": edge["item_key"] or None,
                "min_level": edge["min_level"],
                "conditions": json.loads(edge["detail"] or "{}"),
            }
            for edge in evolutions
        ],
        "abilities": [
            {
                **(abilities.describe(row["ability_key"]) or {}),
                "hidden": row["slot"] == abilities.HIDDEN_SLOT,
            }
            for row in db.query(
                "SELECT ability_key, slot FROM species_ability WHERE species_id = ? "
                "ORDER BY slot, ability_key",
                (species_id,),
            )
        ],
        "biomes": db.query(
            "SELECT biome, weight FROM species_biome WHERE species_id = ? ORDER BY biome",
            (species_id,),
        ),
        "usage": {
            "owned": db.query_one(
                "SELECT COUNT(*) AS n FROM owned_pokemon WHERE species_id = ?", (species_id,)
            )["n"],
            "caught_by": db.query_one(
                "SELECT COUNT(*) AS n FROM dex_entry WHERE species_id = ? AND caught_count > 0",
                (species_id,),
            )["n"],
        },
    }


# ---------------------------------------------------------------- Settings

def _setting_type(default: Any) -> str:
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, (int, float)):
        return "number"
    return "json"


def _settings_groups() -> list[dict[str, Any]]:
    current = db.all_settings()
    listed = {key for keys in SETTING_GROUPS.values() for key in keys}
    groups = dict(SETTING_GROUPS)
    leftover = tuple(key for key in db.DEFAULT_SETTINGS if key not in listed)
    if leftover:
        groups[OTHER_SETTINGS_GROUP] = leftover
    result = []
    for group, keys in groups.items():
        entries = []
        for key in keys:
            if key not in db.DEFAULT_SETTINGS:
                continue
            default = db.DEFAULT_SETTINGS[key]
            value = current.get(key, default)
            entries.append({
                "key": key,
                "type": _setting_type(default),
                "value": value,
                "default": default,
                "modified": value != default,
            })
        if entries:
            result.append({"key": group, "settings": entries})
    return result


@router.get("/settings")
def admin_settings() -> dict[str, Any]:
    """All game settings grouped, each with its default and a `modified` flag."""
    return {"groups": _settings_groups()}


@router.post("/settings/reset")
def reset_settings(payload: SettingsResetIn) -> dict[str, Any]:
    """Reset settings to `db.DEFAULT_SETTINGS` - all of them or one group."""
    groups = _settings_groups()
    if payload.group is None:
        selected = groups
    else:
        selected = [group for group in groups if group["key"] == payload.group]
        if not selected:
            raise HTTPException(status_code=400, detail=f"Unknown settings group: {payload.group}")
    reset = []
    for group in selected:
        for entry in group["settings"]:
            db.set_setting(entry["key"], entry["default"])
            reset.append(entry["key"])
    return {"ok": True, "reset": reset, "groups": _settings_groups()}


# ------------------------------------------------------------------ Spawn

@router.post("/encounters")
def spawn_encounter(
    payload: SpawnIn, current_user: dict = Depends(admin_auth.require_admin)
) -> dict[str, Any]:
    """Create an encounter for the whole household.

    Regular spawns land in one queue per player (see spawn.process_events), an
    admin spawn deliberately doesn't: it stays shared (`user_id` NULL), so every
    player gets their own attempt at exactly this Pokemon. The species itself,
    though, must still be one the real spawn engine could produce (base form,
    generation unlocked for at least one player, see `_wild_spawnable_ids`) -
    otherwise players could be asked to "catch" something impossible under
    their own spawn logic, like an evolution or a locked generation's species.
    Giving a Pokémon directly to a player (`give_pokemon` below) is a separate
    tool and isn't restricted this way, since it never claims to be a wild
    encounter.
    """
    species = _species_or_404(payload.species_id)
    if species["id"] not in _wild_spawnable_ids():
        raise HTTPException(
            status_code=422,
            detail=(
                "This species can't appear in the wild (it's an evolution, or its "
                "generation isn't unlocked for any player yet) - use the direct "
                "give instead."
            ),
        )
    _check_nature(payload.nature)
    _check_gender(payload.gender)

    encounter_id = spawn.create_encounter(
        species, source_entity=f"admin:{current_user['name']}", source_kind="home"
    )
    row = db.query_one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))
    ivs = _merge_ivs(json.loads(row["ivs"]), payload.ivs)
    db.execute(
        "UPDATE encounter SET level = ?, shiny = ?, ivs = ?, nature = ?, gender = ? WHERE id = ?",
        (
            payload.level, int(payload.shiny), json.dumps(ivs),
            payload.nature or row["nature"], _gender_for(species["id"], payload.gender, row["gender"]),
            encounter_id,
        ),
    )
    return {"ok": True, "encounter_id": encounter_id}


@router.post("/users/{user_id}/pokemon")
def give_pokemon(user_id: str, payload: GiveIn) -> dict[str, Any]:
    """Give a Pokémon directly to a player (team or box)."""
    _user_or_404(user_id)
    species = _species_or_404(payload.species_id)
    _check_nature(payload.nature)
    _check_gender(payload.gender)

    ivs = _merge_ivs(spawn.roll_ivs(species["id"]), payload.ivs)
    slot = _free_party_slot(user_id) if payload.placement == "party" else None
    growth = species.get("growth_rate") or "medium_fast"
    ability = spawn.roll_ability(species["id"])

    owned_id = db.execute(
        """
        INSERT INTO owned_pokemon (
            user_id, species_id, nickname, level, exp, shiny, ivs, nature, gender, ability,
            current_hp, party_slot, caught_at, caught_from, locked
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)
        """,
        (
            user_id, species["id"], (payload.nickname or "").strip() or None, payload.level,
            battle.exp_for_level(payload.level, growth), int(payload.shiny), json.dumps(ivs),
            payload.nature or random.choice(spawn.NATURES),
            _gender_for(species["id"], payload.gender, random.choice(GENDERS)), ability,
            _max_hp(species, ivs, payload.level), slot, _now(),
            int(rarity.auto_lock(species)),
        ),
    )
    _mark_caught(user_id, species["id"], shiny=bool(payload.shiny))
    return {
        "ok": True,
        "owned_id": owned_id,
        "party_slot": slot,
        # Team full -> still placed, but in the box.
        "placed_in_box_because_party_full": payload.placement == "party" and slot is None,
    }


# ------------------------------------------------------------------- Users

@router.get("/users")
async def list_users() -> list[dict[str, Any]]:
    rows = db.query(
        """
        SELECT u.*,
            (SELECT COUNT(*) FROM owned_pokemon p WHERE p.user_id = u.id) AS pokemon_count,
            (SELECT COUNT(*) FROM owned_pokemon p
                WHERE p.user_id = u.id AND p.party_slot IS NOT NULL) AS party_count,
            (SELECT COUNT(*) FROM dex_entry d
                WHERE d.user_id = u.id AND d.caught_count > 0) AS caught_species
        FROM app_user u ORDER BY u.created_at
        """
    )
    for row in rows:
        row["is_admin"] = await admin_auth.is_admin(row)
    return rows


@router.get("/users/{user_id}")
def get_user_detail(user_id: str) -> dict[str, Any]:
    user = _user_or_404(user_id)
    species_by_id = {
        row["id"]: row
        for row in db.query(
            "SELECT * FROM species WHERE id IN "
            "(SELECT species_id FROM owned_pokemon WHERE user_id = ?)",
            (user_id,),
        )
    }
    pokemon = [
        _decorate_owned(row, species_by_id.get(row["species_id"]))
        for row in db.query(
            "SELECT * FROM owned_pokemon WHERE user_id = ? "
            "ORDER BY party_slot IS NULL, party_slot, id",
            (user_id,),
        )
    ]
    inventory = db.query(
        "SELECT item_key, quantity FROM inventory WHERE user_id = ? ORDER BY item_key", (user_id,)
    )
    return {
        "user": user,
        "editable_fields": _editable_fields(),
        "pokemon": pokemon,
        "inventory": inventory,
    }


@router.patch("/users/{user_id}")
async def patch_user(user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Change editable `app_user` columns (see EDITABLE_USER_FIELDS). God mode
    can only be switched on for an admin account."""
    user = _user_or_404(user_id)
    if payload.get("god_mode") is True and not await admin_auth.is_admin(user):
        raise HTTPException(status_code=400, detail="God mode is only available to admins")
    editable = _editable_fields()
    unknown = set(payload) - set(editable)
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"Non-editable fields: {sorted(unknown)}"
        )
    if not payload:
        return {"ok": True, "user": db.get_user(user_id)}
    values = {key: _coerce_field(key, editable[key], value) for key, value in payload.items()}
    assignments = ", ".join(f'"{key}" = ?' for key in values)
    db.execute(
        f"UPDATE app_user SET {assignments} WHERE id = ?", (*values.values(), user_id)
    )
    return {"ok": True, "user": db.get_user(user_id)}


@router.post("/users/{user_id}/reset-starter")
def reset_starter(user_id: str, payload: StarterResetIn) -> dict[str, Any]:
    """Reset the starter choice - player will choose again on next open.

    The new starter lands on team slot 1, so the previous team must either
    go to the box or be deleted entirely. The starting inventory is given
    again on re-selection; the Pokédex remains.
    """
    _user_or_404(user_id)
    with db.cursor() as cur:
        if payload.keep_pokemon:
            cur.execute(
                "UPDATE owned_pokemon SET party_slot = NULL WHERE user_id = ?", (user_id,)
            )
            affected = cur.rowcount
        else:
            affected = _delete_owned(cur, "user_id = ?", (user_id,))
        cur.execute("UPDATE app_user SET starter_species_id = NULL WHERE id = ?", (user_id,))
    return {
        "ok": True,
        ("moved_to_box" if payload.keep_pokemon else "deleted_pokemon"): affected,
    }


@router.put("/users/{user_id}/inventory/{item_key}")
def set_inventory(user_id: str, item_key: str, payload: InventoryIn) -> dict[str, Any]:
    _user_or_404(user_id)
    if not ITEM_KEY_RE.match(item_key):
        raise HTTPException(
            status_code=400, detail="item_key: only a-z, 0-9, _ and - (max. 64 characters)"
        )
    if payload.quantity == 0:
        db.execute(
            "DELETE FROM inventory WHERE item_key = ? AND user_id = ?", (item_key, user_id)
        )
    else:
        db.execute(
            """
            INSERT INTO inventory (item_key, user_id, quantity) VALUES (?,?,?)
            ON CONFLICT(item_key, user_id) DO UPDATE SET quantity = excluded.quantity
            """,
            (item_key, user_id, payload.quantity),
        )
    return {"ok": True, "item_key": item_key, "quantity": payload.quantity}


# ----------------------------------------------------------------- Pokédex

@router.get("/users/{user_id}/dex")
def get_dex(user_id: str) -> dict[str, Any]:
    """Every species with this player's seen / caught / shiny state."""
    _user_or_404(user_id)
    rows = db.query(
        """
        SELECT s.id AS species_id, s.name, s.name_de, s.name_en, s.generation, s.sprite_url,
               COALESCE(d.seen_count, 0) AS seen_count,
               COALESCE(d.caught_count, 0) AS caught_count,
               COALESCE(d.shiny_caught, 0) AS shiny_caught
        FROM species s LEFT JOIN dex_entry d ON d.species_id = s.id AND d.user_id = ?
        ORDER BY s.id
        """,
        (user_id,),
    )
    return {
        "entries": rows,
        "seen": sum(1 for r in rows if r["seen_count"] > 0),
        "caught": sum(1 for r in rows if r["caught_count"] > 0),
        "shiny": sum(1 for r in rows if r["shiny_caught"]),
    }


def _write_dex(cur, user_id: str, species_id: int, seen: int, caught: int, shiny: bool) -> None:
    """Set one Pokédex entry to exactly these values (all zero = remove it).
    Caught implies seen, shiny implies caught; first-seen/caught dates are
    kept when already set and cleared when the count drops to zero."""
    caught = max(caught, 1 if shiny else 0)
    seen = max(seen, 1 if caught else 0)
    if not seen:
        cur.execute("DELETE FROM dex_entry WHERE species_id = ? AND user_id = ?", (species_id, user_id))
        return
    now = _now()
    cur.execute(
        """
        INSERT INTO dex_entry (species_id, user_id, seen_count, caught_count, shiny_caught,
                               first_seen_at, first_caught_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(species_id, user_id) DO UPDATE SET
            seen_count = excluded.seen_count,
            caught_count = excluded.caught_count,
            shiny_caught = excluded.shiny_caught,
            first_seen_at = COALESCE(dex_entry.first_seen_at, excluded.first_seen_at),
            first_caught_at = CASE WHEN excluded.caught_count > 0
                THEN COALESCE(dex_entry.first_caught_at, excluded.first_caught_at) END
        """,
        (species_id, user_id, seen, caught, int(shiny), now, now if caught else None),
    )


@router.put("/users/{user_id}/dex/{species_id}")
def set_dex_entry(user_id: str, species_id: int, payload: DexEntryIn) -> dict[str, Any]:
    _user_or_404(user_id)
    _species_or_404(species_id)
    with db.cursor() as cur:
        _write_dex(cur, user_id, species_id, payload.seen_count, payload.caught_count,
                   payload.shiny_caught)
    return {"ok": True, "entry": db.query_one(
        "SELECT * FROM dex_entry WHERE species_id = ? AND user_id = ?", (species_id, user_id)
    )}


@router.post("/users/{user_id}/dex/bulk")
def bulk_dex(user_id: str, payload: DexBulkIn) -> dict[str, Any]:
    """Put every species (or one generation) into the same Pokédex state.
    Raising keeps higher existing counts; `none` removes the entries."""
    _user_or_404(user_id)
    where, params = ("WHERE generation = ?", (payload.generation,)) if payload.generation else ("", ())
    species_ids = [row["id"] for row in db.query(f"SELECT id FROM species {where}", params)]
    current = {
        row["species_id"]: row
        for row in db.query("SELECT * FROM dex_entry WHERE user_id = ?", (user_id,))
    }
    with db.cursor() as cur:
        for species_id in species_ids:
            old = current.get(species_id) or {"seen_count": 0, "caught_count": 0}
            if payload.state == "none":
                _write_dex(cur, user_id, species_id, 0, 0, False)
            elif payload.state == "seen":
                _write_dex(cur, user_id, species_id, max(1, old["seen_count"]), 0, False)
            else:
                _write_dex(cur, user_id, species_id, max(1, old["seen_count"]),
                           max(1, old["caught_count"]), payload.state == "shiny")
    return {"ok": True, "species": len(species_ids)}


# ------------------------------------------------------------ Achievements

@router.get("/users/{user_id}/achievements")
def get_achievements(
    user_id: str, current_user: dict = Depends(admin_auth.require_admin)
) -> dict[str, Any]:
    """Every achievement (gym badges, regions, Pokédex, milestones ...) with
    this player's unlock state - labels in the ADMIN's language."""
    _user_or_404(user_id)
    done = achievements.unlocked(user_id)
    items = [
        {**d, "unlocked": d["key"] in done, "unlocked_at": done.get(d["key"])}
        for d in achievements.definitions(current_user["language"], db.user_region_lock(user_id))
    ]
    return {"achievements": items, "unlocked_count": sum(1 for it in items if it["unlocked"])}


def _set_achievements(user_id: str, keys: list[str], unlocked: bool, reward: bool) -> int:
    by_key = {d["key"]: d for d in achievements.definitions(db.user_language(user_id))}
    unknown = [key for key in keys if key not in by_key]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown achievements: {unknown[:5]}")
    changed = 0
    for key in keys:
        if unlocked:
            if reward:
                changed += achievements._unlock_with_reward(user_id, key, by_key, [])  # noqa: SLF001
            else:
                changed += achievements.unlock(user_id, key)
        else:
            with db.cursor() as cur:
                cur.execute("DELETE FROM achievement WHERE user_id = ? AND key = ?", (user_id, key))
                changed += cur.rowcount
    return changed


@router.put("/users/{user_id}/achievements/{key}")
def set_achievement(user_id: str, key: str, payload: AchievementIn) -> dict[str, Any]:
    """Grant or revoke one achievement. Revoking never takes a reward back;
    Pokédex / milestone achievements whose condition is still met come back
    the next time the player opens their achievements (achievements.overview)."""
    _user_or_404(user_id)
    return {"ok": True, "changed": _set_achievements(user_id, [key], payload.unlocked, payload.reward)}


@router.post("/users/{user_id}/achievements/bulk")
def bulk_achievements(user_id: str, payload: AchievementBulkIn) -> dict[str, Any]:
    _user_or_404(user_id)
    return {"ok": True, "changed": _set_achievements(
        user_id, list(dict.fromkeys(payload.keys)), payload.unlocked, payload.reward
    )}


# ---------------------------------------------------------- Raw data editor
# Every table that holds per-player data, edited row by row - the fallback for
# everything without a dedicated editor (eggs, daycare, daily challenges, PvP
# records, auto rules, the player's own encounter queue ...). A table belongs
# to a player through its owner column(s); new tables with a `user_id` column
# show up automatically. Values are only type-checked against the declared
# column type, so the UI warns that wrong values can break the save.

# Tables whose owner isn't (only) a `user_id` column.
RAW_OWNER_COLUMNS: dict[str, tuple[str, ...]] = {
    "app_user": ("id",),
    "trade": ("initiator_id", "partner_id"),
}
# The account row itself: editable, but never created or deleted here.
RAW_FIXED_TABLES = {"app_user"}


def _raw_tables() -> dict[str, tuple[str, ...]]:
    """Table -> owner columns, for every table that holds per-player rows."""
    with db.cursor() as cur:
        tables = _tables(cur)
        result = {}
        for table in tables:
            owners = RAW_OWNER_COLUMNS.get(table)
            if owners is None and "user_id" in _columns(cur, table):
                owners = ("user_id",)
            if owners and all(o in _columns(cur, table) for o in owners):
                result[table] = owners
    return dict(sorted(result.items()))


def _raw_table_or_404(table: str) -> tuple[str, ...]:
    owners = _raw_tables().get(table)
    if owners is None:
        raise HTTPException(status_code=404, detail=f"No per-player table {table!r}")
    return owners


def _owner_where(owners: tuple[str, ...]) -> str:
    return "(" + " OR ".join(f'"{o}" = ?' for o in owners) + ")"


def _raw_columns(table: str) -> list[dict[str, Any]]:
    return [
        {"name": c["name"], "type": (c["type"] or "").upper(), "notnull": bool(c["notnull"]),
         "pk": bool(c["pk"]), "default": c["dflt_value"]}
        for c in db.query(f'PRAGMA table_info("{table}")')
    ]


def _coerce_raw(column: dict[str, Any], value: Any) -> Any:
    """Convert an editor value to the column's type affinity."""
    if value is None or (value == "" and not column["type"].startswith("TEXT")):
        if column["notnull"]:
            raise HTTPException(status_code=400, detail=f"{column['name']} must not be empty")
        return None
    if value == "" and not column["notnull"]:
        return None
    kind = column["type"]
    try:
        if "INT" in kind:
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, float) and not value.is_integer():
                raise ValueError
            return int(str(value).strip())
        if any(t in kind for t in ("REAL", "FLOA", "DOUB")):
            return float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"{column['name']}: {value!r} is not a valid {kind or 'value'}"
        ) from exc
    return value if isinstance(value, str) else json.dumps(value)


def _raw_values(table: str, owners: tuple[str, ...], values: dict[str, Any]) -> dict[str, Any]:
    columns = {c["name"]: c for c in _raw_columns(table)}
    unknown = [key for key in values if key not in columns]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown columns: {unknown}")
    fixed = [key for key in values if key in owners]
    if fixed:
        raise HTTPException(status_code=400, detail=f"Owner columns can't be changed: {fixed}")
    return {key: _coerce_raw(columns[key], value) for key, value in values.items()}


def _raw_write(sql: str, params: tuple) -> int:
    try:
        with db.cursor() as cur:
            cur.execute(sql, params)
            return cur.lastrowid if sql.startswith("INSERT") else cur.rowcount
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=400, detail=f"Rejected by the database: {exc}") from exc


@router.get("/users/{user_id}/tables")
def raw_tables(user_id: str) -> list[dict[str, Any]]:
    """Every per-player table with this player's row count."""
    _user_or_404(user_id)
    return [
        {"table": table, "owners": list(owners), "fixed": table in RAW_FIXED_TABLES,
         "rows": db.query_one(
             f'SELECT COUNT(*) AS n FROM "{table}" WHERE {_owner_where(owners)}',
             (user_id,) * len(owners),
         )["n"]}
        for table, owners in _raw_tables().items()
    ]


@router.get("/users/{user_id}/tables/{table}")
def raw_rows(user_id: str, table: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    _user_or_404(user_id)
    owners = _raw_table_or_404(table)
    limit = max(1, min(limit, 500))
    params = (user_id,) * len(owners)
    where = _owner_where(owners)
    return {
        "table": table,
        "owners": list(owners),
        "fixed": table in RAW_FIXED_TABLES,
        "columns": _raw_columns(table),
        "total": db.query_one(f'SELECT COUNT(*) AS n FROM "{table}" WHERE {where}', params)["n"],
        "rows": db.query(
            f'SELECT rowid AS _rowid, * FROM "{table}" WHERE {where} ORDER BY rowid DESC LIMIT ? OFFSET ?',
            (*params, limit, max(0, offset)),
        ),
    }


@router.patch("/users/{user_id}/tables/{table}/{rowid}")
def raw_update(user_id: str, table: str, rowid: int, payload: RawRowIn) -> dict[str, Any]:
    _user_or_404(user_id)
    owners = _raw_table_or_404(table)
    values = _raw_values(table, owners, payload.values)
    if not values:
        return {"ok": True, "changed": 0}
    assignments = ", ".join(f'"{key}" = ?' for key in values)
    changed = _raw_write(
        f'UPDATE "{table}" SET {assignments} WHERE rowid = ? AND {_owner_where(owners)}',
        (*values.values(), rowid, *(user_id,) * len(owners)),
    )
    if not changed:
        raise HTTPException(status_code=404, detail="Row not found")
    return {"ok": True, "changed": changed}


@router.post("/users/{user_id}/tables/{table}")
def raw_insert(user_id: str, table: str, payload: RawRowIn) -> dict[str, Any]:
    """New row owned by this player (its first owner column is set to them)."""
    _user_or_404(user_id)
    owners = _raw_table_or_404(table)
    if table in RAW_FIXED_TABLES:
        raise HTTPException(status_code=400, detail=f"Rows of {table} can't be added here")
    values = {owners[0]: user_id, **_raw_values(table, owners, payload.values)}
    columns = ", ".join(f'"{key}"' for key in values)
    marks = ", ".join("?" for _ in values)
    rowid = _raw_write(f'INSERT INTO "{table}" ({columns}) VALUES ({marks})', tuple(values.values()))
    return {"ok": True, "rowid": rowid}


@router.delete("/users/{user_id}/tables/{table}/{rowid}")
def raw_delete(user_id: str, table: str, rowid: int) -> dict[str, Any]:
    """Delete one row - rows referencing it (e.g. battle sessions of an
    encounter) are cleaned up like when releasing a Pokémon."""
    _user_or_404(user_id)
    owners = _raw_table_or_404(table)
    if table in RAW_FIXED_TABLES:
        raise HTTPException(status_code=400, detail=f"Rows of {table} can't be deleted here")
    with db.cursor() as cur:
        deleted = _delete_rows(cur, table, f"rowid = ? AND {_owner_where(owners)}",
                               (rowid, *(user_id,) * len(owners)))
    if not deleted:
        raise HTTPException(status_code=404, detail="Row not found")
    return {"ok": True}


@router.delete("/users/{user_id}/data")
def wipe_user(user_id: str, delete_user: bool = False) -> dict[str, Any]:
    """Wipe save data: Pokémon, inventory, Pokédex, battle log, starter.

    All tables with a `user_id` column are affected - including those
    added later, and including the player's own encounter queue
    (`encounter.user_id`). Old household-wide encounters (`user_id` NULL) stay.
    With `delete_user=true`, the user entry also disappears (it will be
    auto-created on next app open).
    """
    _user_or_404(user_id)
    reset_fields = {
        key: spec.get("reset") for key, spec in _editable_fields().items() if "reset" in spec
    }
    deleted: dict[str, int] = {}
    with db.cursor() as cur:
        # Trades reference Pokémon ids without FK and use initiator_id/partner_id.
        cur.execute("DELETE FROM trade WHERE initiator_id = ? OR partner_id = ?", (user_id, user_id))
        deleted["trade"] = cur.rowcount
        deleted["owned_pokemon"] = _delete_owned(cur, "user_id = ?", (user_id,))
        for table in _tables(cur):
            # `encounter` erst am Ende: andere Tabellen (encounter_user,
            # battle_session, ...) zeigen per Fremdschluessel darauf.
            if table in ("owned_pokemon", "app_user", "encounter"):
                continue
            if "user_id" in _columns(cur, table):
                cur.execute(f'DELETE FROM "{table}" WHERE user_id = ?', (user_id,))
                deleted[table] = cur.rowcount
        cur.execute("DELETE FROM encounter WHERE user_id = ?", (user_id,))
        deleted["encounter"] = cur.rowcount

        if delete_user:
            for table, column, notnull in _referencing_columns(cur, "app_user"):
                if notnull:
                    cur.execute(f'DELETE FROM "{table}" WHERE "{column}" = ?', (user_id,))
                else:
                    cur.execute(
                        f'UPDATE "{table}" SET "{column}" = NULL WHERE "{column}" = ?', (user_id,)
                    )
            cur.execute("DELETE FROM app_user WHERE id = ?", (user_id,))
        else:
            assignments = ", ".join(
                ["starter_species_id = NULL"] + [f'"{key}" = ?' for key in reset_fields]
            )
            cur.execute(
                f"UPDATE app_user SET {assignments} WHERE id = ?",
                (*reset_fields.values(), user_id),
            )
    return {"ok": True, "deleted": deleted, "user_deleted": delete_user}


# ------------------------------------------------------------------ Pokémon

@router.patch("/pokemon/{pokemon_id}")
def patch_pokemon(pokemon_id: int, payload: PokemonPatch) -> dict[str, Any]:
    row = _pokemon_or_404(pokemon_id)
    old_species = _species_or_404(row["species_id"])
    fields = payload.model_fields_set
    species = old_species
    if "species_id" in fields and payload.species_id is not None:
        species = _species_or_404(payload.species_id)
    _check_nature(payload.nature if "nature" in fields else None)
    if "gender" in fields:
        _check_gender(payload.gender)

    old_ivs = battle._as_ivs(row["ivs"])
    ivs = _merge_ivs(old_ivs, payload.ivs) if "ivs" in fields else old_ivs
    old_evs = evs_mod.as_evs(row.get("evs"))
    new_evs = _check_evs(old_evs, payload.evs or {}) if "evs" in fields else old_evs
    growth = species.get("growth_rate") or "medium_fast"
    level = payload.level if "level" in fields and payload.level is not None else row["level"]

    updates: dict[str, Any] = {}
    if "exp" in fields and payload.exp is not None and payload.exp != row["exp"]:
        # EXP wins: the level follows it (and is capped at 100).
        updates["exp"] = payload.exp
        level = _level_for_exp(payload.exp, growth)
    elif level != row["level"] or species is not old_species:
        # EXP matching the new level (start of level), so the next
        # level-up again runs normally via apply_exp.
        updates["exp"] = battle.exp_for_level(level, growth)
    if level != row["level"]:
        updates["level"] = level
    if species is not old_species:
        updates["species_id"] = species["id"]
        if species["id"] in spawn.GENDERLESS_SPECIES:
            updates["gender"] = None
    if ivs != old_ivs:
        updates["ivs"] = json.dumps(ivs)
    if new_evs != old_evs:
        updates["evs"] = json.dumps(new_evs)
    if "nature" in fields and payload.nature:
        updates["nature"] = payload.nature
    if "nickname" in fields:
        updates["nickname"] = (payload.nickname or "").strip() or None
    if "shiny" in fields and payload.shiny is not None:
        updates["shiny"] = int(payload.shiny)
    if "gender" in fields:
        updates["gender"] = _gender_for(species["id"], payload.gender, None)
    if "favorite" in fields and payload.favorite is not None:
        updates["favorite"] = int(payload.favorite)
    if "locked" in fields and payload.locked is not None:
        updates["locked"] = int(payload.locked)
    if "ability" in fields:
        ability = (payload.ability or "").strip() or None
        if ability is not None and not _known_ability(ability):
            raise HTTPException(status_code=400, detail=f"Unknown ability: {ability}")
        updates["ability"] = ability
    if "held_item" in fields:
        held = (payload.held_item or "").strip() or None
        if held is not None and held_items.get(held) is None:
            raise HTTPException(status_code=400, detail=f"Unknown held item: {held}")
        updates["held_item"] = held
    if "moves" in fields and payload.moves is not None:
        keys = payload.moves
        unknown = [key for key in keys if key not in moves_mod.all_moves()]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown moves: {unknown}")
        if len(set(keys)) != len(keys):
            raise HTTPException(status_code=400, detail="Moves must not repeat")
        updates["selected_moves"] = json.dumps(keys) if keys else None
        # Learn pool as it will be AFTER this patch (species / level may change too).
        egg = _grant_moves({**row, "species_id": species["id"], "level": level}, species, keys)
        if egg != _json_list(row.get("egg_moves")):
            updates["egg_moves"] = json.dumps(egg)

    # HP: damage taken remains the same on level/IV/EV change; a species
    # change keeps the HP share, like evolution._evolve.
    old_max = _max_hp(old_species, old_ivs, row["level"], old_evs)
    new_max = _max_hp(species, ivs, level, new_evs)
    current = row["current_hp"]
    if payload.full_heal:
        current = new_max
    elif "current_hp" in fields and payload.current_hp is not None:
        current = payload.current_hp
    elif current is not None and species is not old_species:
        current = 0 if current <= 0 else max(1, round(current / max(1, old_max) * new_max))
    elif current is not None and new_max != old_max:
        current = current + (new_max - old_max)
    if current is not None:
        current = max(0, min(new_max, current))
    if current != row["current_hp"]:
        updates["current_hp"] = current

    with db.cursor() as cur:
        if "party_slot" in fields and payload.party_slot != row["party_slot"]:
            new_slot = payload.party_slot
            if new_slot is not None:
                # Occupied slot -> swap: the other Pokémon gets the
                # previous slot (or moves to the box).
                cur.execute(
                    "UPDATE owned_pokemon SET party_slot = ? "
                    "WHERE user_id = ? AND party_slot = ? AND id != ?",
                    (row["party_slot"], row["user_id"], new_slot, pokemon_id),
                )
            updates["party_slot"] = new_slot
        if updates:
            assignments = ", ".join(f"{key} = ?" for key in updates)
            cur.execute(
                f"UPDATE owned_pokemon SET {assignments} WHERE id = ?",
                (*updates.values(), pokemon_id),
            )
    if species is not old_species:
        _mark_caught(row["user_id"], species["id"], shiny=bool(updates.get("shiny", row["shiny"])))

    fresh = _pokemon_or_404(pokemon_id)
    return {"ok": True, "pokemon": _decorate_owned(fresh, species)}


@router.delete("/pokemon/{pokemon_id}")
def release_pokemon(pokemon_id: int) -> dict[str, Any]:
    _pokemon_or_404(pokemon_id)
    with db.cursor() as cur:
        _delete_owned(cur, "id = ?", (pokemon_id,))
    return {"ok": True}
