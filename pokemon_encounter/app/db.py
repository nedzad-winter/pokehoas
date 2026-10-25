"""SQLite data layer.

Deliberately without ORM: a single file in the add-on data directory, which is
automatically backed up by Supervisor backup. No external DB, no additional
configuration for users.

Multi-User: A household can have multiple Home Assistant users. The spawn queue
(`encounter`) is PER USER - a trigger creates one encounter per player, each
rolled from that player's own unlocked regions (`encounter.user_id`, see
spawn.process_events). Old rows from before that split keep `user_id` NULL and
stay shared household-wide: every eligible player gets their own attempt, tracked
in `encounter_user` (see encounter_state.py). Everything belonging to a person
(team, inventory, Pokedex progress) is linked to `app_user.id` (the Home Assistant
user ID from the Ingress header, see `auth.py`).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

LEGACY_USER_ID = "legacy"


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================= Master Data
-- Pokedex. Seeded from PokeAPI on first startup (see seed.py) and can be
-- easily reloaded for new generations.
CREATE TABLE IF NOT EXISTS species (
    id             INTEGER PRIMARY KEY,          -- National Dex number
    name           TEXT NOT NULL,
    name_de        TEXT,
    name_en        TEXT,
    type1          TEXT NOT NULL,
    type2          TEXT,
    generation     INTEGER NOT NULL DEFAULT 1,
    catch_rate     INTEGER NOT NULL DEFAULT 45,  -- 3..255
    base_exp       INTEGER NOT NULL DEFAULT 64,
    growth_rate    TEXT NOT NULL DEFAULT 'medium_fast',
    base_hp        INTEGER NOT NULL DEFAULT 50,
    base_atk       INTEGER NOT NULL DEFAULT 50,
    base_def       INTEGER NOT NULL DEFAULT 50,
    base_spa       INTEGER NOT NULL DEFAULT 50,
    base_spd       INTEGER NOT NULL DEFAULT 50,
    base_spe       INTEGER NOT NULL DEFAULT 50,
    is_legendary   INTEGER NOT NULL DEFAULT 0,
    is_mythical    INTEGER NOT NULL DEFAULT 0,
    evolves_from   INTEGER,
    sprite_url     TEXT
);
CREATE INDEX IF NOT EXISTS idx_species_gen ON species(generation);
CREATE INDEX IF NOT EXISTS idx_species_type ON species(type1, type2);

-- Mapping species -> biome (only relevant for geolocation triggers).
CREATE TABLE IF NOT EXISTS species_biome (
    species_id INTEGER NOT NULL REFERENCES species(id) ON DELETE CASCADE,
    biome      TEXT NOT NULL,
    weight     REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (species_id, biome)
);

-- ================================================================= Users
-- One entry per Home Assistant user who has opened the web app before.
-- `id` is the HA user ID from the Ingress header - no separate password
-- management needed, HA already authenticates.
CREATE TABLE IF NOT EXISTS app_user (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    display_name        TEXT,
    starter_species_id  INTEGER REFERENCES species(id),
    language            TEXT NOT NULL DEFAULT 'en',
    created_at          TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL
);

-- ========================================================== User Config
-- Which entities are allowed to trigger a spawn, and with what type bonus.
-- Managed in the web app, not in the add-on configuration.
-- Household-wide (not per user) - the triggers are shared smart home devices.
CREATE TABLE IF NOT EXISTS trigger_entity (
    entity_id       TEXT PRIMARY KEY,
    friendly_name   TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    -- States that count as triggers, e.g., ["on"] for motion/lights.
    trigger_states  TEXT NOT NULL DEFAULT '["on"]',
    -- Probability that this event produces a spawn at all.
    spawn_chance    REAL NOT NULL DEFAULT 0.25,
    -- Type bonuses as {"electric": 4.0} -> quadruple weight for electric.
    type_weights    TEXT NOT NULL DEFAULT '{}',
    -- Own cooldown per entity in minutes (0 = global cooldown only).
    cooldown_minutes INTEGER NOT NULL DEFAULT 0,
    last_spawn_at   TEXT
);

-- Geolocation zones -> biome. Only applies when the trigger is the phone's
-- zone (not for home-internal triggers).
CREATE TABLE IF NOT EXISTS zone_biome (
    zone_entity_id TEXT PRIMARY KEY,
    biome          TEXT NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1
);

-- Global settings as key/value (JSON values). Household-wide.
CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ==================================================================== Game
-- Queue of pending encounters. PER USER: a trigger event creates one encounter
-- per registered player, each with its OWN species roll - so nobody profits
-- from another player's unlocked regions (see spawn.process_events).
CREATE TABLE IF NOT EXISTS encounter (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Whose queue is this encounter in? NULL = old, household-wide shared row
    -- (see `_migrate_encounter_owner`); every eligible player may attempt those.
    user_id         TEXT REFERENCES app_user(id),
    species_id      INTEGER NOT NULL REFERENCES species(id),
    level           INTEGER NOT NULL,
    shiny           INTEGER NOT NULL DEFAULT 0,
    ivs             TEXT NOT NULL,                -- JSON {hp,atk,def,spa,spd,spe}
    nature          TEXT NOT NULL DEFAULT 'hardy',
    gender          TEXT,
    -- Where did the spawn come from?
    source_entity   TEXT,
    source_kind     TEXT NOT NULL DEFAULT 'home', -- 'home' | 'geo' | 'event' (spawn.create_event_encounters)
    biome           TEXT,
    -- Lifecycle
    status          TEXT NOT NULL DEFAULT 'waiting', -- waiting|active|caught|fled|defeated
    current_hp      INTEGER,                      -- NULL as long as no battle has occurred
    -- Who has fought/caught the encounter? NULL while 'waiting'.
    claimed_by      TEXT REFERENCES app_user(id),
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,                -- created_at + retention
    resolved_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_encounter_status ON encounter(status, expires_at);

-- Progress of an encounter PER USER (see app/encounter_state.py).
-- For an own encounter (`encounter.user_id` set) this is just the owner's row.
-- Old shared rows (`encounter.user_id IS NULL`) keep their behaviour: every
-- eligible player gets their own attempt, starting from the original state.
-- `outcome` NULL means "still open"; only when all eligible users (db.eligible_users)
-- are done does such a shared encounter leave the queue.
CREATE TABLE IF NOT EXISTS encounter_user (
    encounter_id INTEGER NOT NULL REFERENCES encounter(id),
    user_id      TEXT NOT NULL REFERENCES app_user(id),
    outcome      TEXT,        -- NULL | caught | defeated | fled | released
    current_hp   INTEGER,     -- NULL as long as this user hasn't battled
    level        INTEGER,     -- NULL = original encounter level
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (encounter_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_encounter_user ON encounter_user(user_id, outcome);

-- Pokemon caught by the player. Belongs to exactly one user.
CREATE TABLE IF NOT EXISTS owned_pokemon (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL REFERENCES app_user(id),
    species_id    INTEGER NOT NULL REFERENCES species(id),
    nickname      TEXT,
    level         INTEGER NOT NULL DEFAULT 5,
    exp           INTEGER NOT NULL DEFAULT 0,
    shiny         INTEGER NOT NULL DEFAULT 0,
    ivs           TEXT NOT NULL,
    -- evs: JSON {hp,atk,def,spa,spd,spe}, added via `_migrate_evs`
    -- (NULL = all zero, see app/evs.py).
    nature        TEXT NOT NULL DEFAULT 'hardy',
    gender        TEXT,
    current_hp    INTEGER,
    party_slot    INTEGER,                        -- 1..6, NULL = in the box
    caught_at     TEXT NOT NULL,
    caught_from   INTEGER REFERENCES encounter(id)
    -- favorite: 0/1, added via `_migrate_favorite` (box filter, see www/index.html).
    -- locked: 0/1, added via `_migrate_locked` (box release guard, see api/team.py).
);
-- idx_owned_user / idx_owned_party: created in init_db() AFTER the
-- multi-user migration, because `user_id` only comes into existence via the
-- migration on an old DB (CREATE INDEX IF NOT EXISTS would otherwise fail
-- against the column before it exists).

-- Pokedex progress per user: seen / caught. Controls critical catch and
-- auto-battle option (only for already-caught species).
CREATE TABLE IF NOT EXISTS dex_entry (
    species_id      INTEGER NOT NULL REFERENCES species(id),
    user_id         TEXT NOT NULL REFERENCES app_user(id),
    seen_count      INTEGER NOT NULL DEFAULT 0,
    caught_count    INTEGER NOT NULL DEFAULT 0,
    shiny_caught    INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT,
    first_caught_at TEXT,
    PRIMARY KEY (species_id, user_id)
);
-- idx_dex_user: see comment at idx_owned_user above.

-- Inventory (balls, evolution items, ...). Per user - each collects and
-- consumes their own balls.
CREATE TABLE IF NOT EXISTS inventory (
    item_key  TEXT NOT NULL,
    user_id   TEXT NOT NULL REFERENCES app_user(id),
    quantity  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_key, user_id)
);

-- Battle log, among other things to track auto-battles.
CREATE TABLE IF NOT EXISTS battle_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    encounter_id INTEGER REFERENCES encounter(id) ON DELETE CASCADE,
    user_id      TEXT REFERENCES app_user(id),
    created_at   TEXT NOT NULL,
    auto         INTEGER NOT NULL DEFAULT 0,
    outcome      TEXT,
    detail       TEXT
);

-- Auto rules (see app/auto_rules.py): ordered IF/DO rules per player that
-- handle encounters without input. `conditions`/`actions` are JSON validated
-- by auto_rules.py; the first enabled rule whose conditions match wins.
CREATE TABLE IF NOT EXISTS auto_rule (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL REFERENCES app_user(id),
    position    INTEGER NOT NULL DEFAULT 0,
    name        TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    conditions  TEXT NOT NULL DEFAULT '{}',
    actions     TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auto_rule_user ON auto_rule(user_id, position);

-- What the auto rules did, per encounter (rule log in the web app). `spent`
-- feeds the per-player daily auto-buy budget.
CREATE TABLE IF NOT EXISTS auto_rule_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL REFERENCES app_user(id),
    encounter_id INTEGER,
    rule_id      INTEGER,
    rule_name    TEXT,
    species_id   INTEGER,
    level        INTEGER,
    mode         TEXT,
    outcome      TEXT NOT NULL,
    spent        INTEGER NOT NULL DEFAULT 0,
    detail       TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auto_rule_log_user ON auto_rule_log(user_id, id);

-- ============================================================== Economy
-- Balance: column `app_user.money`, added via migration (see `_migrate_economy`)
-- so existing DBs get it too.

-- Evolutions, seeded from PokeAPI evolution chains (evolution.py), with a
-- small offline fallback in data/fallback_evolutions.json. Deliberately no
-- FK to species: the fallback may reference target species that (don't yet)
-- exist in the Pokedex - the app only checks this when applying an item.
CREATE TABLE IF NOT EXISTS evolution (
    from_species_id INTEGER NOT NULL,
    to_species_id   INTEGER NOT NULL,
    trigger_kind    TEXT NOT NULL,              -- 'use-item' | 'trade' | 'level-up' | ...
    item_key        TEXT NOT NULL DEFAULT '',   -- e.g., 'fire_stone', 'linking_cord'; '' = no item
    min_level       INTEGER,
    detail          TEXT NOT NULL DEFAULT '{}', -- additional conditions as JSON (gender, ...)
    source          TEXT NOT NULL DEFAULT 'pokeapi', -- 'pokeapi' | 'fallback'
    PRIMARY KEY (from_species_id, to_species_id, trigger_kind, item_key)
);
CREATE INDEX IF NOT EXISTS idx_evolution_item ON evolution(item_key);

-- Species -> possible abilities, seeded from PokeAPI (see abilities.py:
-- `/api/v2/pokemon/{id}`, field "abilities") with a small offline fallback.
-- Slot 0/1 = regular (up to two possible), slot 2 = hidden (at most one).
-- Which ability an INDIVIDUAL Pokemon has is in `encounter.ability` /
-- `owned_pokemon.ability` (see `_migrate_abilities` below) - this table is
-- just the pool. Deliberately NO FK to species (like `evolution` above):
-- ability seeding runs independently and concurrently with Pokedex seeding
-- (both background tasks start at the same app startup) - an FK would
-- occasionally fail on a fresh DB if a species row isn't committed yet.
CREATE TABLE IF NOT EXISTS species_ability (
    species_id  INTEGER NOT NULL,
    ability_key TEXT NOT NULL,
    slot        INTEGER NOT NULL,
    PRIMARY KEY (species_id, ability_key)
);
CREATE INDEX IF NOT EXISTS idx_species_ability ON species_ability(species_id);

-- Marker for one-time background seedings (e.g., evolution chains), so a
-- failed run is repeated on the next startup. Deliberately not in `setting`
-- because that displays the settings page 1:1.
CREATE TABLE IF NOT EXISTS seed_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Ongoing and completed manual battles (see battle_session.py). A battle
-- claims its encounter (encounter.status='active', claimed_by) - other
-- users can neither battle nor catch it in the meantime. `state` is JSON
-- with everything that only exists during the battle (HP/level of wild
-- Pokemon, active Pokemon, move restrictions, battle log). HP and items of
-- your own team are instead in owned_pokemon.
CREATE TABLE IF NOT EXISTS battle_session (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    encounter_id INTEGER NOT NULL REFERENCES encounter(id) ON DELETE CASCADE,
    user_id      TEXT NOT NULL REFERENCES app_user(id),
    status       TEXT NOT NULL DEFAULT 'ongoing', -- ongoing|won|lost|caught|ran|fled|abandoned
    state        TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_battle_session_encounter ON battle_session(encounter_id, status);
CREATE INDEX IF NOT EXISTS idx_battle_session_user ON battle_session(user_id, status);

-- Gym/League battles against gym leaders, elite four, and champion (see
-- trainer_battle.py, trainers in data/trainers.json). Unlike wild battles,
-- the state of your own team is also in `state` here because it battles at
-- a scaled level - owned_pokemon is only written at battle end.
CREATE TABLE IF NOT EXISTS trainer_battle_session (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trainer_id   TEXT NOT NULL,
    user_id      TEXT NOT NULL REFERENCES app_user(id),
    status       TEXT NOT NULL DEFAULT 'ongoing', -- ongoing|won|lost|abandoned
    state        TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trainer_battle_user ON trainer_battle_session(user_id, status);

-- Battle against an AI-controlled COPY of another team (see pvp.py).
-- `opponent_user_id` is NULL if the opponent comes from a team code;
-- the opponent's team itself is a snapshot in `state` so that foreign
-- Pokemon never need to be touched during battle.
CREATE TABLE IF NOT EXISTS pvp_battle_session (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          TEXT NOT NULL REFERENCES app_user(id),
    opponent_user_id TEXT,
    opponent_name    TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'ongoing', -- ongoing|won|lost|abandoned
    state            TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pvp_battle_user ON pvp_battle_session(user_id, status);

-- Record of a user against a household member - from the perspective of
-- the challenger (`user_id`), team code battles don't count.
CREATE TABLE IF NOT EXISTS pvp_record (
    user_id          TEXT NOT NULL REFERENCES app_user(id),
    opponent_user_id TEXT NOT NULL REFERENCES app_user(id),
    wins             INTEGER NOT NULL DEFAULT 0,
    losses           INTEGER NOT NULL DEFAULT 0,
    last_at          TEXT,
    PRIMARY KEY (user_id, opponent_user_id)
);

-- Trades between two household members (see trading.py). Three steps:
-- `requested` (initiator offers a Pokemon) -> `offered` (partner answers with
-- one of theirs) -> `completed`; or one of `declined`/`rejected`/`cancelled`.
-- The Pokemon ids deliberately have no FK: releasing or admin-deleting a
-- Pokemon must not fail because of an open trade - trading.py revalidates.
CREATE TABLE IF NOT EXISTS trade (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    initiator_id         TEXT NOT NULL REFERENCES app_user(id),
    partner_id           TEXT NOT NULL REFERENCES app_user(id),
    offered_pokemon_id   INTEGER NOT NULL,   -- owned by the initiator
    requested_pokemon_id INTEGER,            -- owned by the partner, set on counter-offer
    status               TEXT NOT NULL DEFAULT 'requested',
    hidden_initiator     INTEGER NOT NULL DEFAULT 0,
    hidden_partner       INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    closed_at            TEXT
);
CREATE INDEX IF NOT EXISTS idx_trade_initiator ON trade(initiator_id, status);
CREATE INDEX IF NOT EXISTS idx_trade_partner ON trade(partner_id, status);

-- Breeding (see breeding.py): day care pair per user. `since` = start of the
-- current egg timer (pair set or last egg collected).
CREATE TABLE IF NOT EXISTS daycare (
    user_id   TEXT PRIMARY KEY REFERENCES app_user(id),
    parent_a  INTEGER NOT NULL,
    parent_b  INTEGER NOT NULL,
    since     TEXT NOT NULL
);

-- Incubators in use (item `incubator`, as in Pokemon GO). A copy from the bag
-- becomes a row here when first used and lasts `uses_left` eggs, after which
-- it is deleted.
CREATE TABLE IF NOT EXISTS incubator (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL REFERENCES app_user(id),
    uses_left  INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

-- Eggs: the complete individual is already fixed, it only becomes visible on
-- hatching (then a row in owned_pokemon). An egg only collects steps with
-- location 'party' (occupies one of the six team slots) or 'incubator'.
CREATE TABLE IF NOT EXISTS pokemon_egg (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          TEXT NOT NULL REFERENCES app_user(id),
    species_id       INTEGER NOT NULL REFERENCES species(id),
    shiny            INTEGER NOT NULL DEFAULT 0,
    ivs              TEXT NOT NULL,
    nature           TEXT NOT NULL DEFAULT 'hardy',
    gender           TEXT,
    ability          TEXT,
    steps_total      INTEGER NOT NULL,
    steps_done       INTEGER NOT NULL DEFAULT 0,
    location         TEXT NOT NULL DEFAULT 'box',   -- box | party | incubator
    incubator_id     INTEGER,   -- deliberately no FK: wipe_user deletes table by table
    parent_species_a INTEGER,
    parent_species_b INTEGER,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pokemon_egg_user ON pokemon_egg(user_id, location);

-- Unlocked achievements per user (definitions in achievements.py).
CREATE TABLE IF NOT EXISTS achievement (
    user_id      TEXT NOT NULL REFERENCES app_user(id),
    key          TEXT NOT NULL,
    unlocked_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

-- Every owned Pokemon that has ever reached level 100, insert-only and kept
-- even after the Pokemon is released or traded away - counts towards the
-- level-100 milestone achievements (achievements.LEVEL100_KEYS), which must
-- stay permanent like every other achievement. No FK to owned_pokemon on
-- purpose, for the same reason.
CREATE TABLE IF NOT EXISTS pokemon_level100 (
    user_id      TEXT NOT NULL REFERENCES app_user(id),
    owned_id     INTEGER NOT NULL,
    reached_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, owned_id)
);

-- Roaming legendaries that ran off from a player with damage (see roaming.py):
-- hp_fraction (0-1 of max HP) is what the next spawn of this species for that
-- player starts from; consumed when it spawns.
CREATE TABLE IF NOT EXISTS roamer_wound (
    user_id      TEXT NOT NULL REFERENCES app_user(id),
    species_id   INTEGER NOT NULL,
    hp_fraction  REAL NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, species_id)
);

-- Daily challenge quests (see challenge.py): three opponents per user and day,
-- easy / normal / hard. The opponent team is rolled once and stored as a
-- snapshot (level 100, tier IVs/EVs) so it stays the same all day.
CREATE TABLE IF NOT EXISTS daily_challenge (
    user_id       TEXT NOT NULL REFERENCES app_user(id),
    day           TEXT NOT NULL,                  -- local date, YYYY-MM-DD
    slot          INTEGER NOT NULL,               -- 1 easy, 2 normal, 3 hard
    opponent_kind TEXT NOT NULL,                  -- trainer | user
    opponent_ref  TEXT NOT NULL,                  -- trainer id or user id
    opponent_name TEXT NOT NULL,                  -- fallback display name
    snapshot      TEXT NOT NULL,                  -- JSON list of Pokemon (pvp snapshot format)
    status        TEXT NOT NULL DEFAULT 'open',   -- open | won
    attempts      INTEGER NOT NULL DEFAULT 0,
    completed_at  TEXT,
    PRIMARY KEY (user_id, day, slot)
);
"""


DEFAULT_SETTINGS: dict[str, Any] = {
    # Queue
    "retention_days": 7,
    "global_cooldown_minutes": 30,
    "max_spawns_per_day": 15,
    # Auto-battle: household master switch for the queue's auto-battle button
    # and the players' auto rules (app/auto_rules.py).
    "auto_battle_enabled": False,
    # Level range for wild Pokemon
    "wild_level_min": 2,
    "wild_level_max": 30,
    "shiny_rate": 1 / 4096,
    # Spawn weight per rarity tier (see app/rarity.py). A species' weight is
    # tier multiplier x catch_rate/255; type bonus from the trigger and biome
    # weights multiply on top. Without any defeated league, these six values
    # yield a distribution of 83/10/3/2.5/1/0.5% (total 100) - defeated
    # leagues then raise them tier-specifically (see rarity.REGION_FULL_CLEAR_TARGET).
    "rarity_weight_common": 83.0,
    "rarity_weight_rare": 10.0,
    "rarity_weight_pseudo_legendary": 3.0,
    "rarity_weight_paradox": 2.5,
    "rarity_weight_legendary": 1.0,
    "rarity_weight_mythical": 0.5,
    # A species whose IV floor (`species.min_ivs`) already sits at 31 on every
    # stat gives no further IV benefit from another catch - new spawns of it
    # already roll perfect (see economy.species_ivs_maxed). Its spawn weight
    # is multiplied by this factor instead of dropping to zero, so it stays
    # catchable for the Pokedex, EVs and the repeat-catch bonus of less-maxed
    # family members (see spawn._rarity_weight).
    "maxed_ivs_spawn_weight_factor": 0.2,
    # Chance that a newly rolled Pokemon gets its hidden (instead of a regular)
    # ability - see spawn.roll_ability.
    "hidden_ability_chance": 0.05,
    # Polling
    "poll_lookback_minutes": 15,
    # Starting inventory a user gets when choosing their starter.
    "starter_inventory": {"poke_ball": 20, "great_ball": 5, "ultra_ball": 1, "potion": 5},
    "starter_level": 5,
    # Economy (see app/economy.py, prices in app/data/shop.json).
    # Reward = (money_base + money_per_level * level) * rarity
    #   rarity = 1 + money_rarity_factor * (255 - catch_rate) / 252
    #              + money_base_exp_factor * base_exp / 100
    #            (x money_legendary_multiplier for legendary/mythical)
    # then x money_catch_multiplier or x money_battle_multiplier (x shiny).
    "starting_money": 1000,
    "money_base": 50,
    "money_per_level": 25,
    "money_rarity_factor": 0.5,
    "money_base_exp_factor": 0.25,
    "money_legendary_multiplier": 3.0,
    "money_shiny_multiplier": 2.0,
    "money_catch_multiplier": 1.0,
    "money_battle_multiplier": 0.75,
    "shop_price_multiplier": 1.0,
    # IV gain on repeat catch (entire evolution family): per family member,
    # ONE random value not yet maxed increases by min_gain..max_gain points
    # (capped at 31). Every catch also raises the per-species IV minimum
    # (`species.min_ivs`, starts at 5) the same way; new spawns never roll an
    # IV below it (see economy.raise_species_min_ivs / spawn.roll_ivs).
    "iv_repeat_catch_enabled": True,
    "iv_repeat_catch_min_gain": 1,
    "iv_repeat_catch_max_gain": 2,
    # Battle & healing (see healing.py / battle_session.py)
    "heal_regen_percent_per_hour": 20,   # HP regeneration as % of max HP per hour
    "heal_team_cooldown_minutes": 60,    # Cooldown for "heal team"
    "battle_timeout_minutes": 30,        # unused battle frees the encounter (0 = never)
    # Gym & League (see trainer_battle.py). Prize money = factor x highest
    # opponent level x (1 gym leader, 1.5 elite four, 2 champion).
    "trainer_exp_multiplier": 1.5,
    "trainer_money_per_level": 100,
    # Player vs player (see pvp.py): EXP like in trainer battles
    # (trainer_exp_multiplier), plus deliberately lower prize money =
    # factor x highest level of opponent's team.
    "pvp_money_per_level": 40,
    # Shiny charm (reward for "league champion"): multiplier on shiny_rate for
    # the spawns of whoever owns it (see spawn.shiny_charm_owned).
    "shiny_charm_multiplier": 2,
    # Pokedex perks (see achievements.shiny_boost): the charm's multiplier is
    # scaled by the seen factor once every Pokemon is seen, by the caught factor
    # (instead) once every one is caught; a living Pokedex gives this flat
    # shiny chance per spawn.
    "shiny_charm_seen_factor": 1.1,
    "shiny_charm_caught_factor": 2.0,
    "living_dex_shiny_rate": 0.01,
    # Per-generation Pokedex perks (see achievements.GEN_CAUGHT_PREFIX): every
    # species of a generation caught -> its spawns roll shiny x this factor
    # (multiplies with the charm).
    "gen_caught_shiny_factor": 1.5,
    # Buddy (see buddy.py): steps the buddy needs for +1 level. 6500 steps
    # ~ 5 km (~3 mi) at ~0.77 m stride; distance sensors are converted to steps.
    "buddy_steps_per_level": 6500,
    # Buddy finds (see buddy.roll_finds): chance PER STEP (not every N steps)
    # that the buddy finds an item on the ground; 1/3000 by default. The pool
    # has weighted tiers, each with the item keys it can drop (one item each).
    "buddy_find_chance": round(1 / 3000, 8),
    "buddy_find_pool": {
        "common": {"weight": 90, "items": ["poke_ball", "great_ball", "potion", "super_potion", "ultra_ball"]},
        "rare": {"weight": 10, "items": ["rare_candy", "revive", "max_revive"]},
    },
    # Breeding (see breeding.py): minutes until the day care pair has an egg
    # (same species; different species / Ditto take twice as long) and steps
    # that eggs in the team or an incubator gain per hour without a step sensor.
    "daycare_egg_minutes": 240,
    "egg_passive_steps_per_hour": 250,
    # Effort values (see app/evs.py): upper limits like in the main games -
    # 252 per stat, 510 in total. Every 4 EVs in a stat are worth +1 point of
    # that stat at level 100 (floor(EV/4) in battle.stat_hp/stat_other).
    # EVs come from items (vitamins, wings, berries) and from defeated
    # opponents (species yield from the PokeAPI, `ev_battle_gain_enabled`).
    "ev_max_per_stat": 252,
    "ev_max_total": 510,
    "ev_battle_gain_enabled": True,
}


# Previously used settings that `init_db` deletes from existing DBs.
OBSOLETE_SETTINGS = (
    # Replaced by iv_repeat_catch_min_gain / iv_repeat_catch_max_gain.
    "iv_repeat_catch_bonus",
    # Removed outright: a ball is always allowed as the first action.
    "require_battle_for_new_species",
    "require_battle_for_known_species",
    # Replaced by per-player auto rules (app/auto_rules.py); a player who had
    # auto-catch on gets an equivalent rule (`_migrate_auto_rules`).
    "auto_battle_only_known_species",
    "auto_battle_also_catches",
)


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    """Convert row tuple to dictionary using column names."""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def connect(path: Path | None = None) -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            db_path = path or settings.db_path
            db_path.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(db_path, check_same_thread=False)
            _conn.row_factory = _dict_factory
        return _conn


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    conn = connect()
    with _lock:
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Get column names for a table. conn.row_factory is our dict factory,
    so rows come back as dicts, not tuples - index by column name, not position."""
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _ensure_legacy_user(conn: sqlite3.Connection) -> None:
    """Catch-all for data from before multi-user support."""
    now = _now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO app_user (id, name, display_name, created_at, last_seen_at) "
        "VALUES (?,?,?,?,?)",
        (LEGACY_USER_ID, "legacy", "Previous Progress", now, now),
    )


def _migrate_multiuser(conn: sqlite3.Connection) -> None:
    """Upgrade a DB created before multi-user support to the new schema.

    Existing progress (team, inventory, Pokedex) is not discarded, but linked
    to a placeholder user (`legacy`), which the first real HA user can continue
    playing if it doesn't merge automatically - but the data is not lost.
    """
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    if "owned_pokemon" in tables and "user_id" not in _table_columns(conn, "owned_pokemon"):
        _ensure_legacy_user(conn)
        conn.execute("ALTER TABLE owned_pokemon ADD COLUMN user_id TEXT REFERENCES app_user(id)")
        conn.execute(
            "UPDATE owned_pokemon SET user_id = ? WHERE user_id IS NULL", (LEGACY_USER_ID,)
        )

    if "battle_log" in tables and "user_id" not in _table_columns(conn, "battle_log"):
        conn.execute("ALTER TABLE battle_log ADD COLUMN user_id TEXT REFERENCES app_user(id)")

    if "encounter" in tables and "claimed_by" not in _table_columns(conn, "encounter"):
        conn.execute("ALTER TABLE encounter ADD COLUMN claimed_by TEXT REFERENCES app_user(id)")

    if "dex_entry" in tables and "user_id" not in _table_columns(conn, "dex_entry"):
        _ensure_legacy_user(conn)
        conn.execute("ALTER TABLE dex_entry RENAME TO dex_entry_legacy")
        conn.execute(
            """
            CREATE TABLE dex_entry (
                species_id      INTEGER NOT NULL REFERENCES species(id),
                user_id         TEXT NOT NULL REFERENCES app_user(id),
                seen_count      INTEGER NOT NULL DEFAULT 0,
                caught_count    INTEGER NOT NULL DEFAULT 0,
                first_seen_at   TEXT,
                first_caught_at TEXT,
                PRIMARY KEY (species_id, user_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO dex_entry (species_id, user_id, seen_count, caught_count,
                                    first_seen_at, first_caught_at)
            SELECT species_id, ?, seen_count, caught_count, first_seen_at, first_caught_at
            FROM dex_entry_legacy
            """,
            (LEGACY_USER_ID,),
        )
        conn.execute("DROP TABLE dex_entry_legacy")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dex_user ON dex_entry(user_id)")

    if "inventory" in tables and "user_id" not in _table_columns(conn, "inventory"):
        _ensure_legacy_user(conn)
        conn.execute("ALTER TABLE inventory RENAME TO inventory_legacy")
        conn.execute(
            """
            CREATE TABLE inventory (
                item_key  TEXT NOT NULL,
                user_id   TEXT NOT NULL REFERENCES app_user(id),
                quantity  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (item_key, user_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO inventory (item_key, user_id, quantity)
            SELECT item_key, ?, quantity FROM inventory_legacy
            """,
            (LEGACY_USER_ID,),
        )
        conn.execute("DROP TABLE inventory_legacy")

    conn.commit()


def _migrate_trigger_source(conn: sqlite3.Connection) -> None:
    """Retrofit trigger source (`source`) and category (`category`).

    * `source`: 'manual' = created manually in trigger tab, 'config' =
      automatically from add-on configuration (categories + extra_entities,
      see trigger_sync.py). Existing rows all predate automatic sync and thus
      become 'manual' - the sync never touches them.
    * `category`: category key for grouping in web app (NULL for manual rows,
      determined when listing).

    Purely additive (ALTER TABLE ADD COLUMN) and idempotent - works on a
    fresh DB as well as an existing one.
    """
    columns = _table_columns(conn, "trigger_entity")
    if "source" not in columns:
        conn.execute(
            "ALTER TABLE trigger_entity ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"
        )
    if "category" not in columns:
        conn.execute("ALTER TABLE trigger_entity ADD COLUMN category TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trigger_source ON trigger_entity(source)"
    )


def _migrate_economy(conn: sqlite3.Connection) -> None:
    """Money column for `app_user`. Idempotent and non-destructive.

    Users already playing when money is introduced (starter chosen) get the
    starting money once - new players get it when choosing their starter.
    """
    if "money" not in _table_columns(conn, "app_user"):
        conn.execute("ALTER TABLE app_user ADD COLUMN money INTEGER NOT NULL DEFAULT 0")
        row = conn.execute("SELECT value FROM setting WHERE key = 'starting_money'").fetchone()
        amount = json.loads(row["value"]) if row else DEFAULT_SETTINGS["starting_money"]
        conn.execute(
            "UPDATE app_user SET money = ? WHERE starter_species_id IS NOT NULL",
            (int(amount),),
        )


def _migrate_battle(conn: sqlite3.Connection) -> None:
    """Columns for battle, healing, and held items (idempotent, additive).

    Deliberately via ALTER TABLE instead of CREATE TABLE above, so old and
    new databases take the same path.
    """
    owned_cols = _table_columns(conn, "owned_pokemon")
    if "held_item" not in owned_cols:
        conn.execute("ALTER TABLE owned_pokemon ADD COLUMN held_item TEXT")
    if "hp_updated_at" not in owned_cols:
        conn.execute("ALTER TABLE owned_pokemon ADD COLUMN hp_updated_at TEXT")
    if "selected_moves" not in owned_cols:
        conn.execute("ALTER TABLE owned_pokemon ADD COLUMN selected_moves TEXT")
    if "shiny_caught" not in _table_columns(conn, "dex_entry"):
        conn.execute("ALTER TABLE dex_entry ADD COLUMN shiny_caught INTEGER NOT NULL DEFAULT 0")
    if "last_team_heal_at" not in _table_columns(conn, "app_user"):
        conn.execute("ALTER TABLE app_user ADD COLUMN last_team_heal_at TEXT")
    # Encounters that previously got stuck on 'active' after a failed throw/lost
    # auto-battle (and thus disappeared from the queue) - free them up again,
    # as long as no ongoing battle holds them.
    conn.execute(
        "UPDATE encounter SET status = 'waiting', claimed_by = NULL "
        "WHERE status = 'active' AND id NOT IN "
        "(SELECT encounter_id FROM battle_session WHERE status = 'ongoing')"
    )
    conn.commit()


def _migrate_abilities(conn: sqlite3.Connection) -> None:
    """Ability columns for encounters and owned Pokemon (idempotent, additive).
    `species_ability` itself is a CREATE TABLE IF NOT EXISTS in the schema above,
    not here - only existing tables need ALTER TABLE.

    Existing rows get `ability = NULL` ("no ability known") - the battle engine
    treats this as a no-op everywhere, not an error. There is deliberately NO
    retroactive rollup for existing encounters/Pokemon in this migration - that
    only happens for new spawns and catches (see spawn.roll_ability).
    """
    if "ability" not in _table_columns(conn, "encounter"):
        conn.execute("ALTER TABLE encounter ADD COLUMN ability TEXT")
    if "ability" not in _table_columns(conn, "owned_pokemon"):
        conn.execute("ALTER TABLE owned_pokemon ADD COLUMN ability TEXT")
    conn.commit()


def _migrate_i18n(conn: sqlite3.Connection) -> None:
    """Language preference per user (idempotent, additive). Existing users
    get 'en' - English is the app's default language, German the first
    additional selectable language (see app/i18n.py).

    `species.name_en` belongs thematically here (also an i18n field), even
    though it's not an app_user field: existing rows get NULL initially,
    `seed.start_name_en_backfill()` fills it in the background (just like
    ability backfill in abilities.py)."""
    if "language" not in _table_columns(conn, "app_user"):
        conn.execute("ALTER TABLE app_user ADD COLUMN language TEXT NOT NULL DEFAULT 'en'")
    if "name_en" not in _table_columns(conn, "species"):
        conn.execute("ALTER TABLE species ADD COLUMN name_en TEXT")
    conn.commit()


# Previously sold, now removed items -> (replacement, quantity per unit).
# Rogueball (x3, 1500 P) -> 2 Hyperballs (each x2, 600 P): roughly equivalent in value.
RETIRED_ITEMS: dict[str, tuple[str, int]] = {
    "rogue_ball": ("ultra_ball", 2),
}


def _migrate_retired_items(conn: sqlite3.Connection) -> None:
    """Swaps removed items in inventory and `starter_inventory` for their
    replacements (idempotent: afterwards no rows with the old key exist).
    Balls can't be held, so `held_item` remains untouched."""
    for old_key, (new_key, factor) in RETIRED_ITEMS.items():
        rows = conn.execute(
            "SELECT user_id, quantity FROM inventory WHERE item_key = ?", (old_key,)
        ).fetchall()
        for row in rows:
            amount = max(0, int(row["quantity"])) * factor
            if amount:
                conn.execute(
                    "INSERT INTO inventory (item_key, user_id, quantity) VALUES (?,?,?) "
                    "ON CONFLICT(item_key, user_id) DO UPDATE SET quantity = quantity + excluded.quantity",
                    (new_key, row["user_id"], amount),
                )
        conn.execute("DELETE FROM inventory WHERE item_key = ?", (old_key,))

        setting = conn.execute(
            "SELECT value FROM setting WHERE key = 'starter_inventory'"
        ).fetchone()
        if setting:
            try:
                starter = json.loads(setting["value"])
            except ValueError:
                starter = None
            if isinstance(starter, dict) and old_key in starter:
                try:
                    amount = int(starter.pop(old_key)) * factor
                except (TypeError, ValueError):
                    amount = 0
                if amount > 0:
                    try:
                        starter[new_key] = int(starter.get(new_key, 0)) + amount
                    except (TypeError, ValueError):
                        starter[new_key] = amount
                conn.execute(
                    "UPDATE setting SET value = ? WHERE key = 'starter_inventory'",
                    (json.dumps(starter),),
                )
    conn.commit()


def _migrate_encounter_user(conn: sqlite3.Connection) -> None:
    """Progress per user (`encounter_user`, created in schema above).

    Previously, an encounter left the queue as soon as ONE person caught/defeated
    it - the outcome was in `encounter.status` + `claimed_by`. This migration
    records exactly these old outcomes as rows for the respective user, so
    completed encounters don't suddenly reappear in their queue. Idempotent
    via INSERT OR IGNORE (primary key encounter_id+user_id).
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO encounter_user
            (encounter_id, user_id, outcome, current_hp, level, updated_at)
        SELECT id, claimed_by, status, current_hp, level,
               COALESCE(resolved_at, created_at)
        FROM encounter
        WHERE claimed_by IS NOT NULL AND status IN ('caught', 'defeated', 'fled')
        """
    )
    conn.commit()


def _migrate_battle_history(conn: sqlite3.Connection) -> None:
    """Additional columns for battle history (idempotent, additive).

    So far, `battle_log` only stored outcome + log text, and only for wild
    battles. For the history view, we need per battle: the type of battle
    (`kind`: wild | auto | trainer), the opponent, money earned, whether
    caught, and EXP breakdown per team member (`exp_breakdown`, JSON). Old
    rows remain and have NULL in these columns - the display handles that.
    """
    cols = _table_columns(conn, "battle_log")
    for name, ddl in (
        ("kind", "TEXT"),
        ("opponent", "TEXT"),
        ("money_earned", "INTEGER"),
        ("caught", "INTEGER"),
        ("exp_breakdown", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE battle_log ADD COLUMN {name} {ddl}")
    conn.commit()


def _migrate_buddy(conn: sqlite3.Connection) -> None:
    """Buddy Pokemon per user (see app/buddy.py), idempotent and additive.

    `buddy_pokemon_id`: the one selected Pokemon that is mirrored to Home
    Assistant as a sensor. `buddy_sensor`: HA step/distance sensor;
    `buddy_sensor_last` last reading (converted to steps, NULL = no baseline
    yet), `buddy_steps` steps saved up towards the next level,
    `buddy_steps_total` total steps walked with the buddy.
    """
    cols = _table_columns(conn, "app_user")
    for name, ddl in (
        ("buddy_pokemon_id", "INTEGER"),
        ("buddy_sensor", "TEXT"),
        ("buddy_sensor_last", "REAL"),
        ("buddy_steps", "INTEGER NOT NULL DEFAULT 0"),
        ("buddy_steps_total", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE app_user ADD COLUMN {name} {ddl}")
    # Items the buddy found while walking (see buddy.roll_finds).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS buddy_find ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, "
        "item_key TEXT NOT NULL, tier TEXT NOT NULL, found_at TEXT NOT NULL)"
    )
    conn.commit()


REGION_LOCK_MODES = ("unlocked", "region", "previous_region")
DEFAULT_REGION_LOCK = "region"


def _migrate_region_lock(conn: sqlite3.Connection) -> None:
    """Region lock mode per user (see `achievements.unlocked_generations`),
    idempotent and additive.

    `app_user.region_lock`: how the wild spawn pool unlocks - 'unlocked'
    (everything from the start), 'region' (clearing a region's league unlocks
    that region's generation) or 'previous_region' (clearing a region unlocks
    the NEXT generation). The column default 'region' is what the game did
    before the option existed, so existing players keep playing exactly as
    they did.
    """
    if "region_lock" not in _table_columns(conn, "app_user"):
        conn.execute(
            "ALTER TABLE app_user ADD COLUMN region_lock TEXT NOT NULL DEFAULT 'region'"
        )
    conn.commit()


BATTLE_STYLES = ("set", "shift")
DEFAULT_BATTLE_STYLE = "set"


def _migrate_battle_style(conn: sqlite3.Connection) -> None:
    """Battle style per user for trainer battles (see `battle._check_faints`),
    idempotent and additive.

    `app_user.battle_style`: 'set' - the opponent's next Pokemon simply comes
    out - or 'shift' - after an opponent Pokemon faints you may switch for free
    before the next one fights. The column default 'set' is what the game did
    before the option existed, so existing players keep playing as before.
    """
    if "battle_style" not in _table_columns(conn, "app_user"):
        conn.execute(
            "ALTER TABLE app_user ADD COLUMN battle_style TEXT NOT NULL DEFAULT 'set'"
        )
    conn.commit()


GYM_DIFFICULTY_MODES = ("normal", "generation_limit", "same_generation")
DEFAULT_GYM_DIFFICULTY = "normal"


def _migrate_gym_difficulty(conn: sqlite3.Connection) -> None:
    """Gym/league difficulty per user (see `trainer_battle.start`), idempotent
    and additive.

    `app_user.gym_difficulty`: 'normal' - the whole party fights, scaled down
    to the trainer's level cap as always -, 'generation_limit' - only party
    members from the trainer's own generation or an earlier one are allowed to
    fight -, or 'same_generation' - only party members from exactly the
    trainer's (the league's) own generation are allowed to fight (trainer's
    region -> generation via `trainers.generation_for_region`, species
    generation from `species.generation`).
    The column default 'normal' is what the game did before the option
    existed, so existing players keep playing exactly as before.
    """
    if "gym_difficulty" not in _table_columns(conn, "app_user"):
        conn.execute(
            "ALTER TABLE app_user ADD COLUMN gym_difficulty TEXT NOT NULL DEFAULT 'normal'"
        )
    conn.commit()


ANIMATION_SPEEDS = ("off", "fast", "normal", "slow")
# Default for brand-new users (and the fallback for unknown/corrupt data);
# see upsert_user(). Animations on, normal speed.
DEFAULT_ANIMATION_SPEED = "normal"


def _migrate_animation_speed(conn: sqlite3.Connection) -> None:
    """Battle animation speed per user (see www/index.html renderBattle),
    idempotent and additive.

    `app_user.animation_speed`: one of ANIMATION_SPEEDS, purely a frontend
    CSS-timing knob (sprite entrance/faint/hit effects). The column default
    here is intentionally the literal 'off', not DEFAULT_ANIMATION_SPEED:
    this only backfills rows on databases that predate the animation_speed
    column, and 'off' matches what the game did before the option existed
    (no animations), so existing players keep playing exactly as before.
    New users get DEFAULT_ANIMATION_SPEED explicitly (see upsert_user()).
    """
    if "animation_speed" not in _table_columns(conn, "app_user"):
        conn.execute(
            "ALTER TABLE app_user ADD COLUMN animation_speed TEXT NOT NULL DEFAULT 'off'"
        )
    conn.commit()


def _migrate_auto_heal(conn: sqlite3.Connection) -> None:
    """Auto-heal preferences per user (see `autoheal.py`), idempotent and additive.

    `app_user.auto_heal`: shows the "auto heal" button in the team view.
    `app_user.auto_buy_potions`: re-buys the potions the auto heal used; only
    meaningful (and only accepted) together with `auto_heal`. Both default to
    off, so existing players keep playing exactly as before.
    """
    cols = _table_columns(conn, "app_user")
    if "auto_heal" not in cols:
        conn.execute("ALTER TABLE app_user ADD COLUMN auto_heal INTEGER NOT NULL DEFAULT 0")
    if "auto_buy_potions" not in cols:
        conn.execute("ALTER TABLE app_user ADD COLUMN auto_buy_potions INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def _migrate_auto_rules(conn: sqlite3.Connection) -> None:
    """Per-player auto rules (see `auto_rules.py`), idempotent and additive.

    `app_user.auto_rules_enabled`: the player's own on/off switch (default
    off, so nothing runs behind anyone's back after the update).
    `app_user.auto_rules_daily_cap`: max money auto-buy may spend per day
    (0 = no cap). `battle_log.rule_name`: which rule fought the battle.

    The old household switch `auto_battle_also_catches` becomes a rule: when it
    was on, every player without rules gets a "catch" rule (limited to caught
    species if `auto_battle_only_known_species` was on). Runs before init_db
    deletes the old settings; afterwards the switch is gone, so it seeds once.
    """
    cols = _table_columns(conn, "app_user")
    if "auto_rules_enabled" not in cols:
        conn.execute("ALTER TABLE app_user ADD COLUMN auto_rules_enabled INTEGER NOT NULL DEFAULT 0")
    if "auto_rules_daily_cap" not in cols:
        conn.execute("ALTER TABLE app_user ADD COLUMN auto_rules_daily_cap INTEGER NOT NULL DEFAULT 0")
    if "rule_name" not in _table_columns(conn, "battle_log"):
        conn.execute("ALTER TABLE battle_log ADD COLUMN rule_name TEXT")

    stored = {
        row["key"]: json.loads(row["value"])
        for row in conn.execute(
            "SELECT key, value FROM setting WHERE key IN "
            "('auto_battle_also_catches', 'auto_battle_only_known_species')"
        )
    }
    if stored.get("auto_battle_also_catches") is True:
        conditions = {"caught_before": True} \
            if stored.get("auto_battle_only_known_species", True) else {}
        # Old behaviour: beat it down, then one Poke Ball. chance_min 100 =
        # throw only once no safe move is left (lowest HP reachable).
        actions = {"mode": "catch", "ball": "specific", "ball_key": "poke_ball",
                   "chance_min": 100, "max_balls": 1, "on_fail": "leave"}
        now = datetime.now().astimezone().isoformat()
        users = conn.execute(
            "SELECT id FROM app_user WHERE id NOT IN (SELECT user_id FROM auto_rule)"
        ).fetchall()
        conn.executemany(
            "INSERT INTO auto_rule (user_id, position, name, enabled, conditions, actions, "
            "created_at) VALUES (?, 0, ?, 1, ?, ?, ?)",
            [(row["id"], "Auto-catch", json.dumps(conditions), json.dumps(actions), now)
             for row in users],
        )
    conn.commit()


def _migrate_species_min_ivs(conn: sqlite3.Connection) -> None:
    """IV floor per species (see `economy.species_min_ivs`), idempotent.

    `species.min_ivs`: JSON {hp,atk,...}; NULL = initial value (5 on every IV).
    Seeding (seed._upsert) only overwrites its own columns, so the floor is
    preserved when the Pokedex is reloaded.
    """
    if "min_ivs" not in _table_columns(conn, "species"):
        conn.execute("ALTER TABLE species ADD COLUMN min_ivs TEXT")
    conn.commit()


def _migrate_breeding(conn: sqlite3.Connection) -> None:
    """Breeding (see app/breeding.py), idempotent and additive. The tables
    `daycare`/`incubator`/`pokemon_egg` are created by the schema above.

    `species.egg_groups` (JSON list) / `species.hatch_counter`: backfilled
    from PokeAPI (breeding.start_background_backfill), NULL = fallback values.
    `app_user.egg_steps_at`: up to when passive egg steps have been credited.
    """
    species_cols = _table_columns(conn, "species")
    if "egg_groups" not in species_cols:
        conn.execute("ALTER TABLE species ADD COLUMN egg_groups TEXT")
    if "hatch_counter" not in species_cols:
        conn.execute("ALTER TABLE species ADD COLUMN hatch_counter INTEGER")
    if "egg_steps_at" not in _table_columns(conn, "app_user"):
        conn.execute("ALTER TABLE app_user ADD COLUMN egg_steps_at TEXT")
    conn.commit()


def _migrate_encounter_owner(conn: sqlite3.Connection) -> None:
    """Switch the queue from household-wide to per-user, idempotent.

    New: `encounter.user_id` - every trigger event creates its own row per
    user (see spawn.process_events). Existing rows deliberately keep NULL:
    they are shared household-wide and stay that way, so that running queues
    in a live DB don't suddenly belong to nobody (claim/attempt per user as
    before, see app/encounter_state.py).
    """
    if "user_id" not in _table_columns(conn, "encounter"):
        conn.execute("ALTER TABLE encounter ADD COLUMN user_id TEXT")
    conn.commit()


def _migrate_tms(conn: sqlite3.Connection) -> None:
    """TMs (see app/tms.py), idempotent and additive.

    `owned_pokemon.taught_moves` (JSON list of move keys): moves learned via
    TM. NULL/missing = none - existing data needs no backfill. The TMs
    themselves live in the inventory (`inventory`) like all items, so no
    table entry of their own is needed.

    `species.tm_moves` (JSON list of move keys): every move the species can
    learn from a TM/HM in any main-series game, backfilled from PokeAPI
    (`tms.start_background_backfill`). NULL = not fetched yet (offline start),
    then `tms.compatible` falls back to its type-based approximation; `[]`
    means "fetched, learns none".
    """
    if "taught_moves" not in _table_columns(conn, "owned_pokemon"):
        conn.execute("ALTER TABLE owned_pokemon ADD COLUMN taught_moves TEXT")
    if "tm_moves" not in _table_columns(conn, "species"):
        conn.execute("ALTER TABLE species ADD COLUMN tm_moves TEXT")
    conn.commit()


def _migrate_evs(conn: sqlite3.Connection) -> None:
    """Effort values (see app/evs.py), idempotent and additive.

    `owned_pokemon.evs` (JSON {hp,atk,def,spa,spd,spe}): EVs raised with
    vitamins/wings/berries. NULL/missing = all zero, so existing data needs no
    backfill - every read goes through `evs.as_evs`, which treats missing
    values as 0.
    """
    if "evs" not in _table_columns(conn, "owned_pokemon"):
        conn.execute("ALTER TABLE owned_pokemon ADD COLUMN evs TEXT")
    conn.commit()


def _migrate_ev_yield(conn: sqlite3.Connection) -> None:
    """EV yield per species (see app/evs.py), idempotent and additive.

    `species.ev_yield` (JSON {hp,atk,def,spa,spd,spe}): effort points a
    defeated member of the species gives, from the PokeAPI (`stats[].effort`).
    NULL = not fetched yet (existing rows, offline fallback) - it yields
    nothing until `seed.start_name_en_backfill()` has filled it in the
    background, exactly like `name_en`.
    """
    if "ev_yield" not in _table_columns(conn, "species"):
        conn.execute("ALTER TABLE species ADD COLUMN ev_yield TEXT")
    conn.commit()


def _migrate_show_team_ivs(conn: sqlite3.Connection) -> None:
    """Toggle for the IV line on each party card in the Team tab (see
    www/index.html monCard), idempotent and additive.

    `app_user.show_team_ivs`: defaults to on (1), since IVs were always shown
    before this setting existed - existing players keep seeing them until
    they switch it off themselves.
    """
    if "show_team_ivs" not in _table_columns(conn, "app_user"):
        conn.execute("ALTER TABLE app_user ADD COLUMN show_team_ivs INTEGER NOT NULL DEFAULT 1")
    conn.commit()


def _migrate_sprite_style(conn: sqlite3.Connection) -> None:
    """Sprite style per player (see `user_sprite_style`), idempotent and
    additive. `app_user.sprite_style`: NULL = the normal sprites, otherwise the
    generation whose game sprites are shown (needs that generation's living
    Pokedex achievement, achievements.living_generations)."""
    if "sprite_style" not in _table_columns(conn, "app_user"):
        conn.execute("ALTER TABLE app_user ADD COLUMN sprite_style INTEGER")
    conn.commit()


def _migrate_species_sprite_style(conn: sqlite3.Connection) -> None:
    """Sprite style per player AND species (see `user_species_sprite_styles`),
    idempotent and additive. Replaces the old global `app_user.sprite_style`
    (column kept, no longer read)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS species_sprite_style (
            user_id TEXT NOT NULL,
            species_id INTEGER NOT NULL,
            style INTEGER NOT NULL,
            PRIMARY KEY (user_id, species_id)
        )
        """
    )
    conn.commit()


def _migrate_box_item_return(conn: sqlite3.Connection) -> None:
    """Toggle for auto-returning a held item to the bag when a Pokemon is
    moved from the team to the box (see api/team.py: set_party), idempotent
    and additive.

    `app_user.box_item_return`: defaults to on (1) - a Pokemon sent to the
    box comes back unequipped and the item lands in the bag, unless the
    player switches this off.
    """
    if "box_item_return" not in _table_columns(conn, "app_user"):
        conn.execute("ALTER TABLE app_user ADD COLUMN box_item_return INTEGER NOT NULL DEFAULT 1")
    conn.commit()


def _migrate_auto_lock_first_catch(conn: sqlite3.Connection) -> None:
    """Toggle for auto-locking a species' very first catch (see
    api/encounters.py: `finalize_catch`), idempotent and additive.

    `app_user.auto_lock_first_catch`: defaults to off (0) - locking on first
    catch is an opt-in safety net against accidentally releasing a Pokemon
    that completes the Pokedex entry, same spirit as the automatic lock on
    legendary/mythical catches (rarity.auto_lock) but player-controlled.
    """
    if "auto_lock_first_catch" not in _table_columns(conn, "app_user"):
        conn.execute(
            "ALTER TABLE app_user ADD COLUMN auto_lock_first_catch INTEGER NOT NULL DEFAULT 0"
        )
    conn.commit()


def _migrate_auto_lock_shiny(conn: sqlite3.Connection) -> None:
    """Toggle for auto-locking every shiny catch (see api/encounters.py:
    `finalize_catch`), idempotent and additive.

    `app_user.auto_lock_shiny`: defaults to ON (1) - shiny Pokemon are rare
    enough that most players never want to release one by accident, so unlike
    `auto_lock_first_catch` this is opt-out, not opt-in.
    """
    if "auto_lock_shiny" not in _table_columns(conn, "app_user"):
        conn.execute(
            "ALTER TABLE app_user ADD COLUMN auto_lock_shiny INTEGER NOT NULL DEFAULT 1"
        )
    conn.commit()


def _migrate_auto_lock_living_dex(conn: sqlite3.Connection) -> None:
    """Toggle for auto-locking catches the living Pokedex still needs (see
    api/encounters.py: `finalize_catch`, evolution.living_dex_need),
    idempotent and additive.

    `app_user.auto_lock_living_dex`: defaults to off (0) - opt-in like
    `auto_lock_first_catch`, which it extends to the whole evolution line
    (a second Eevee still locks while Eeveelutions are missing).
    """
    if "auto_lock_living_dex" not in _table_columns(conn, "app_user"):
        conn.execute(
            "ALTER TABLE app_user ADD COLUMN auto_lock_living_dex INTEGER NOT NULL DEFAULT 0"
        )
    conn.commit()


def _migrate_god_mode(conn: sqlite3.Connection) -> None:
    """God mode switch per account (see app/god_mode.py), off for everyone -
    purely additive. Only an admin can turn it on, for an admin account."""
    if "god_mode" not in _table_columns(conn, "app_user"):
        conn.execute("ALTER TABLE app_user ADD COLUMN god_mode INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def _migrate_favorite(conn: sqlite3.Connection) -> None:
    """Favorite flag per owned Pokémon (party or box), settable by the player
    to filter the box (see www/index.html boxView) - purely additive.
    """
    if "favorite" not in _table_columns(conn, "owned_pokemon"):
        conn.execute("ALTER TABLE owned_pokemon ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def _migrate_locked(conn: sqlite3.Connection) -> None:
    """Lock flag per owned Pokémon (party or box): a locked Pokémon can't be
    released from the box (see api/team.py: `_release`). New legendary/mythical
    catches set it automatically (see api/encounters.py: `finalize_catch`,
    rarity.auto_lock); a species' very first catch does too if the player
    opted into `app_user.auto_lock_first_catch` (see `_migrate_auto_lock_first_catch`);
    the player can lock/unlock any other Pokémon by hand (`PUT /api/team/{id}/locked`).
    Existing rows default to unlocked - purely additive, same as `_migrate_favorite`.
    """
    if "locked" not in _table_columns(conn, "owned_pokemon"):
        conn.execute("ALTER TABLE owned_pokemon ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")
    conn.commit()


# Rarity tier weights before they became per-tier spawn shares (commit
# 4c0103d): tiny per-species multipliers, no paradox tier yet.
_LEGACY_RARITY_WEIGHTS = {
    "rarity_weight_common": 1.0,
    "rarity_weight_rare": 0.35,
    "rarity_weight_pseudo_legendary": 0.06,
    "rarity_weight_legendary": 0.01,
    "rarity_weight_mythical": 0.005,
}


def _migrate_rarity_weight_scale(conn: sqlite3.Connection) -> None:
    """Move untouched legacy rarity weights to the new share scale.

    `init_db` never overwrites a stored setting, so databases created before
    the per-tier normalisation kept the old defaults 1/0.35/0.06/0.01/0.005
    while the new paradox tier was seeded with 2.5 - paradox then took ~64 %
    of all spawns. Only when EVERY legacy tier still holds its old default
    (and paradox is missing or at its default) are all six weights reset to
    the current DEFAULT_SETTINGS; any admin customisation is left alone.
    Idempotent: afterwards the values no longer match the legacy set.
    """
    stored = {
        row["key"]: json.loads(row["value"])
        for row in conn.execute(
            "SELECT key, value FROM setting WHERE key LIKE 'rarity_weight_%'"
        )
    }
    if any(stored.get(key) != value for key, value in _LEGACY_RARITY_WEIGHTS.items()):
        return
    paradox_key = "rarity_weight_paradox"
    if stored.get(paradox_key, DEFAULT_SETTINGS[paradox_key]) != DEFAULT_SETTINGS[paradox_key]:
        return
    conn.executemany(
        "INSERT INTO setting(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [(key, json.dumps(DEFAULT_SETTINGS[key]))
         for key in (*_LEGACY_RARITY_WEIGHTS, paradox_key)],
    )
    conn.commit()


def _migrate_genderless_species(conn: sqlite3.Connection) -> None:
    """Ditto is genderless (see spawn.GENDERLESS_SPECIES): clear the male/female
    that older builds rolled for existing Ditto, in owned Pokemon and wild
    encounters. Runs on every start, which is cheap and idempotent - a stored
    gender on a genderless species is never valid.
    """
    from .spawn import GENDERLESS_SPECIES  # local import: spawn imports db

    ids = sorted(GENDERLESS_SPECIES)
    placeholders = ",".join("?" * len(ids))
    for table in ("owned_pokemon", "encounter"):
        conn.execute(
            f"UPDATE {table} SET gender = NULL "
            f"WHERE species_id IN ({placeholders}) AND gender IS NOT NULL",
            ids,
        )
    conn.commit()


def _migrate_egg_moves(conn: sqlite3.Connection) -> None:
    """Egg moves (see breeding.py), idempotent and additive.

    `pokemon_egg.egg_moves` / `owned_pokemon.egg_moves` (JSON list of move
    keys): egg moves inherited from the parents, fixed when the egg is laid and
    handed to the hatchling. NULL/missing = none - existing data needs no
    backfill.
    """
    if "egg_moves" not in _table_columns(conn, "pokemon_egg"):
        conn.execute("ALTER TABLE pokemon_egg ADD COLUMN egg_moves TEXT")
    if "egg_moves" not in _table_columns(conn, "owned_pokemon"):
        conn.execute("ALTER TABLE owned_pokemon ADD COLUMN egg_moves TEXT")
    conn.commit()


def _migrate_real_learnsets(conn: sqlite3.Connection) -> None:
    """Moves follow the species' real learnset (moves.py, data/learnsets.json)
    instead of the old "own types + normal" rule. Drop every battle move an
    owned Pokemon can't know (not a level-up move up to its level, no TM or
    egg move of it); an emptied selection falls back to the default moveset.
    Runs on every start, cheap and idempotent.
    """
    from . import moves as moves_mod  # local import, like spawn above

    rows = conn.execute(
        "SELECT o.*, s.type1, s.type2, s.base_atk, s.base_spa FROM owned_pokemon o "
        "JOIN species s ON s.id = o.species_id WHERE o.selected_moves IS NOT NULL"
    ).fetchall()
    for row in rows:
        owned = dict(row)
        species = {"id": owned["species_id"], "type1": owned["type1"], "type2": owned["type2"],
                   "base_atk": owned["base_atk"], "base_spa": owned["base_spa"]}
        try:
            selected = json.loads(owned["selected_moves"] or "null") or []
        except ValueError:
            selected = []
        known = set(moves_mod.owned_learned_moves(species, owned))
        kept = [key for key in selected if key in known]
        if kept != selected:
            conn.execute("UPDATE owned_pokemon SET selected_moves = ? WHERE id = ?",
                         (json.dumps(kept) if kept else None, owned["id"]))
    conn.commit()


def init_db() -> None:
    conn = connect()
    with _lock:
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate_multiuser(conn)
        _migrate_trigger_source(conn)
        _migrate_economy(conn)
        _migrate_battle(conn)
        _migrate_abilities(conn)
        _migrate_i18n(conn)
        _migrate_retired_items(conn)
        _migrate_encounter_user(conn)
        _migrate_battle_history(conn)
        _migrate_buddy(conn)
        _migrate_region_lock(conn)
        _migrate_auto_heal(conn)
        _migrate_auto_rules(conn)
        _migrate_battle_style(conn)
        _migrate_gym_difficulty(conn)
        _migrate_animation_speed(conn)
        _migrate_species_min_ivs(conn)
        _migrate_breeding(conn)
        _migrate_encounter_owner(conn)
        _migrate_tms(conn)
        _migrate_evs(conn)
        _migrate_ev_yield(conn)
        _migrate_show_team_ivs(conn)
        _migrate_box_item_return(conn)
        _migrate_auto_lock_first_catch(conn)
        _migrate_auto_lock_shiny(conn)
        _migrate_auto_lock_living_dex(conn)
        _migrate_god_mode(conn)
        _migrate_favorite(conn)
        _migrate_locked(conn)
        _migrate_rarity_weight_scale(conn)
        _migrate_genderless_species(conn)
        _migrate_sprite_style(conn)
        _migrate_species_sprite_style(conn)
        _migrate_egg_moves(conn)
        _migrate_real_learnsets(conn)
        # Create only AFTER the migration - on a freshly upgraded legacy DB
        # the referenced columns don't exist beforehand.
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_owned_user ON owned_pokemon(user_id);
            CREATE INDEX IF NOT EXISTS idx_owned_party ON owned_pokemon(user_id, party_slot);
            CREATE INDEX IF NOT EXISTS idx_dex_user ON dex_entry(user_id);
            CREATE INDEX IF NOT EXISTS idx_battle_log_user ON battle_log(user_id, id);
            CREATE INDEX IF NOT EXISTS idx_encounter_owner ON encounter(user_id, status);
            """
        )
        conn.commit()
    for key, value in DEFAULT_SETTINGS.items():
        if get_setting(key, _missing := object()) is _missing:
            set_setting(key, value)
    # Remove obsolete settings so they don't appear as raw keys in the
    # settings page (PATCH would reject them anyway).
    with cursor() as cur:
        cur.executemany("DELETE FROM setting WHERE key = ?",
                        [(key,) for key in OBSOLETE_SETTINGS])


# ================================================================ Users

def get_user(user_id: str) -> dict[str, Any] | None:
    return query_one("SELECT * FROM app_user WHERE id = ?", (user_id,))


def user_language(user_id: str) -> str:
    """Language preference of a user - for places that build server-side
    text (battle log, error messages) and only know the user_id,
    not the full current_user object from the auth dependency."""
    user = get_user(user_id)
    return (user or {}).get("language") or "en"


def set_user_language(user_id: str, language: str) -> dict[str, Any]:
    execute("UPDATE app_user SET language = ? WHERE id = ?", (language, user_id))
    user = get_user(user_id)
    assert user is not None
    return user


def user_region_lock(user_id: str) -> str:
    """Region lock mode of a user (one of REGION_LOCK_MODES); unknown users
    and unexpected stored values fall back to the default."""
    mode = (get_user(user_id) or {}).get("region_lock")
    return mode if mode in REGION_LOCK_MODES else DEFAULT_REGION_LOCK


def set_user_region_lock(user_id: str, mode: str) -> dict[str, Any]:
    if mode not in REGION_LOCK_MODES:
        raise ValueError(f"Unknown region lock mode: {mode}")
    execute("UPDATE app_user SET region_lock = ? WHERE id = ?", (mode, user_id))
    user = get_user(user_id)
    assert user is not None
    return user


def user_show_team_ivs(user_id: str) -> bool:
    """Whether the Team tab shows each party member's IVs; unknown users and
    legacy rows without the column default to on (previous behavior)."""
    user = get_user(user_id)
    return True if user is None else bool(user.get("show_team_ivs", 1))


def set_user_show_team_ivs(user_id: str, show: bool) -> dict[str, Any]:
    execute("UPDATE app_user SET show_team_ivs = ? WHERE id = ?", (int(show), user_id))
    user = get_user(user_id)
    assert user is not None
    return user


def user_species_sprite_styles(user_id: str) -> dict[int, int]:
    """species_id -> generation whose game sprites this player sees for that
    species (www/index.html SPRITE_STYLES); species not listed use the normal
    sprites."""
    rows = query("SELECT species_id, style FROM species_sprite_style WHERE user_id = ?", (user_id,))
    return {int(r["species_id"]): int(r["style"]) for r in rows}


def set_species_sprite_style(user_id: str, species_id: int, generation: int | None) -> None:
    if generation is None:
        execute("DELETE FROM species_sprite_style WHERE user_id = ? AND species_id = ?",
                (user_id, species_id))
    else:
        execute("INSERT OR REPLACE INTO species_sprite_style (user_id, species_id, style) "
                "VALUES (?, ?, ?)", (user_id, species_id, generation))


def user_box_item_return(user_id: str) -> bool:
    """Whether moving a Pokemon from the team to the box also returns its
    held item to the bag (see api/team.py: set_party); unknown users and
    legacy rows without the column default to on (the requested default)."""
    user = get_user(user_id)
    return True if user is None else bool(user.get("box_item_return", 1))


def set_user_box_item_return(user_id: str, value: bool) -> dict[str, Any]:
    execute("UPDATE app_user SET box_item_return = ? WHERE id = ?", (int(value), user_id))
    user = get_user(user_id)
    assert user is not None
    return user


def user_auto_lock_first_catch(user_id: str) -> bool:
    """Whether a species' very first catch should start locked (see
    api/encounters.py: `finalize_catch`); unknown users and legacy rows
    without the column default to off."""
    user = get_user(user_id)
    return False if user is None else bool(user.get("auto_lock_first_catch", 0))


def set_user_auto_lock_first_catch(user_id: str, value: bool) -> dict[str, Any]:
    execute("UPDATE app_user SET auto_lock_first_catch = ? WHERE id = ?", (int(value), user_id))
    user = get_user(user_id)
    assert user is not None
    return user


def user_auto_lock_shiny(user_id: str) -> bool:
    """Whether a newly caught shiny Pokemon should start locked (see
    api/encounters.py: `finalize_catch`); unknown users and legacy rows
    without the column default to on."""
    user = get_user(user_id)
    return True if user is None else bool(user.get("auto_lock_shiny", 1))


def set_user_auto_lock_shiny(user_id: str, value: bool) -> dict[str, Any]:
    execute("UPDATE app_user SET auto_lock_shiny = ? WHERE id = ?", (int(value), user_id))
    user = get_user(user_id)
    assert user is not None
    return user


def user_auto_lock_living_dex(user_id: str) -> bool:
    """Whether a catch the living Pokedex still needs should start locked (see
    api/encounters.py: `finalize_catch`); unknown users and legacy rows
    without the column default to off."""
    user = get_user(user_id)
    return False if user is None else bool(user.get("auto_lock_living_dex", 0))


def set_user_auto_lock_living_dex(user_id: str, value: bool) -> dict[str, Any]:
    execute("UPDATE app_user SET auto_lock_living_dex = ? WHERE id = ?", (int(value), user_id))
    user = get_user(user_id)
    assert user is not None
    return user


def user_battle_style(user_id: str) -> str:
    """Battle style of a user (one of BATTLE_STYLES); unknown users and
    unexpected stored values fall back to the default."""
    style = (get_user(user_id) or {}).get("battle_style")
    return style if style in BATTLE_STYLES else DEFAULT_BATTLE_STYLE


def set_user_battle_style(user_id: str, style: str) -> dict[str, Any]:
    if style not in BATTLE_STYLES:
        raise ValueError(f"Unknown battle style: {style}")
    execute("UPDATE app_user SET battle_style = ? WHERE id = ?", (style, user_id))
    user = get_user(user_id)
    assert user is not None
    return user


def user_gym_difficulty(user_id: str) -> str:
    """Gym/league difficulty of a user (one of GYM_DIFFICULTY_MODES); unknown
    users and unexpected stored values fall back to the default."""
    mode = (get_user(user_id) or {}).get("gym_difficulty")
    return mode if mode in GYM_DIFFICULTY_MODES else DEFAULT_GYM_DIFFICULTY


def set_user_gym_difficulty(user_id: str, mode: str) -> dict[str, Any]:
    if mode not in GYM_DIFFICULTY_MODES:
        raise ValueError(f"Unknown gym difficulty: {mode}")
    execute("UPDATE app_user SET gym_difficulty = ? WHERE id = ?", (mode, user_id))
    user = get_user(user_id)
    assert user is not None
    return user


def user_animation_speed(user_id: str) -> str:
    """Battle animation speed of a user (one of ANIMATION_SPEEDS); unknown
    users and unexpected stored values fall back to the default."""
    speed = (get_user(user_id) or {}).get("animation_speed")
    return speed if speed in ANIMATION_SPEEDS else DEFAULT_ANIMATION_SPEED


def set_user_animation_speed(user_id: str, speed: str) -> dict[str, Any]:
    if speed not in ANIMATION_SPEEDS:
        raise ValueError(f"Unknown animation speed: {speed}")
    execute("UPDATE app_user SET animation_speed = ? WHERE id = ?", (speed, user_id))
    user = get_user(user_id)
    assert user is not None
    return user


def user_auto_heal(user_id: str) -> dict[str, bool]:
    """Auto-heal preferences of a user. `auto_buy_potions` is only ever true
    together with `auto_heal`."""
    user = get_user(user_id) or {}
    auto_heal = bool(user.get("auto_heal"))
    return {"auto_heal": auto_heal,
            "auto_buy_potions": auto_heal and bool(user.get("auto_buy_potions"))}


def set_user_auto_heal(
    user_id: str, auto_heal: bool | None = None, auto_buy_potions: bool | None = None,
) -> dict[str, bool]:
    """Updates the auto-heal preferences (None = keep). Turning auto heal off
    also turns auto buy off; enabling auto buy while auto heal is (and stays)
    off raises ValueError."""
    current = user_auto_heal(user_id)
    new_heal = current["auto_heal"] if auto_heal is None else bool(auto_heal)
    if auto_buy_potions is None:
        new_buy = current["auto_buy_potions"] and new_heal
    else:
        new_buy = bool(auto_buy_potions)
    if new_buy and not new_heal:
        raise ValueError("auto_buy_potions requires auto_heal")
    execute("UPDATE app_user SET auto_heal = ?, auto_buy_potions = ? WHERE id = ?",
            (int(new_heal), int(new_buy), user_id))
    return user_auto_heal(user_id)


def upsert_user(user_id: str, name: str, display_name: str | None = None) -> dict[str, Any]:
    """Create a user or update name/timestamp on each visit.

    animation_speed is set explicitly on INSERT (not left to the column
    default, which stays 'off' for legacy-database backfills, see
    _migrate_animation_speed) and left untouched on UPDATE, so existing
    users keep their own choice.
    """
    now = _now_iso()
    db_execute = execute  # local to avoid recursion when reading below
    db_execute(
        """
        INSERT INTO app_user (id, name, display_name, animation_speed, created_at, last_seen_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            display_name = excluded.display_name,
            last_seen_at = excluded.last_seen_at
        """,
        (user_id, name, display_name, DEFAULT_ANIMATION_SPEED, now, now),
    )
    user = get_user(user_id)
    assert user is not None
    return user


def list_users() -> list[dict[str, Any]]:
    return query("SELECT * FROM app_user ORDER BY created_at")


# Who counts as an active participant in the household?
ELIGIBLE_DAYS = 7
_TEST_USER_MARKER = "test"


def _seen_within(value: str | None, cutoff: datetime) -> bool:
    try:
        seen = datetime.fromisoformat(value or "")
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.astimezone()
    return seen >= cutoff


def eligible_users(now: datetime | None = None) -> list[dict[str, Any]]:
    """Users who count toward shared gameplay.

    These are active household members: been in the app in the last `ELIGIBLE_DAYS`
    days and not a test account ("test" in name or display name, case insensitive).
    Used e.g. by `encounter_state.settle` to decide when an encounter definitively
    leaves the queue - an account inactive for months shouldn't block it.
    """
    cutoff = (now or datetime.now().astimezone()) - timedelta(days=ELIGIBLE_DAYS)
    return [
        user
        for user in list_users()
        if _seen_within(user.get("last_seen_at"), cutoff)
        and _TEST_USER_MARKER not in (user.get("name") or "").casefold()
        and _TEST_USER_MARKER not in (user.get("display_name") or "").casefold()
    ]


# ============================================================= Settings

def get_setting(key: str, default: Any = None) -> Any:
    with cursor() as cur:
        cur.execute("SELECT value FROM setting WHERE key = ?", (key,))
        row = cur.fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


def set_setting(key: str, value: Any) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO setting(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


def all_settings() -> dict[str, Any]:
    with cursor() as cur:
        cur.execute("SELECT key, value FROM setting")
        rows = cur.fetchall()
    return {row["key"]: json.loads(row["value"]) for row in rows}


# ================================================================ Misc

def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def query_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params: tuple = ()) -> int:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.lastrowid
