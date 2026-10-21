"""Battle, catch, and level logic.

Deliberately designed close to mainline games or PokeRogue:
* Catch chance depends on remaining HP of wild Pokemon.
* A catch attempt costs a ball and gives the opponent another action on failure
  (enforced by the flow in `attempt_catch` / `throw_ball` - the turn is
  handled by the caller).
* Critical catch with chance dependent on Pokedex progress.
* A wild Pokemon that has already leveled in this battle is no longer
  catchable in this battle (`leveled_in_battle`). It can level by earning EXP
  from defeated team members (`wild_gain_exp`).
* Turn-based battle engine (`Battle`, `run_turn`, `switch_to`) with real
  moves (`moves.py`), accuracy, critical hits, STAB, type effectiveness,
  physical/special, and held items (`held_items.py`). Manual battles
  (`battle_session.py`) and auto-battles use the same engine.

This file is the reference implementation of the rules. Once the frontend is
based on PokeRogue, the presentation moves there - the rules stay here so that
offline auto-battles use the same logic as the UI.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import abilities, db, held_items, i18n, mega, roaming
from . import moves as moves_mod
from .battle_events import Event, emit

# ------------------------------------------------------------------- Balls
# Ball tiers and special balls analogous to mainline games (values from Gen. 7).
# `bonus` is the fixed base multiplier; special balls have an additional
# `effect`, which `ball_multiplier` evaluates against the current catch context
# (`CatchContext`). If the condition is not met, they act like a Pokéball (x1).
# Balls whose condition the game does not model (friendship, fishing, weight, ...)
# intentionally do not exist.
BALLS: dict[str, dict[str, Any]] = {
    "poke_ball":   {"label": "Poké Ball",   "label_de": "Pokéball",    "bonus": 1.0, "tier": 1},
    "great_ball":  {"label": "Great Ball",  "label_de": "Superball",   "bonus": 1.5, "tier": 2},
    "ultra_ball":  {"label": "Ultra Ball",  "label_de": "Hyperball",   "bonus": 2.0, "tier": 3},
    "net_ball":    {"label": "Net Ball",    "label_de": "Netzball",    "bonus": 1.0, "tier": 2, "effect": "net"},
    "dive_ball":   {"label": "Dive Ball",   "label_de": "Tauchball",   "bonus": 1.0, "tier": 2, "effect": "dive"},
    "nest_ball":   {"label": "Nest Ball",   "label_de": "Nestball",    "bonus": 1.0, "tier": 2, "effect": "nest"},
    "repeat_ball": {"label": "Repeat Ball", "label_de": "Wiederball",  "bonus": 1.0, "tier": 2, "effect": "repeat"},
    "timer_ball":  {"label": "Timer Ball",  "label_de": "Timerball",   "bonus": 1.0, "tier": 2, "effect": "timer"},
    "quick_ball":  {"label": "Quick Ball",  "label_de": "Flottball",   "bonus": 1.0, "tier": 2, "effect": "quick"},
    "dusk_ball":   {"label": "Dusk Ball",   "label_de": "Finsterball", "bonus": 1.0, "tier": 2, "effect": "dusk"},
    "level_ball":  {"label": "Level Ball",  "label_de": "Levelball",   "bonus": 1.0, "tier": 2, "effect": "level"},
    "fast_ball":   {"label": "Fast Ball",   "label_de": "Turboball",   "bonus": 1.0, "tier": 2, "effect": "fast"},
    "heal_ball":   {"label": "Heal Ball",   "label_de": "Heilball",    "bonus": 1.0, "tier": 1, "effect": "heal"},
    "master_ball": {"label": "Master Ball", "label_de": "Meisterball", "bonus": 255.0, "tier": 5},
}

# Retired balls (e.g., the former Rogueball) are swapped in the inventory
# by the migration `db._migrate_retired_items` for an equivalent replacement.

NET_BALL_TYPES = {"water", "bug"}
DIVE_BALL_BIOMES = {"lake", "beach"}      # Water biomes from geo-triggers
DUSK_NIGHT_START, DUSK_NIGHT_END = 20, 6  # Night = 20:00-05:59 local time
NEST_BALL_MAX_LEVEL = 30
# Hard level cap: no EXP, Rare Candy, buddy steps, spawn or catch may exceed it.
MAX_LEVEL = 100
TIMER_BALL_MAX = 4.0
TIMER_BALL_PER_TURN = 1229 / 4096         # Like Gen. 5+: approx. +0.3 per turn
FAST_BALL_MIN_SPEED = 100


def ball_label(key: str, lang: str = "en") -> str:
    ball = BALLS.get(key, BALLS["poke_ball"])
    return (ball.get("label_de") if lang == "de" else None) or ball["label"]


@dataclass
class CatchContext:
    """What special balls need to know about the situation.

    * `turn`: turns already played in the ongoing battle (0 outside).
    * `first_turn`: no turn has been played against this Pokemon yet
      (Quick Ball) - also no prior battle/missed throw.
    * `player_level`: level of own active (outside of battle: first)
      team Pokemon (Level Ball).
    * `species_caught`: species already caught by this user (Repeat Ball).
    * `biome`: biome of encounter, only set for geo-triggers (Dive Ball).
    * `now`: local time (Dusk Ball); None = now.
    """
    turn: int = 0
    first_turn: bool = False
    player_level: int | None = None
    species_caught: bool = False
    biome: str | None = None
    now: datetime | None = None


def is_night(now: datetime | None = None) -> bool:
    hour = (now or datetime.now().astimezone()).hour
    return hour >= DUSK_NIGHT_START or hour < DUSK_NIGHT_END


def ball_multiplier(ball_key: str, wild: Combatant, ctx: CatchContext | None = None) -> float:
    """Effective catch multiplier of a ball in the current situation."""
    ball = BALLS.get(ball_key, BALLS["poke_ball"])
    effect = ball.get("effect")
    ctx = ctx or CatchContext()
    bonus = float(ball["bonus"])
    if effect == "net":
        return 3.5 if NET_BALL_TYPES & set(wild.types) else bonus
    if effect == "dive":
        return 3.5 if ctx.biome in DIVE_BALL_BIOMES else bonus
    if effect == "nest":
        if wild.level < NEST_BALL_MAX_LEVEL:
            return max(bonus, (41 - wild.level) / 10)
        return bonus
    if effect == "repeat":
        return 3.5 if ctx.species_caught else bonus
    if effect == "timer":
        return min(TIMER_BALL_MAX, bonus + max(0, ctx.turn) * TIMER_BALL_PER_TURN)
    if effect == "quick":
        return 5.0 if ctx.first_turn else bonus
    if effect == "dusk":
        return 3.0 if is_night(ctx.now) else bonus
    if effect == "level":
        mine = ctx.player_level or 0
        if mine >= 4 * wild.level:
            return 8.0
        if mine > 2 * wild.level:
            return 4.0
        if mine > wild.level:
            return 2.0
        return bonus
    if effect == "fast":
        return 4.0 if (wild.species.get("base_spe") or 0) >= FAST_BALL_MIN_SPEED else bonus
    return bonus


def ball_options(
    wild: Combatant, ctx: CatchContext, inventory: dict[str, int] | None = None
) -> list[dict[str, Any]]:
    """Ball list for UI selection: label, stock, current
    multiplier and whether a special ball is currently using its bonus."""
    inventory = inventory or {}
    options = []
    for key, ball in BALLS.items():
        multiplier = ball_multiplier(key, wild, ctx)
        options.append({
            "key": key, "label": ball["label"], "label_de": ball.get("label_de"),
            "count": inventory.get(key, 0),
            "effect": ball.get("effect"),
            "multiplier": round(multiplier, 2),
            "boosted": bool(ball.get("effect")) and multiplier > ball["bonus"],
        })
    return options

STATUS_BONUS = {
    None: 1.0, "none": 1.0,
    "burn": 1.5, "paralysis": 1.5, "poison": 1.5, "toxic": 1.5,
    "sleep": 2.5, "freeze": 2.5,
}

# As in mainline games: certain types are immune to certain statuses,
# regardless of the type of the inflicting move (this is separate from the
# normal type effectiveness in TYPE_CHART, which e.g. Ghost protects against
# normal moves - here it's only about the status itself).
STATUS_IMMUNE_TYPES: dict[str, set[str]] = {
    "burn": {"fire"},
    "poison": {"poison", "steel"},
    "toxic": {"poison", "steel"},
    "paralysis": {"electric"},
    "freeze": {"ice"},
}

def status_verb(status: str, lang: str) -> str:
    return i18n.t(lang, f"battle.status_verb.{status}")

# Damage per turn as a fraction of max HP.
STATUS_TICK_FRACTION = {"poison": 1 / 8, "burn": 1 / 16, "curse": 1 / 4}
TOXIC_MAX_TICKS = 15  # Capped at 15/16 max HP as in mainline games

# Only deviations from 1.0; everything not listed is neutral.
TYPE_CHART: dict[str, dict[str, float]] = {
    "normal":   {"rock": 0.5, "ghost": 0.0, "steel": 0.5},
    "fire":     {"fire": 0.5, "water": 0.5, "grass": 2, "ice": 2, "bug": 2, "rock": 0.5, "dragon": 0.5, "steel": 2},
    "water":    {"fire": 2, "water": 0.5, "grass": 0.5, "ground": 2, "rock": 2, "dragon": 0.5},
    "electric": {"water": 2, "electric": 0.5, "grass": 0.5, "ground": 0.0, "flying": 2, "dragon": 0.5},
    "grass":    {"fire": 0.5, "water": 2, "grass": 0.5, "poison": 0.5, "ground": 2, "flying": 0.5, "bug": 0.5, "rock": 2, "dragon": 0.5, "steel": 0.5},
    "ice":      {"fire": 0.5, "water": 0.5, "grass": 2, "ice": 0.5, "ground": 2, "flying": 2, "dragon": 2, "steel": 0.5},
    "fighting": {"normal": 2, "ice": 2, "poison": 0.5, "flying": 0.5, "psychic": 0.5, "bug": 0.5, "rock": 2, "ghost": 0.0, "dark": 2, "steel": 2, "fairy": 0.5},
    "poison":   {"grass": 2, "poison": 0.5, "ground": 0.5, "rock": 0.5, "ghost": 0.5, "steel": 0.0, "fairy": 2},
    "ground":   {"fire": 2, "electric": 2, "grass": 0.5, "poison": 2, "flying": 0.0, "bug": 0.5, "rock": 2, "steel": 2},
    "flying":   {"electric": 0.5, "grass": 2, "fighting": 2, "bug": 2, "rock": 0.5, "steel": 0.5},
    "psychic":  {"fighting": 2, "poison": 2, "psychic": 0.5, "dark": 0.0, "steel": 0.5},
    "bug":      {"fire": 0.5, "grass": 2, "fighting": 0.5, "poison": 0.5, "flying": 0.5, "psychic": 2, "ghost": 0.5, "dark": 2, "steel": 0.5, "fairy": 0.5},
    "rock":     {"fire": 2, "ice": 2, "fighting": 0.5, "ground": 0.5, "flying": 2, "bug": 2, "steel": 0.5},
    "ghost":    {"normal": 0.0, "psychic": 2, "ghost": 2, "dark": 0.5},
    "dragon":   {"dragon": 2, "steel": 0.5, "fairy": 0.0},
    "dark":     {"fighting": 0.5, "psychic": 2, "ghost": 2, "dark": 0.5, "fairy": 0.5},
    "steel":    {"fire": 0.5, "water": 0.5, "electric": 0.5, "ice": 2, "rock": 2, "steel": 0.5, "fairy": 2},
    "fairy":    {"fire": 0.5, "fighting": 2, "poison": 0.5, "dragon": 2, "dark": 2, "steel": 0.5},
}

GROWTH_RATE_FACTOR = {
    "fast": 0.8, "medium_fast": 1.0, "medium": 1.0,
    "medium_slow": 1.2, "slow": 1.25, "erratic": 1.0, "fluctuating": 1.0,
}


# ------------------------------------------------------------------- Stats
# Formulas as in the main games from generation 3 on, without nature:
# every 4 EVs (effort values, see app/evs.py - here only via items) count like
# one more IV point, so a maxed stat (252 EV) is worth +63 at level 100.

# Shedinja (base_hp 1) is a hardcoded exception to the HP formula in every
# generation it exists in (Gen 3 onward, unchanged through Gen 9): it always
# has exactly 1 max HP, regardless of level, IVs or EVs.
SHEDINJA_SPECIES_ID = 292


def stat_hp(base: int, iv: int, level: int, ev: int = 0, species_id: int | None = None) -> int:
    if species_id == SHEDINJA_SPECIES_ID:
        return 1
    return math.floor((2 * base + iv + ev // 4) * level / 100) + level + 10


def stat_other(base: int, iv: int, level: int, ev: int = 0) -> int:
    return math.floor((2 * base + iv + ev // 4) * level / 100) + 5


# Stat stages (moves like Swords Dance/Growl): -6 .. +6, like in the
# main games. Only the five "real" stats - accuracy/evasion do not
# exist in this simplified engine.
STAT_STAGE_KEYS = ("atk", "def", "spa", "spd", "spe")
STAT_STAGE_MIN, STAT_STAGE_MAX = -6, 6


def stat_stage_multiplier(stage: int) -> float:
    stage = max(STAT_STAGE_MIN, min(STAT_STAGE_MAX, stage))
    return (2 + stage) / 2 if stage >= 0 else 2 / (2 - stage)


@dataclass
class Combatant:
    species: dict[str, Any]
    level: int
    ivs: dict[str, int]
    # Effort values (see app/evs.py, `owned_pokemon.evs`). Only own Pokemon
    # ever have any - wild Pokemon, trainer teams and imported team codes
    # fight with all zero, which an empty dict means.
    evs: dict[str, int] = field(default_factory=dict)
    max_hp: int = 0
    current_hp: int = 0
    status: str | None = None
    # sleep: remaining sleep rounds. toxic: number of prior ticks
    # (increasing damage). Unused for all other statuses (0).
    status_counter: int = 0
    owned_id: int | None = None
    leveled_in_battle: bool = False
    exp_gained: int = 0
    types: list[str] = field(default_factory=list)
    # Battle engine (see below): moves, held item, choice lock.
    moves: list[str] = field(default_factory=list)
    held_item: str | None = None
    consumed_item: str | None = None
    # Ability, rolled once at spawn/catch/starter (see spawn.roll_ability) -
    # never changes during a battle, lives like held_item NOT in battle_session
    # state, but is read fresh from the DB each time via
    # combatant_from_encounter/combatant_from_owned. None is explicitly
    # allowed (ability seeding not yet run, or pre-existing Pokemon from before
    # this feature) - every ability check below must treat that as "no effect".
    ability: str | None = None
    choice_lock: str | None = None
    # Stat stages (see STAT_STAGE_KEYS) - battle-internal state like status,
    # lives like that NOT in owned_pokemon, but only in session state (see
    # mon_state/apply_mon_state, battle_session._to_state). Reset when
    # switching the holder (see switch_to).
    stat_stages: dict[str, int] = field(default_factory=lambda: {k: 0 for k in STAT_STAGE_KEYS})
    # Flinch: only relevant until the end of the CURRENT turn - like
    # stat_stages battle-internal state, but even shorter-lived (see
    # use_move/run_turn); also does not live in owned_pokemon.
    flinched: bool = False
    # Ability state that lasts until the holder leaves the field (e.g. Flash
    # Fire's boost, see ability_effects.py) - battle state like stat_stages,
    # persisted in the session (battle_session._to_state, trainer_battle.
    # mon_state) and cleared on switching out (see switch_to).
    volatile: dict[str, Any] = field(default_factory=dict)
    # Mega Evolution (see mega.py): item key of the active Mega Stone while
    # the form lasts - pure battle state like status/stat_stages, living only
    # in the session (mega_evolve sets it, restore_mega restores it on load).
    # `mega_base` remembers the base form's species/types/ability so that
    # level-ups at the end of the battle keep using the real species (see
    # refresh_after_level).
    mega: str | None = None
    mega_base: dict[str, Any] | None = field(default=None, repr=False)
    is_wild: bool = False
    # Roaming legendary (see roaming.py): runs away on its turn unless `trapped`
    # (Mean Look, Spider Web) - which lasts for the battle, see run_turn.
    roams: bool = False
    trapped: bool = False
    exp: int = 0  # only for wild Pokemon (level up during battle)
    trainer: str | None = None  # Name of opposing trainer (gym/league)

    def __post_init__(self) -> None:
        if not self.types:
            self.types = [t for t in (self.species["type1"], self.species.get("type2")) if t]
        if not self.max_hp:
            self.max_hp = stat_hp(self.species["base_hp"], self.ivs.get("hp", 15), self.level,
                                  self.evs.get("hp", 0), species_id=self.species.get("id"))
        if not self.current_hp:
            self.current_hp = self.max_hp
        if not self.moves:
            self.moves = moves_mod.moveset_for(self.species, self.level)

    @property
    def fainted(self) -> bool:
        return self.current_hp <= 0

    @property
    def name(self) -> str:
        return self.species.get("name_en") or self.species.get("name_de") or self.species["name"]

    def stat(self, key: str) -> int:
        base = self.species[f"base_{key}"]
        return stat_other(base, self.ivs.get(key, 15), self.level, self.evs.get(key, 0))

    @property
    def base_species(self) -> dict[str, Any]:
        """Species without Mega form - basis for everything that applies
        beyond the battle (level-up, learning moves)."""
        return (self.mega_base or {}).get("species") or self.species

    @property
    def base_ability(self) -> str | None:
        """Ability without Mega form (the Mega ability only applies in battle)."""
        return self.mega_base["ability"] if self.mega_base else self.ability

    def refresh_after_level(self) -> list[str]:
        """After a level up: HP gain like in the main games (the difference
        is added on top) and derive moveset anew. Returns newly learned moves.
        Deliberately uses `base_species`: a Mega form only applies for this
        battle, the level belongs to the real species."""
        new_max = stat_hp(self.base_species["base_hp"], self.ivs.get("hp", 15), self.level,
                          self.evs.get("hp", 0), species_id=self.base_species.get("id"))
        if not self.fainted:
            self.current_hp = min(new_max, self.current_hp + max(0, new_max - self.max_hp))
        self.max_hp = new_max
        old_moves = list(self.moves)
        self.moves = moves_mod.moveset_for(self.base_species, self.level)
        if self.choice_lock and self.choice_lock not in self.moves:
            self.choice_lock = None
        return [key for key in self.moves if key not in old_moves]


def _as_ivs(value: Any) -> dict[str, int]:
    """Accepts both JSON string from DB and a finished dict."""
    if isinstance(value, dict):
        return value
    return json.loads(value or "{}")


def _as_evs(value: Any) -> dict[str, int]:
    """Like `_as_ivs` for `owned_pokemon.evs` (NULL/missing = all zero). The
    rules around EVs live in app/evs.py - kept here so that module can import
    battle without a cycle."""
    if isinstance(value, dict):
        return {key: int(value.get(key) or 0) for key in value}
    return {key: int(v or 0) for key, v in json.loads(value or "{}").items()}


def combatant_from_encounter(encounter: dict[str, Any]) -> Combatant:
    species = db.query_one("SELECT * FROM species WHERE id = ?", (encounter["species_id"],))
    combatant = Combatant(
        species=species, level=encounter["level"], ivs=_as_ivs(encounter["ivs"]),
        is_wild=True, ability=encounter.get("ability"),
        roams=roaming.is_roaming(species["id"]),
    )
    if encounter.get("current_hp") is not None:
        combatant.current_hp = min(encounter["current_hp"], combatant.max_hp)
    return combatant


def combatant_from_owned(owned: dict[str, Any], level: int | None = None) -> Combatant:
    """`level` < real level: level-scaled battle copy (gym/league). Selected
    moves the Pokemon doesn't know yet at this level (`moves.learned_moves`)
    are dropped and filled with the standard moveset of the battle level."""
    species = db.query_one("SELECT * FROM species WHERE id = ?", (owned["species_id"],))
    selected = json.loads(owned.get("selected_moves") or "null") or []
    if level is not None and level < owned["level"]:
        known = set(moves_mod.owned_learned_moves(species, owned, level))
        selected = [key for key in selected if key in known]
        for key in moves_mod.moveset_for(species, level):
            if len(selected) >= 4:
                break
            if key not in selected:
                selected.append(key)
    else:
        level = owned["level"]
    combatant = Combatant(
        species=species, level=level, ivs=_as_ivs(owned["ivs"]),
        evs=_as_evs(owned.get("evs")),
        owned_id=owned["id"], held_item=owned.get("held_item"), moves=selected,
        ability=owned.get("ability"),
    )
    if owned.get("current_hp") is not None:
        combatant.current_hp = min(owned["current_hp"], combatant.max_hp)
    return combatant


# ------------------------------------------------------------------ Damage

def type_effectiveness(attack_type: str, defender_types: list[str]) -> float:
    multiplier = 1.0
    row = TYPE_CHART.get(attack_type, {})
    for defender_type in defender_types:
        multiplier *= row.get(defender_type, 1.0)
    return multiplier


def _type_effectiveness_for(
    attack_type: str, defender: "Combatant", move: dict[str, Any] | None = None
) -> float:
    """Like `type_effectiveness`, but including ability immunities that
    apply independently of own type (currently only Levitate against Ground)
    and Wonder Guard, which needs `move` to tell a damaging hit from a status
    move's type check (e.g. Thunder Wave vs. Ground) - only the former is
    blocked when not super effective. Use everywhere the effect of ONE
    concrete move on ONE concrete target is checked (not in the pure type
    chart)."""
    return emit(Event.TYPE_EFFECT, defender, move_type=attack_type, move=move,
                value=type_effectiveness(attack_type, defender.types)).value


def _ability_blocks_status(target: "Combatant", status: str, b: "Battle | None" = None) -> bool:
    """Innate status immunity from an ability (e.g., immunity to poison) -
    in addition to type-based STATUS_IMMUNE_TYPES above. With `b`, the
    blocking ability also writes its log line."""
    return emit(Event.BEFORE_STATUS, target, battle=b, status=status).cancelled


def _stab_multiplier(attacker: "Combatant", move: dict[str, Any]) -> float:
    """STAB (Same Type Attack Bonus): 1.5x normally, 2x with Adaptability."""
    if move["type"] not in attacker.types:
        return 1.0
    return emit(Event.STAB, attacker, move=move, value=1.5).value


# Critical hit stages (Gen 7+): 1/24, 1/8, 1/2, guaranteed.
CRIT_CHANCE = [1 / 24, 1 / 8, 1 / 2, 1.0]


def effective_stat(combatant: Combatant, key: str) -> float:
    """Stat value including item modifiers (Choice Band/-Specs/-Scarf), status
    (Paralysis halves Speed, as in current mainline games) and Guts (preserves
    Attack during Burn AND boosts it by 50% with ANY status, instead of
    lowering it)."""
    value: float = combatant.stat(key)
    value *= stat_stage_multiplier(combatant.stat_stages.get(key, 0))
    item = held_items.get(combatant.held_item)
    if item and item["effect"] == "choice" and item.get("stat") == key:
        value *= float(item.get("multiplier", 1.5))
    if key == "spe" and combatant.status == "paralysis":
        value *= 0.5
    ctx = emit(Event.EFFECTIVE_STAT, combatant, stat=key)
    if key == "atk" and combatant.status == "burn" and "ignore_burn" not in ctx.flags:
        value *= 0.5
    return value * ctx.value


def _item_damage_multiplier(attacker: Combatant, move: dict[str, Any]) -> float:
    item = held_items.get(attacker.held_item)
    if not item:
        return 1.0
    if item["effect"] == "type_boost" and item.get("type") == move["type"]:
        return float(item.get("multiplier", 1.2))
    if item["effect"] == "life_orb":
        return float(item.get("multiplier", 1.3))
    return 1.0


def _ability_damage_multiplier(attacker: Combatant, move: dict[str, Any]) -> float:
    """Damage multiplier from the attacker's ability (e.g., Blaze/Overgrow/
    Torrent/Swarm: matching type + below 1/3 HP)."""
    return emit(Event.DAMAGE_MULT, attacker, move=move).value


def _ability_defense_multiplier(
    attacker: Combatant, defender: Combatant, move: dict[str, Any], effectiveness: float
) -> float:
    """Damage multiplier from the defender's ability (e.g., Dry Skin takes
    1.25x from Fire moves)."""
    return emit(Event.DEFENSE_MULT, defender, attacker, move=move,
                effectiveness=effectiveness).value


def _crit_stage(attacker: Combatant, move: dict[str, Any]) -> int:
    stage = 1 if move.get("high_crit") else 0
    item = held_items.get(attacker.held_item)
    if item and item["effect"] == "crit_stage":
        stage += int(item.get("stages", 1))
    return min(stage, len(CRIT_CHANCE) - 1)


def calc_damage(
    attacker: Combatant, defender: Combatant, move: dict[str, Any]
) -> tuple[int, float, bool]:
    """Mainline formula with real move. Returns (damage, effectiveness, critical_hit).

    Physical: Attack vs. Defense, Special: Sp.Atk vs. Sp.Def. Includes critical
    hit (1.5x), STAB (1.5x, 2x with Adaptability), type effectiveness (including
    ability immunity like Levitate), item/ability bonuses and random factor
    0.85-1.0.
    """
    special = move.get("category") == "special"
    atk = effective_stat(attacker, "spa" if special else "atk")
    dfn = effective_stat(defender, "spd" if special else "def")

    base = (((2 * attacker.level / 5 + 2) * move["power"] * atk / dfn) / 50) + 2

    critical = random.random() < CRIT_CHANCE[_crit_stage(attacker, move)]
    if critical:
        base *= 1.5

    base *= _stab_multiplier(attacker, move)

    effectiveness = _type_effectiveness_for(move["type"], defender, move=move)
    if effectiveness == 0:
        return 0, effectiveness, False
    base *= effectiveness
    base *= _item_damage_multiplier(attacker, move)
    base *= _ability_damage_multiplier(attacker, move)
    base *= _ability_defense_multiplier(attacker, defender, move, effectiveness)
    base *= random.uniform(0.85, 1.0)

    return max(1, int(base)), effectiveness, critical


def damage(
    attacker: Combatant,
    defender: Combatant,
    move_power: int = 60,
    move_type: str | None = None,
    special: bool = False,
) -> tuple[int, float, bool]:
    """Compatibility wrapper around `calc_damage` for a generic move."""
    move = {
        "power": move_power,
        "type": move_type or attacker.types[0],
        "category": "special" if special else "physical",
    }
    return calc_damage(attacker, defender, move)


# --------------------------------------------------------------------- EXP

def exp_for_level(level: int, growth_rate: str = "medium_fast") -> int:
    factor = GROWTH_RATE_FACTOR.get(growth_rate, 1.0)
    return int(factor * level ** 3)


def exp_gain(winner: Combatant, loser: Combatant) -> int:
    """Experience for a defeated opponent (simplified mainline formula)."""
    return max(1, int(loser.species["base_exp"] * loser.level / 5))


def apply_exp(combatant: Combatant, amount: int) -> list[int]:
    """Apply EXP and return the levels gained."""
    if combatant.owned_id is None:
        combatant.exp_gained += amount
        return []

    owned = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (combatant.owned_id,))
    growth = combatant.species.get("growth_rate", "medium_fast")
    # EXP stops at the start of the max level, so nothing piles up beyond it.
    total_exp = min(owned["exp"] + amount, max(owned["exp"], exp_for_level(MAX_LEVEL, growth)))
    level = owned["level"]
    gained_levels: list[int] = []

    while level < MAX_LEVEL and total_exp >= exp_for_level(level + 1, growth):
        level += 1
        gained_levels.append(level)

    db.execute(
        "UPDATE owned_pokemon SET exp = ?, level = ? WHERE id = ?",
        (total_exp, level, combatant.owned_id),
    )
    combatant.level = level
    combatant.exp_gained += amount
    if gained_levels:
        combatant.leveled_in_battle = True
    return gained_levels


# -------------------------------------------------------------------- Catching

def dex_caught_count(user_id: str) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM dex_entry WHERE user_id = ? AND caught_count > 0",
        (user_id,),
    )
    return row["n"] if row else 0


def critical_catch_chance(user_id: str) -> float:
    """As in mainline games, depends on Pokedex progress (0 .. ~2.5%). Per user."""

    caught = dex_caught_count(user_id)
    if caught < 30:
        return 0.0
    if caught < 150:
        return 0.005
    if caught < 300:
        return 0.010
    if caught < 450:
        return 0.015
    if caught < 600:
        return 0.020
    return 0.025


def lead_level(user_id: str) -> int | None:
    """Level of the first team Pokemon (Level Ball outside of battle)."""
    row = db.query_one(
        "SELECT level FROM owned_pokemon WHERE user_id = ? AND party_slot IS NOT NULL "
        "ORDER BY party_slot LIMIT 1",
        (user_id,),
    )
    return row["level"] if row else None


def catch_context(
    wild: Combatant,
    user_id: str,
    *,
    turn: int = 0,
    first_turn: bool = False,
    player_level: int | None = None,
    biome: str | None = None,
    now: datetime | None = None,
) -> CatchContext:
    """Catch context for `user_id`; fills in user-related information
    (already caught? Level of first team Pokemon, if not given)."""
    if player_level is None:
        player_level = lead_level(user_id)
    return CatchContext(
        turn=turn, first_turn=first_turn, player_level=player_level,
        species_caught=species_is_known(wild.species["id"], user_id),
        biome=biome, now=now,
    )


def catch_probability(
    wild: Combatant,
    ball_key: str = "poke_ball",
    status: str | None = None,
    context: CatchContext | None = None,
) -> float:
    """Probability of a successful catch (0..1)."""

    catch_rate = wild.species["catch_rate"]
    status_bonus = STATUS_BONUS.get(status or wild.status, 1.0)

    a = (
        (3 * wild.max_hp - 2 * wild.current_hp)
        * catch_rate
        * ball_multiplier(ball_key, wild, context)
        / (3 * wild.max_hp)
    ) * status_bonus

    if a >= 255:
        return 1.0
    shake = 65536 / ((255 / a) ** 0.1875)
    return min(1.0, (shake / 65536) ** 4)


@dataclass
class CatchResult:
    caught: bool
    critical: bool
    shakes: int
    probability: float
    reason: str | None = None


def throw_ball(
    wild: Combatant,
    user_id: str,
    ball_key: str = "poke_ball",
    status: str | None = None,
    context: CatchContext | None = None,
) -> CatchResult:
    """One catch attempt. Costs a ball (charged by the caller).

    `user_id` determines the chance of a critical catch (depends on Pokedex
    progress of THIS user) - the catch itself goes to the caller, regardless
    of who originally saw the encounter. `context` controls special balls
    (without: neutral context).
    """
    if wild.leveled_in_battle:
        return CatchResult(
            caught=False, critical=False, shakes=0, probability=0.0,
            reason=i18n.t(db.user_language(user_id), "battle.error.wild_cant_be_caught"),
        )

    probability = catch_probability(wild, ball_key, status, context)
    critical = random.random() < critical_catch_chance(user_id)

    if critical:
        # Critical catch: only one shake, but with higher chance.
        success = random.random() < min(1.0, probability ** 0.25)
        return CatchResult(caught=success, critical=True, shakes=1 if success else 0,
                           probability=probability)

    per_shake = probability ** 0.25
    shakes = 0
    for _ in range(4):
        if random.random() >= per_shake:
            break
        shakes += 1
    return CatchResult(caught=shakes == 4, critical=False, shakes=shakes,
                       probability=probability)


# ------------------------------------------------------------ Battle Engine
# Turn-based battle shared between manual battle (`battle_session.py`)
# and auto-battle (`auto_battle` below) - ensuring both follow the same rules.
# The engine itself has no database access except through `on_battle_won`/`save_team`;
# state lives in the `Battle` object.

@dataclass
class Battle:
    wild: Combatant
    team: list[Combatant]
    active: int = 0                 # Index in `team`
    turn: int = 0
    log: list[str] = field(default_factory=list)
    participants: set[int] = field(default_factory=set)  # Indices that have fought
    needs_switch: bool = False      # Active Pokemon defeated -> forced switch
    # Shift style (trainer/PvP battles, see db.BATTLE_STYLES): after a foe
    # faints and the next one comes out, the player may switch for free once
    # (`shift_offer`); it ends with the next turn. `shift_mode` off = set style.
    shift_mode: bool = False
    shift_offer: bool = False
    fought: bool = False            # Player has used at least one move
    outcome: str | None = None      # None | won | lost | caught | ran | fled (roamer escaped)
    # Mega Evolution (see mega.py): exactly once per side per battle, as in
    # the main games. Applies to the whole side, not per Pokémon.
    mega_used: bool = False
    foe_mega_used: bool = False
    # Trainer battle: not yet used opposing Pokemon (next first) and per
    # defeated opponent Pokemon, who fought against it.
    bench: list[Combatant] = field(default_factory=list)
    knockouts: list[dict[str, Any]] = field(default_factory=list)
    # Language for this instance's battle log - set by caller at creation from
    # user preference (see db.user_language). Lives only here, not in
    # owned_pokemon etc. - a language change mid-battle only affects the next
    # NEW battle.
    lang: str = "en"
    # EXP breakdown per team member of the LAST settled battle (see
    # grant_exp/award_battle_exp) - for the battle history. Filled in the same
    # request and written away, therefore intentionally not in the saved session
    # state.
    exp_details: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.participants.add(self.active)

    @property
    def active_mon(self) -> Combatant:
        return self.team[self.active]

    def alive_indexes(self) -> list[int]:
        return [idx for idx, member in enumerate(self.team) if not member.fainted]


def possessive(name: str, lang: str = "en") -> str:
    if lang == "de":
        return f"{name}'" if name[-1:].lower() in ("s", "x", "z", "ß") else f"{name}s"
    return f"{name}'s"


def who(combatant: Combatant, lang: str = "en") -> str:
    if combatant.trainer:
        return f"{possessive(combatant.trainer, lang)} {combatant.name}"
    key = "battle.log.who_wild" if combatant.is_wild else "battle.log.who_yours"
    return i18n.t(lang, key, name=combatant.name)


def move_label(move: dict[str, Any], lang: str = "en") -> str:
    return (move.get("name_de") if lang == "de" else None) or move["name"]


def available_moves(combatant: Combatant) -> list[str]:
    """Usable moves - with choice item only the locked one."""
    if combatant.choice_lock and held_items.effect_of(combatant.held_item) == "choice":
        return [combatant.choice_lock]
    return list(combatant.moves) or ["tackle"]


STATUS_MOVE_AI_VALUE = 45.0  # Utility value of pure status moves for AI selection below.


def expected_damage(attacker: Combatant, defender: Combatant, move_key: str) -> float:
    """Deterministic damage expectation (for AI, without randomness/critical hits).

    For self-healing (e.g., Rest) returns the expected healed HP amount (0 if
    already full HP) - the same "currency" as damage, so AI weighs healing
    realistically against attacks. For stat-changing moves, the utility value
    (STATUS_MOVE_AI_VALUE) shrinks as the targeted stat approaches its cap, so
    the AI sets up briefly rather than spending the whole fight on one stat
    move (see the "headroom" factor below). For other pure status moves (no
    damage), returns instead a fixed utility value - 0 if the status would be
    ineffective (already affected or immune), else a moderate value. AI thus
    prefers damage mostly, but falls back to status/healing if its attacks
    are otherwise weak or it's badly hurt.
    """
    move = moves_mod.get(move_key)
    if move["power"] is None:
        if defender.fainted:
            return 0.0
        heal_fraction = move.get("heal")
        if heal_fraction:
            missing = attacker.max_hp - attacker.current_hp
            if missing <= 0:
                return 0.0
            accuracy = move["accuracy"] if move["accuracy"] is not None else 100
            return min(missing, attacker.max_hp * float(heal_fraction)) * accuracy / 100
        stat_changes = move.get("stat_changes")
        if stat_changes:
            targets_foe = any(change.get("target") != "self" for change in stat_changes)
            if targets_foe and _type_effectiveness_for(move["type"], defender) == 0:
                return 0.0
            useful = False
            headroom = 0.0
            for change in stat_changes:
                target = attacker if change.get("target") == "self" else defender
                current = target.stat_stages.get(change["stat"], 0)
                stages = change["stages"]
                if (stages > 0 and current < STAT_STAGE_MAX) or (stages < 0 and current > STAT_STAGE_MIN):
                    useful = True
                    remaining = STAT_STAGE_MAX - current if stages > 0 else current - STAT_STAGE_MIN
                    headroom = max(headroom, remaining / STAT_STAGE_MAX)
            if not useful:
                return 0.0
            accuracy = move["accuracy"] if move["accuracy"] is not None else 100
            # Diminishing returns as the targeted stat nears its cap: a fresh
            # +2/-2 is worth the full value, but stacking the same move again
            # on an already-boosted stat is worth less, so the AI sets up a
            # turn or two and then switches to attacking instead of spending
            # the whole battle maxing out a single stat change.
            return STATUS_MOVE_AI_VALUE * headroom * accuracy / 100
        if move.get("reset_stats"):
            # Haze: only worth it when it wipes more boosts off the foe than
            # off the user (i.e. the net effect favours the attacker).
            net_gain = sum(defender.stat_stages.values()) - sum(attacker.stat_stages.values())
            return STATUS_MOVE_AI_VALUE if net_gain > 0 else 0.0
        if not move.get("status"):
            return 0.0
        if defender.status is not None:
            return 0.0
        if STATUS_IMMUNE_TYPES.get(move["status"], set()) & set(defender.types):
            return 0.0
        if _ability_blocks_status(defender, move["status"]):
            return 0.0
        if _type_effectiveness_for(move["type"], defender) == 0:
            return 0.0
        accuracy = move["accuracy"] if move["accuracy"] is not None else 100
        return STATUS_MOVE_AI_VALUE * accuracy / 100
    special = move["category"] == "special"
    atk = effective_stat(attacker, "spa" if special else "atk")
    dfn = effective_stat(defender, "spd" if special else "def")
    base = (((2 * attacker.level / 5 + 2) * move["power"] * atk / dfn) / 50) + 2
    base *= _stab_multiplier(attacker, move)
    effectiveness = _type_effectiveness_for(move["type"], defender, move=move)
    base *= effectiveness
    base *= _item_damage_multiplier(attacker, move)
    base *= _ability_damage_multiplier(attacker, move)
    base *= _ability_defense_multiplier(attacker, defender, move, effectiveness)
    base *= 0.925
    accuracy = move["accuracy"] if move["accuracy"] is not None else 100
    expected = min(base, defender.current_hp) * accuracy / 100
    if move.get("recoil"):
        expected *= 0.95
    if move.get("drain"):
        expected *= 1.05
    return expected


def ai_choose_move(attacker: Combatant, defender: Combatant) -> str:
    """AI: the move with the highest expected damage. In case of a tie (e.g.
    both deal enough for a KO) the one with higher priority wins."""
    options = available_moves(attacker)
    return max(
        options,
        key=lambda key: (
            round(expected_damage(attacker, defender, key), 3),
            moves_mod.get(key).get("priority", 0),
            -options.index(key),
        ),
    )


def wild_choose_move(wild: Combatant, target: Combatant) -> str:
    """Wild Pokemon mostly play optimally, but not always (25% random).
    Trainer Pokemon always play optimally."""
    options = available_moves(wild)
    if not wild.trainer and len(options) > 1 and random.random() < 0.25:
        return random.choice(options)
    return ai_choose_move(wild, target)


def _heal(combatant: Combatant, amount: int) -> int:
    if combatant.fainted or amount <= 0:
        return 0
    before = combatant.current_hp
    combatant.current_hp = min(combatant.max_hp, combatant.current_hp + amount)
    return combatant.current_hp - before


def _hurt(combatant: Combatant, amount: int) -> None:
    combatant.current_hp = max(0, combatant.current_hp - max(0, amount))


def _consume_item(combatant: Combatant) -> None:
    combatant.consumed_item = combatant.held_item
    combatant.held_item = None


def _check_berry(b: Battle, combatant: Combatant) -> None:
    item = held_items.get(combatant.held_item)
    if not item or item["effect"] != "berry_heal" or combatant.fainted:
        return
    if combatant.current_hp > combatant.max_hp * float(item.get("threshold", 0.5)):
        return
    amount = int(item["amount"]) if item.get("amount") else max(
        1, int(combatant.max_hp * float(item.get("fraction", 0.25)))
    )
    healed = _heal(combatant, amount)
    item_label = held_items.label(item["key"], b.lang)
    _consume_item(combatant)
    b.log.append(i18n.t(b.lang, "battle.log.eats_berry", who=who(combatant, b.lang),
                         item=item_label, amount=healed))


def inflict_status(b: Battle, target: Combatant, status: str) -> bool:
    """Attempts to inflict `target` with a status. Only one at a time (like in
    the main games) - also fails if the type is naturally immune (see
    STATUS_IMMUNE_TYPES) or an ability (immunity, flexibility, insomnia, water
    absorb, heat proof, ...) blocks exactly this status."""
    if target.fainted or target.status is not None:
        return False
    if STATUS_IMMUNE_TYPES.get(status, set()) & set(target.types):
        b.log.append(i18n.t(b.lang, "battle.log.status_immune", who=who(target, b.lang)))
        return False
    if _ability_blocks_status(target, status, b):
        return False
    target.status = status
    target.status_counter = random.randint(1, 3) if status == "sleep" else 0
    b.log.append(i18n.t(b.lang, "battle.log.status_inflicted",
                         who=who(target, b.lang), status=status_verb(status, b.lang)))
    return True


def stat_name(stat: str, lang: str) -> str:
    return i18n.t(lang, f"battle.stat_name.{stat}")


# Log text depends on the size of the change, like in the main games:
# 1 stage = normal, 2 = "sharply", 3+ = "drastically".
_STAT_CHANGE_LOG_KEYS = {
    1: ("battle.log.stat_rose", "battle.log.stat_fell"),
    2: ("battle.log.stat_rose_sharply", "battle.log.stat_fell_harshly"),
    3: ("battle.log.stat_rose_drastically", "battle.log.stat_fell_severely"),
}


def apply_stat_change(
    b: Battle, target: Combatant, stat: str, stages: int,
    source: Combatant | None = None, flags: tuple[str, ...] = (),
) -> bool:
    """Changes a stat stage of `target` by `stages` (capped at -6..+6).

    `source` is the Pokemon causing the change. A drop caused by a FOE (source
    is not the target) can be blocked by the target's ability (Clear Body,
    Hyper Cutter, ...; `flags` like "intimidate" let abilities react to the
    kind of drop) and, once applied, triggers reactions (Defiant, ...).
    Returns False if nothing changed (blocked, or the stage was already at the
    limit - both logged), else True."""
    if target.fainted or stages == 0:
        return False
    by_foe = stages < 0 and source is not None and source is not target
    if by_foe and emit(Event.BEFORE_STAT_CHANGE, target, source, battle=b, stat=stat,
                       value=stages, flags=set(flags)).cancelled:
        return False
    current = target.stat_stages.get(stat, 0)
    new = max(STAT_STAGE_MIN, min(STAT_STAGE_MAX, current + stages))
    if new == current:
        key = "battle.log.stat_wont_go_higher" if stages > 0 else "battle.log.stat_wont_go_lower"
        b.log.append(i18n.t(b.lang, key, who=who(target, b.lang), stat=stat_name(stat, b.lang)))
        return False
    target.stat_stages[stat] = new
    magnitude = min(abs(stages), 3)
    log_key = _STAT_CHANGE_LOG_KEYS[magnitude][0 if stages > 0 else 1]
    b.log.append(i18n.t(b.lang, log_key, who=who(target, b.lang), stat=stat_name(stat, b.lang)))
    if by_foe:
        emit(Event.STAT_LOWERED, target, source, battle=b, stat=stat)
    return True


def _status_prevents_action(b: Battle, c: Combatant) -> bool:
    """Checks BEFORE the move if c cannot act at all this turn due to status.
    Always logs itself - returns True if the turn is over for c."""
    if c.status == "paralysis" and random.random() < 0.25:
        b.log.append(i18n.t(b.lang, "battle.log.paralyzed_cant_move", who=who(c, b.lang)))
        return True
    if c.status == "freeze":
        if random.random() < 0.2:
            c.status = None
            b.log.append(i18n.t(b.lang, "battle.log.thaws_out", who=who(c, b.lang)))
            return False
        b.log.append(i18n.t(b.lang, "battle.log.frozen_cant_move", who=who(c, b.lang)))
        return True
    if c.status == "sleep":
        if c.status_counter <= 0:
            c.status = None
            b.log.append(i18n.t(b.lang, "battle.log.wakes_up", who=who(c, b.lang)))
            return False
        c.status_counter -= 1
        b.log.append(i18n.t(b.lang, "battle.log.fast_asleep", who=who(c, b.lang)))
        return True
    return False


def _tick_status(b: Battle, c: Combatant) -> None:
    """Damage from poison/burn/curse at end of turn. Faint message and battle
    outcome are handled by `_check_faints` after - only damage here."""
    if c.fainted or c.status not in ("poison", "toxic", "burn", "curse"):
        return
    if c.status == "toxic":
        c.status_counter = min(c.status_counter + 1, TOXIC_MAX_TICKS)
        fraction = c.status_counter / 16
        key = "battle.log.poison_damage"
    elif c.status == "poison":
        fraction = STATUS_TICK_FRACTION["poison"]
        key = "battle.log.poison_damage"
    elif c.status == "curse":
        fraction = STATUS_TICK_FRACTION["curse"]
        key = "battle.log.curse_damage"
    else:
        fraction = STATUS_TICK_FRACTION["burn"]
        key = "battle.log.burn_damage"
    dealt = max(1, int(c.max_hp * fraction))
    _hurt(c, dealt)
    b.log.append(i18n.t(b.lang, key, who=who(c, b.lang), amount=dealt))


def player_goes_first(b: Battle, player_move: str, wild_move: str) -> bool:
    player, wild = b.active_mon, b.wild
    player_prio = moves_mod.get(player_move).get("priority", 0)
    wild_prio = moves_mod.get(wild_move).get("priority", 0)
    if player_prio != wild_prio:
        return player_prio > wild_prio

    def quick_claw(combatant: Combatant) -> bool:
        item = held_items.get(combatant.held_item)
        return bool(item and item["effect"] == "quick_claw"
                    and random.random() < float(item.get("chance", 0.2)))

    player_claw, wild_claw = quick_claw(player), quick_claw(wild)
    if player_claw != wild_claw:
        lucky = player if player_claw else wild
        b.log.append(i18n.t(b.lang, "battle.log.quick_claw", who=who(lucky, b.lang)))
        return player_claw

    player_speed, wild_speed = effective_stat(player, "spe"), effective_stat(wild, "spe")
    if player_speed != wild_speed:
        return player_speed > wild_speed
    return random.random() < 0.5


def _move_had_no_effect(b: Battle, attacker: Combatant, defender: Combatant,
                        move: dict[str, Any]) -> None:
    """The move doesn't affect the defender (type chart or ability immunity).
    An absorbing ability (Water Absorb, Flash Fire, ...) reacts instead of
    the generic "no effect" line."""
    ctx = emit(Event.MOVE_IMMUNITY, defender, attacker, battle=b, move=move,
               move_type=move["type"])
    if "absorbed" not in ctx.flags:
        b.log.append(i18n.t(b.lang, "battle.log.no_effect", who=who(defender, b.lang)))


def use_move(b: Battle, attacker: Combatant, defender: Combatant, move_key: str) -> None:
    move = moves_mod.get(move_key)
    b.log.append(i18n.t(b.lang, "battle.log.use_move",
                         who=who(attacker, b.lang), move=move_label(move, b.lang)))

    if held_items.effect_of(attacker.held_item) == "choice" and not attacker.choice_lock:
        attacker.choice_lock = move_key

    accuracy = move["accuracy"]
    if accuracy is not None and random.random() * 100 >= accuracy:
        b.log.append(i18n.t(b.lang, "battle.log.move_missed"))
        return

    if move["power"] is None:
        # Pure status move: self-healing (e.g. Rest/Recover), stat change
        # (e.g. Swords Dance, Growl) or classic status (e.g. Thunder Wave) -
        # never more than one. Type immunity still applies (e.g. Ground against
        # Electric moves, or Levitate against Ground moves) - except for
        # self-targeting moves that have no target in the type chart sense.
        if move.get("splash"):
            b.log.append(i18n.t(b.lang, "battle.log.splash", who=who(attacker, b.lang)))
            return
        heal_fraction = move.get("heal")
        if heal_fraction:
            healed = _heal(attacker, max(1, int(attacker.max_hp * float(heal_fraction))))
            if healed:
                b.log.append(i18n.t(b.lang, "battle.log.heals_self",
                                     who=who(attacker, b.lang), amount=healed))
            else:
                b.log.append(i18n.t(b.lang, "battle.log.heal_failed", who=who(attacker, b.lang)))
            return
        stat_changes = move.get("stat_changes")
        if stat_changes:
            targets_foe = any(change.get("target") != "self" for change in stat_changes)
            if targets_foe and _type_effectiveness_for(move["type"], defender) == 0:
                _move_had_no_effect(b, attacker, defender, move)
                return
            for change in stat_changes:
                target = attacker if change.get("target") == "self" else defender
                apply_stat_change(b, target, change["stat"], change["stages"], source=attacker)
            return
        if move.get("reset_stats"):
            attacker.stat_stages = {k: 0 for k in STAT_STAGE_KEYS}
            defender.stat_stages = {k: 0 for k in STAT_STAGE_KEYS}
            b.log.append(i18n.t(b.lang, "battle.log.stats_reset"))
            return
        if _type_effectiveness_for(move["type"], defender) == 0:
            _move_had_no_effect(b, attacker, defender, move)
            return
        if move.get("trap"):
            # Mean Look / Spider Web: a roamer can't run away any more (see
            # _roamer_escapes). Nothing else to hold - no effect on the rest.
            if defender.roams:
                defender.trapped = True
                b.log.append(i18n.t(b.lang, "battle.log.cant_escape", who=who(defender, b.lang)))
            else:
                b.log.append(i18n.t(b.lang, "battle.log.no_effect", who=who(defender, b.lang)))
            return
        if move.get("curse"):
            # Curse: user gives up half its own max HP to bind the target in
            # a curse that deals 1/4 max HP each turn (see _tick_status) -
            # unlike the other status moves here, this one hurts the user too.
            self_damage = max(1, attacker.max_hp // 2)
            _hurt(attacker, self_damage)
            b.log.append(i18n.t(b.lang, "battle.log.recoil", who=who(attacker, b.lang)))
            if attacker.fainted:
                return
        inflict_status(b, defender, move["status"])
        return

    dealt, effectiveness, critical = calc_damage(attacker, defender, move)
    if effectiveness == 0:
        _move_had_no_effect(b, attacker, defender, move)
        return

    # Sturdy & co.: the defender's ability may cap the damage (a capped hit
    # is announced after the HP loss below).
    endure = emit(Event.BEFORE_DAMAGE, defender, attacker, battle=b, move=move, value=dealt)
    dealt = int(endure.value)
    sash = False
    if (dealt >= defender.current_hp and defender.current_hp == defender.max_hp
            and defender.current_hp > 1
            and held_items.effect_of(defender.held_item) == "focus_sash"):
        dealt = defender.current_hp - 1
        sash = True
    dealt = min(dealt, defender.current_hp)
    _hurt(defender, dealt)

    if move["type"] == "fire" and defender.status == "freeze" and dealt > 0:
        defender.status = None
        b.log.append(i18n.t(b.lang, "battle.log.thaws_from_heat", who=who(defender, b.lang)))

    if critical:
        b.log.append(i18n.t(b.lang, "battle.log.critical_hit"))
    if effectiveness > 1:
        b.log.append(i18n.t(b.lang, "battle.log.super_effective"))
    elif effectiveness < 1:
        b.log.append(i18n.t(b.lang, "battle.log.not_very_effective"))
    b.log.append(i18n.t(b.lang, "battle.log.loses_hp", who=who(defender, b.lang), amount=dealt))
    if "endured" in endure.flags:
        b.log.append(i18n.t(b.lang, "battle.log.ability_endured", who=who(defender, b.lang),
                             ability=abilities.label(defender.ability, b.lang)))
    if sash:
        _consume_item(defender)
        b.log.append(i18n.t(b.lang, "battle.log.focus_sash", who=who(defender, b.lang)))

    if dealt > 0:
        if move.get("drain"):
            if _heal(attacker, max(1, int(dealt * float(move["drain"])))):
                b.log.append(i18n.t(b.lang, "battle.log.drained", who=who(defender, b.lang)))
        if move.get("recoil"):
            _hurt(attacker, max(1, int(dealt * float(move["recoil"]))))
            b.log.append(i18n.t(b.lang, "battle.log.recoil", who=who(attacker, b.lang)))
        item = held_items.get(attacker.held_item)
        if item and item["effect"] == "life_orb":
            _hurt(attacker, max(1, int(attacker.max_hp * float(item.get("recoil", 0.1)))))
            b.log.append(i18n.t(b.lang, "battle.log.life_orb_recoil", who=who(attacker, b.lang)))
        if item and item["effect"] == "shell_bell":
            if _heal(attacker, max(1, int(dealt * float(item.get("fraction", 0.125))))):
                b.log.append(i18n.t(b.lang, "battle.log.shell_bell_heal", who=who(attacker, b.lang)))
        emit(Event.DAMAGE_TAKEN, defender, attacker, battle=b, move=move, dealt=dealt)

    status = move.get("status")
    if status and dealt > 0 and not defender.fainted:
        if random.random() * 100 < move.get("status_chance", 100):
            inflict_status(b, defender, status)

    stat_changes = move.get("stat_changes")
    if stat_changes and dealt > 0:
        if random.random() * 100 < move.get("stat_chance", 100):
            for change in stat_changes:
                target = attacker if change.get("target") == "self" else defender
                if not target.fainted:
                    apply_stat_change(b, target, change["stat"], change["stages"], source=attacker)

    flinch_chance = move.get("flinch_chance")
    if flinch_chance and dealt > 0 and not defender.fainted:
        if random.random() * 100 < flinch_chance:
            if not emit(Event.BEFORE_FLINCH, defender, attacker, battle=b).cancelled:
                defender.flinched = True

    _check_berry(b, defender)
    _check_berry(b, attacker)


def _try_ability_cure_status(b: Battle, c: Combatant) -> None:
    """End-of-turn ability effects (e.g., Shed Skin heals its own status)."""
    emit(Event.END_OF_TURN, c, battle=b)


def end_of_turn(b: Battle) -> None:
    for combatant in (b.active_mon, b.wild):
        _tick_status(b, combatant)
    for combatant in (b.active_mon, b.wild):
        item = held_items.get(combatant.held_item)
        if (item and item["effect"] == "end_turn_heal" and not combatant.fainted
                and combatant.current_hp < combatant.max_hp):
            _heal(combatant, max(1, int(combatant.max_hp * float(item.get("fraction", 0.0625)))))
            b.log.append(i18n.t(b.lang, "battle.log.end_turn_item_heal",
                                 who=who(combatant, b.lang), item=held_items.label(item["key"], b.lang)))
        _check_berry(b, combatant)
        _try_ability_cure_status(b, combatant)


def wild_gain_exp(b: Battle, loser: Combatant) -> None:
    """When the wild Pokémon defeats a team member, it gains EXP itself.
    If it levels up in the process, it can no longer be caught IN THIS BATTLE."""
    wild = b.wild
    growth = wild.species.get("growth_rate", "medium_fast")
    if not wild.exp:
        wild.exp = exp_for_level(wild.level, growth)
    wild.exp += exp_gain(wild, loser)
    leveled = False
    while wild.level < MAX_LEVEL and wild.exp >= exp_for_level(wild.level + 1, growth):
        wild.level += 1
        leveled = True
    if leveled:
        wild.refresh_after_level()
        wild.leveled_in_battle = True
        b.log.append(i18n.t(b.lang, "battle.log.wild_levels_up", who=who(wild, b.lang), level=wild.level))


def _check_faints(b: Battle) -> None:
    player, wild = b.active_mon, b.wild
    if wild.fainted:
        b.log.append(i18n.t(b.lang, "battle.log.wild_fainted", who=who(wild, b.lang)))
        if wild.trainer:
            # `ev_participants`: who is still standing NOW - a Pokemon that
            # beat this foe and faints later still keeps its EVs (see evs.py).
            b.knockouts.append({"level": wild.level, "species_id": wild.species["id"],
                                "participants": sorted(b.participants),
                                "ev_participants": sorted(
                                    idx for idx in b.participants if not b.team[idx].fainted)})
        if b.bench:
            b.wild = b.bench.pop(0)
            b.participants = set() if player.fainted else {b.active}
            b.log.append(i18n.t(b.lang, "battle.log.trainer_sends_out",
                                 trainer=wild.trainer, name=b.wild.name, level=b.wild.level))
            enter_field(b, b.wild)
            if b.shift_mode and not player.fainted and len(b.alive_indexes()) > 1:
                b.shift_offer = True
                b.log.append(i18n.t(b.lang, "battle.log.shift_offer"))
        else:
            b.outcome = "won"
    if player.fainted:
        b.log.append(i18n.t(b.lang, "battle.log.player_fainted", who=who(player, b.lang)))
        if not wild.fainted and wild.is_wild:
            wild_gain_exp(b, player)
        if b.outcome is None:
            if wild.is_wild:
                # Unlike the mainline games: a wild encounter ends the moment
                # your active Pokemon faints, with no forced switch to the
                # next team member - the wild Pokemon flees right away.
                b.outcome = "lost"
                b.log.append(i18n.t(b.lang, "battle.log.wild_flees_on_faint", who=who(wild, b.lang)))
            elif b.alive_indexes():
                b.needs_switch = True
            else:
                b.outcome = "lost"
                b.log.append(i18n.t(b.lang, "battle.log.team_wiped"))


def _roamer_escapes(b: Battle, c: Combatant) -> bool:
    """A roaming wild Pokemon that gets its turn runs away at once (see
    roaming.py) - unless a trap holds it. Sleep, paralysis and freezing were
    checked before this, so a Hypnotized roamer simply loses its turns."""
    if not c.roams or c.trapped:
        return False
    b.log.append(i18n.t(b.lang, "battle.log.roamer_escaped", who=who(c, b.lang)))
    b.outcome = "fled"
    return True


def run_turn(b: Battle, player_move: str | None) -> None:
    """One turn. `player_move=None` means: the player has already used their
    turn otherwise (switch, ball) - only the wild Pokemon acts."""
    if b.outcome is not None:
        return
    if b.needs_switch:
        raise ValueError(i18n.t(b.lang, "battle.error.must_switch"))
    player, wild = b.active_mon, b.wild
    if player_move is not None and player_move not in available_moves(player):
        raise ValueError(i18n.t(b.lang, "battle.error.move_unavailable"))
    b.shift_offer = False  # the free switch is over once the turn starts

    # As in the games, before the attacks: the foe Mega Evolves if it can
    # (the player decides for themselves, see mega_evolve).
    ai_mega_evolve(b)
    b.turn += 1
    wild_move = wild_choose_move(wild, player)
    if player_move is None:
        order = [(wild, player, wild_move)]
    else:
        b.fought = True
        order = [(player, wild, player_move), (wild, player, wild_move)]
        if not player_goes_first(b, player_move, wild_move):
            order.reverse()

    for attacker, defender, move_key in order:
        if attacker.fainted or defender.fainted:
            continue
        if attacker.flinched:
            attacker.flinched = False
            b.log.append(i18n.t(b.lang, "battle.log.flinched", who=who(attacker, b.lang)))
            continue
        if _status_prevents_action(b, attacker):
            continue
        if _roamer_escapes(b, attacker):
            break
        use_move(b, attacker, defender, move_key)
    # Flinch only applies until the end of THIS turn (see Combatant.flinched) -
    # regardless of whether it was already checked above (e.g. if the holder
    # was defeated earlier), never carry into the next turn.
    player.flinched = wild.flinched = False
    if b.outcome != "fled" and not (player.fainted and wild.fainted):
        end_of_turn(b)
    _check_faints(b)


def switch_is_free(b: Battle) -> bool:
    """A switch right now does not cost the turn (forced or shift offer)."""
    return b.needs_switch or b.shift_offer


def switch_to(b: Battle, index: int, forced: bool = False) -> None:
    """Switch. Voluntary switch costs the turn (then `run_turn(b, None)`),
    forced switch after a KO does not - nor does the shift-style switch
    offered after a foe's KO (`switch_is_free`)."""
    if b.outcome is not None:
        raise ValueError(i18n.t(b.lang, "battle.over"))
    if not 0 <= index < len(b.team):
        raise ValueError(i18n.t(b.lang, "battle.error.not_in_team"))
    if index == b.active:
        raise ValueError(i18n.t(b.lang, "battle.error.already_active"))
    if b.team[index].fainted:
        raise ValueError(i18n.t(b.lang, "battle.error.fainted_cant_switch"))
    if forced != b.needs_switch:
        raise ValueError(i18n.t(b.lang, "battle.error.no_forced_switch" if forced
                                 else "battle.error.must_switch"))
    old = b.active_mon
    old.choice_lock = None
    old.stat_stages = {k: 0 for k in STAT_STAGE_KEYS}
    old.flinched = False
    old.volatile = {}
    if not old.fainted:
        b.log.append(i18n.t(b.lang, "battle.log.recall", name=old.name))
    b.active = index
    b.participants.add(index)
    b.needs_switch = False
    b.shift_offer = False
    b.log.append(i18n.t(b.lang, "battle.log.send_out", name=b.active_mon.name))
    enter_field(b, b.active_mon)


def enter_field(b: Battle, c: Combatant) -> None:
    """`c` has just been sent out: its switch-in ability (Intimidate,
    Download, ...) takes effect against the opposing active Pokemon."""
    foe = b.wild if c is b.active_mon else b.active_mon
    emit(Event.SWITCH_IN, c, foe, battle=b)


def battle_start_effects(b: Battle) -> None:
    """Switch-in abilities of both opening Pokemon - called once after the
    opening log lines (start_log, trainer_battle.start, pvp). As in the main
    games the faster one goes first; on a speed tie, the player's."""
    first, second = b.active_mon, b.wild
    if effective_stat(second, "spe") > effective_stat(first, "spe"):
        first, second = second, first
    enter_field(b, first)
    enter_field(b, second)


# ------------------------------------------------------- Mega Evolution
# Rules and catalog: see mega.py. Only the battle part lives here: check,
# apply and (when loading a session) restore.

def mega_candidate(c: Combatant) -> dict[str, Any] | None:
    """Form this Pokémon could change into RIGHT NOW: matching stone held and
    not yet Mega Evolved. Ignores whether the side has already used its Mega
    Evolution (see can_mega_evolve)."""
    if c.mega is not None or c.fainted:
        return None
    return mega.form_for(c.species.get("id"), c.held_item)


def side_used_mega(b: Battle, c: Combatant) -> bool:
    return b.foe_mega_used if c is b.wild else b.mega_used


def can_mega_evolve(b: Battle, c: Combatant) -> bool:
    """Everything mega_evolve would check - so the view offers the button
    exactly when the action will go through."""
    return (b.outcome is None and not b.needs_switch and not side_used_mega(b, c)
            and mega_candidate(c) is not None)


def apply_mega(c: Combatant, form: dict[str, Any]) -> None:
    """Apply the form: derived species, types and ability of the Mega form.
    Status conditions, stat stages and HP stay - only the base stats change
    (and with them `stat()`)."""
    c.mega_base = {"species": c.species, "types": list(c.types), "ability": c.ability}
    c.species = mega.species_for(c.species, form)
    c.types = [t for t in (form["type1"], form.get("type2")) if t]
    if form.get("ability"):
        c.ability = form["ability"]
    c.mega = form["stone"]
    # Mega forms share the base form's base HP; should a form ever change
    # that, the difference is added like on a level-up instead of shifting HP.
    new_max = stat_hp(c.species["base_hp"], c.ivs.get("hp", 15), c.level,
                      species_id=c.species.get("id"))
    if new_max != c.max_hp:
        c.current_hp = max(1, min(new_max, c.current_hp + (new_max - c.max_hp)))
        c.max_hp = new_max


def restore_mega(c: Combatant, stone_key: str | None) -> None:
    """When loading a running session: re-apply the saved form.
    An unknown stone (catalog changed) is silently ignored - the Pokémon then
    keeps fighting in its base form."""
    form = mega.form_for(c.species.get("id"), stone_key)
    if form is not None:
        apply_mega(c, form)


def mega_evolve(b: Battle, c: Combatant) -> dict[str, Any]:
    """Trigger Mega Evolution (player action `mega` or AI). Does not cost the
    turn - as in the games, it happens before the attacks."""
    if b.outcome is not None:
        raise ValueError(i18n.t(b.lang, "battle.over"))
    if b.needs_switch:
        raise ValueError(i18n.t(b.lang, "battle.error.must_switch"))
    if side_used_mega(b, c):
        raise ValueError(i18n.t(b.lang, "battle.error.mega_already_used"))
    form = mega_candidate(c)
    if form is None:
        raise ValueError(i18n.t(b.lang, "battle.error.mega_not_possible"))
    before = who(c, b.lang)
    apply_mega(c, form)
    if c is b.wild:
        b.foe_mega_used = True
    else:
        b.mega_used = True
    key = "battle.log.primal_reverted" if form["kind"] == "primal" else "battle.log.mega_evolved"
    b.log.append(i18n.t(b.lang, key, who=before, form=mega.name(form, b.lang),
                        item=mega.stone_label(form, b.lang)))
    return form


def ai_mega_evolve(b: Battle) -> None:
    """Gegner (Arena/Liga/Spielerkopie) megaentwickelt, sobald er kann - wilde
    Pokémon tragen keine Items, betrifft also nur Trainer-Gegner."""
    if b.wild.is_wild or not can_mega_evolve(b, b.wild):
        return
    mega_evolve(b, b.wild)


def mega_view(b: Battle) -> dict[str, Any]:
    """Mega block for the battle view (button in the frontend)."""
    form = mega_candidate(b.active_mon)
    active = mega.get(b.active_mon.mega)
    return {
        "used": b.mega_used,
        "available": can_mega_evolve(b, b.active_mon),
        "option": mega.describe(form),
        "active": mega.describe(active),
    }


def describe(member: Combatant, usable: list[str] | None = None) -> dict[str, Any]:
    species = member.species
    return {
        "owned_id": member.owned_id,
        "species_id": species["id"],
        "name": member.name,
        "sprite_url": species.get("sprite_url"),
        "types": member.types,
        "level": member.level,
        "current_hp": member.current_hp,
        "max_hp": member.max_hp,
        "fainted": member.fainted,
        "status": member.status,
        "stat_stages": {k: v for k, v in member.stat_stages.items() if v},
        "held_item": member.held_item,
        "held_item_info": held_items.get(member.held_item),
        "ability": member.ability,
        "ability_info": abilities.describe(member.ability),
        # Active Mega form (None = base form) - name/sprite/types above then
        # already come from the Mega form (see mega.species_for).
        "mega": member.mega,
        "mega_info": mega.describe(mega.get(member.mega)),
        "choice_lock": member.choice_lock,
        "moves": [
            {**moves_mod.describe(key), "usable": usable is None or key in usable}
            for key in member.moves
        ],
    }


def start_log(b: Battle) -> None:
    b.log.append(i18n.t(b.lang, "battle.log.wild_appears", name=b.wild.name, level=b.wild.level))
    b.log.append(i18n.t(b.lang, "battle.log.send_out", name=b.active_mon.name))
    battle_start_effects(b)


def save_team(team: list[Combatant]) -> None:
    """Write back HP and (possibly consumed) item of team members."""
    now = datetime.now().astimezone().isoformat()
    for member in team:
        if member.owned_id is None:
            continue
        db.execute(
            "UPDATE owned_pokemon SET current_hp = ?, held_item = ?, hp_updated_at = ? "
            "WHERE id = ?",
            (member.current_hp, member.held_item, now, member.owned_id),
        )


def exp_shares(
    team: list[Combatant], loser: Combatant, participants: set[int] | list[int],
    multiplier: float = 1.0, share_all: bool = False,
) -> dict[int, int]:
    """EXP per team index for a defeated opponent: full EXP for all who fought
    against it, one fifth for the rest - or full EXP too while holding an Exp.
    Share (reward for the level-100 achievements, see achievements.py) - or
    everybody (`share_all`, the 50th level-100 achievement). Fainted
    team members also get their share. Lucky Egg x1.5."""
    if not team:
        return {}
    base = int(exp_gain(team[0], loser) * multiplier)
    shares: dict[int, int] = {}
    for idx, member in enumerate(team):
        item = held_items.get(member.held_item)
        if share_all or idx in participants or (item and item["effect"] == "exp_share"):
            amount = base
        else:
            amount = base // 5
        if item and item["effect"] == "exp_boost":
            amount = int(amount * float(item.get("multiplier", 1.5)))
        if amount > 0:
            shares[idx] = amount
    return shares


def grant_exp(
    log: list[str], team: list[Combatant], shares: dict[int, int], lang: str = "en",
    details: list[dict[str, Any]] | None = None,
) -> dict[int, int]:
    """Records EXP, logs level ups/new moves. Returns {owned_id: EXP}.

    `details` (optional) is additionally filled with a breakdown per team
    member - name, EXP, level before/after and number of level ups. This goes
    into the battle history (`battle_history.py`); the concise {owned_id: EXP}
    remains the return value so existing callers work unchanged.

    Level evolutions intentionally do NOT happen here - there's only a note in
    the log; evolution happens in the Pokemon menu (see evolution.py)."""
    from . import evolution  # evolution importiert battle

    exp_by_owned: dict[int, int] = {}
    for idx, amount in shares.items():
        member = team[idx]
        level_before = member.level
        levels = apply_exp(member, amount)
        if member.owned_id is not None:
            exp_by_owned[member.owned_id] = amount
        if details is not None:
            details.append({
                "owned_id": member.owned_id,
                # Write both name fields: the history will be read later in the
                # then-set language (speciesName in frontend), not in the battle's.
                "species_id": member.species.get("id"),
                "name_en": member.species.get("name_en") or member.species.get("name"),
                "name_de": member.species.get("name_de"),
                "sprite_url": member.species.get("sprite_url"),
                "exp": amount,
                "level_from": level_before,
                "level_to": member.level,
                "level_ups": len(levels),
            })
        log.append(i18n.t(lang, "battle.log.exp_gained", name=member.name, amount=amount))
        if levels:
            log.append(i18n.t(lang, "battle.log.level_up", name=member.name, level=levels[-1]))
            for move_key in member.refresh_after_level():
                log.append(i18n.t(lang, "battle.log.move_learned", name=member.name,
                                   move=move_label(moves_mod.get(move_key), lang)))
            if member.owned_id is not None:
                row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (member.owned_id,))
                if row is not None and evolution.level_evolutions(row):
                    log.append(i18n.t(lang, "battle.log.can_evolve", name=member.name))
    return exp_by_owned


def award_battle_exp(b: Battle, user_id: str | None = None) -> dict[int, int]:
    """EXP distribution after a won OR successfully completed (caught) battle -
    in the main games a catch gives as much EXP as a win. Distribution see
    `exp_shares`. Level ups increase HP and can unlock new moves."""
    from . import achievements  # lazy: circular import, see on_battle_won

    b.exp_details = []
    shares = exp_shares(b.team, b.wild, b.participants,
                        share_all=achievements.share_all_exp(user_id))
    return grant_exp(b.log, b.team, shares, b.lang, details=b.exp_details)


def on_battle_won(
    user_id: str, b: Battle, encounter_id: int | None = None
) -> dict[str, Any]:
    """ONLY entry point for a won battle (manual AND auto-battle).

    Further win rewards (e.g. money) belong here - the return value `reward` is
    passed by both callers to the frontend. The caller sets the encounter status.
    """
    from . import evs  # evs importiert battle
    from . import achievements  # lazy: rewards -> economy -> battle would else be circular

    wild = b.wild
    exp_by_owned = award_battle_exp(b, user_id)
    # EVs only for a defeated opponent, not for a catch (`award_battle_exp` is
    # shared with the catch path).
    # The battle ends with the KO, so "standing now" is "standing at the KO".
    standing = {idx for idx in b.participants if not b.team[idx].fainted}
    ev_gains = evs.grant_battle_evs(b.log, b.team, [(wild.base_species, standing)], b.lang)
    new_achievements = achievements.check_level100(user_id, b.exp_details, b.lang)
    for ach in new_achievements:
        b.log.append(i18n.t(b.lang, "battle.log.achievement_unlocked", title=ach["title"]))
        if ach.get("reward"):
            b.log.append(i18n.t(b.lang, "battle.log.achievement_reward", label=ach["reward"]["label"]))

    reward = None
    if encounter_id is not None:
        from . import economy  # economy importiert battle

        row = db.query_one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))
        if row is not None:
            multiplier = 2 if held_items.any_holds(b.team, "amulet_coin") else 1
            earned = economy.award_battle_win(user_id, {**row, "level": wild.level}, multiplier=multiplier)
            reward = {"money_earned": earned, "money": economy.balance(user_id)}
            b.log.append(i18n.t(b.lang, "battle.log.money_earned", amount=earned))
    return {"exp": exp_by_owned, "reward": reward, "evs": ev_gains, "achievements": new_achievements}


# ------------------------------------------------------------- Auto-battle

def party(user_id: str) -> list[dict[str, Any]]:
    # HP regeneration lazily applied when reading (see healing.py).
    from . import healing
    healing.apply_regen(user_id)
    return db.query(
        "SELECT * FROM owned_pokemon WHERE user_id = ? AND party_slot IS NOT NULL "
        "ORDER BY party_slot",
        (user_id,),
    )


def owned_selection(user_id: str, ids: list[int]) -> list[dict[str, Any]]:
    """Specific owned Pokemon in the given id order - not necessarily the
    party. Lets a one-off battle (daily challenge, see challenge.py) field a
    lineup for just that fight without writing it to party_slot, so the real
    team stays untouched once the fight ends. Duplicate ids and ids that
    aren't this user's own Pokemon are silently dropped."""
    from . import healing
    healing.apply_regen(user_id)
    ids = list(dict.fromkeys(ids))
    if not ids:
        return []
    rows = {
        row["id"]: row for row in db.query(
            f"SELECT * FROM owned_pokemon WHERE user_id = ? AND id IN ({','.join('?' * len(ids))})",
            (user_id, *ids),
        )
    }
    return [rows[i] for i in ids if i in rows]


def species_is_known(species_id: int, user_id: str) -> bool:
    row = db.query_one(
        "SELECT caught_count FROM dex_entry WHERE species_id = ? AND user_id = ?",
        (species_id, user_id),
    )
    return bool(row and row["caught_count"] > 0)


def auto_battle(
    encounter: dict[str, Any], user_id: str, max_turns: int = 60,
    owned: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Runs a battle without user input, with the team of `user_id`.

    Uses the same engine as manual battle; AI chooses the move with the
    highest expected damage each time and switches the next fit team member in
    after a KO. `owned`: the fighters in send-out order (an auto rule's team
    choice, see auto_rules.py); default is the party. Auto-battle does not
    throw balls - catching is an auto-rule action (auto_rules.py).
    """
    lang = db.user_language(user_id)
    wild = combatant_from_encounter(encounter)
    team = [combatant_from_owned(row) for row in (party(user_id) if owned is None else owned)]
    if not team:
        return {"outcome": "no_party", "log": [i18n.t(lang, "battle.log.no_party")]}

    alive = [idx for idx, member in enumerate(team) if not member.fainted]
    if not alive:
        return {"outcome": "lost", "log": [i18n.t(lang, "battle.log.team_cant_battle")],
                "wild_hp": wild.current_hp, "wild_level": wild.level}

    b = Battle(wild=wild, team=team, active=alive[0], lang=lang)
    start_log(b)
    while b.outcome is None and b.turn < max_turns:
        if b.needs_switch:
            switch_to(b, b.alive_indexes()[0], forced=True)
            continue
        # Ohne Spielerentscheidung: megaentwickeln, sobald es geht.
        if can_mega_evolve(b, b.active_mon):
            mega_evolve(b, b.active_mon)
        run_turn(b, ai_choose_move(b.active_mon, wild))

    reward = None
    if b.outcome == "won":
        reward = on_battle_won(user_id, b, encounter.get("id"))["reward"]
    save_team(team)
    result = {
        "outcome": b.outcome or "timeout",
        "log": b.log,
        "wild_hp": wild.current_hp,
        "wild_level": wild.level,
        # For the battle history (see battle_history.py).
        "opponent": wild.name,
        "exp_details": b.exp_details,
    }
    if reward:
        result.update(reward)
    return result


# Registers the ability handlers on the event hub (see battle_events.py) -
# at the end, because ability_effects itself uses functions of this module.
from . import ability_effects  # noqa: E402,F401
