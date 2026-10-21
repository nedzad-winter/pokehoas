"""Moves and movesets.

Which species learns which move, and at which level, follows the main games:
`data/learnsets.json` (built by `scripts/build_learnsets.py` from Showdown's
data) holds, per species, the level-up moves of `data/moves.json` with their
level, the TM moves and the egg moves - Scarlet/Violet (gen 9) for every
species in it, otherwise the newest generation the species appeared in.
Moves learned on evolution or only via the Move Reminder count as level 1.

A Pokemon's learned moves (`learned_moves`) are its level-up moves up to its
level, plus moves taught by TM (`tms.py`) and egg moves inherited when it
hatched (`breeding.py`). From its level-up moves it deterministically gets up
to four battle moves (`moveset_for`):

* for each type of the species and normal, the strongest damaging move,
* at most one status move of its own types,
* filled with the next best damaging moves.

"Strongest" means: power x accuracy x matching base stat (attack for
physical, special attack for special moves) - a Pokemon with high special
attack thus prefers special moves. Same species + same level = always the
same moveset, so nothing needs to be stored.

A move's `min_level` no longer decides when a species learns it naturally -
it is only the level a TM needs (`tms.min_level`), so a TM never hands a
level 5 Pokemon a 110 BP attack.

Fallback: a few species learn no damaging move of the catalog yet at their
level (Magikarp below level 15 ...) or none at all (Abra, Metapod, Ditto ...).
So they can still fight, they get their earliest damaging level-up move ahead
of time (Magikarp: Tackle), or - learning none - the single best damaging move
of the old type rule: own types + normal, up to the move's `min_level`
(`_fallback_attack`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
MAX_MOVES = 4

_moves: dict[str, dict[str, Any]] | None = None
_learnsets: dict[str, dict[str, Any]] | None = None


def all_moves() -> dict[str, dict[str, Any]]:
    global _moves
    if _moves is None:
        raw = json.loads((DATA_DIR / "moves.json").read_text(encoding="utf-8"))
        _moves = {key: {"key": key, **value} for key, value in raw.items()}
    return _moves


def all_learnsets() -> dict[str, dict[str, Any]]:
    """Species id (as string) -> {"gen", "level": {move: level}, "tm": [...], "egg": [...]}.

    Moves no longer in `data/moves.json` are dropped, so a stale learnset file
    can never hand out an unknown move."""
    global _learnsets
    if _learnsets is None:
        raw = json.loads((DATA_DIR / "learnsets.json").read_text(encoding="utf-8"))
        known = all_moves()
        _learnsets = {
            species_id: {
                "gen": entry["gen"],
                "level": {k: lvl for k, lvl in entry["level"].items() if k in known},
                "tm": [k for k in entry["tm"] if k in known],
                "egg": [k for k in entry["egg"] if k in known],
            }
            for species_id, entry in raw.items()
        }
    return _learnsets


def learnset(species: dict[str, Any]) -> dict[str, Any] | None:
    """The species' learnset, None for a species without one (no id)."""
    species_id = species.get("id")
    return all_learnsets().get(str(species_id)) if species_id is not None else None


# Moves outside moves.json: never learned by a species (see moveset_for /
# learned_moves), only known by the training sparring partners (training.py).
# Splash has no effect at all (see battle.use_move, flag "splash").
SPECIAL_MOVES: dict[str, dict[str, Any]] = {
    "splash": {"key": "splash", "name": "Splash", "name_de": "Platscher", "type": "normal",
               "power": None, "accuracy": None, "category": "status", "min_level": 1,
               "splash": True},
}


def get(key: str) -> dict[str, Any]:
    if key in SPECIAL_MOVES:
        return SPECIAL_MOVES[key]
    return all_moves()[key]


def learn_level(species: dict[str, Any], key: str) -> int | None:
    """Level at which the species learns `key` by level-up, None if never."""
    entry = learnset(species)
    return entry["level"].get(key) if entry else None


def can_learn(move: dict[str, Any], species: dict[str, Any]) -> bool:
    """Whether `species` learns `move` by level-up at all (level aside)."""
    return learn_level(species, move["key"]) is not None


def tm_moves(species: dict[str, Any]) -> set[str] | None:
    """Moves the species can learn from a TM in its learnset generation,
    None for a species without a learnset."""
    entry = learnset(species)
    return set(entry["tm"]) if entry else None


_machine_moves: dict[int, set[str]] = {}


def is_machine_move(key: str, gen: int) -> bool:
    """Whether `key` is a TM in generation `gen` at all (some species of that
    generation learns it by TM)."""
    if gen not in _machine_moves:
        _machine_moves[gen] = {k for entry in all_learnsets().values()
                               if entry["gen"] == gen for k in entry["tm"]}
    return key in _machine_moves[gen]


def egg_moves(species: dict[str, Any]) -> list[str]:
    """Egg moves of the species (only base stages have any)."""
    entry = learnset(species)
    return list(entry["egg"]) if entry else []


def inherited_egg_moves(owned: dict[str, Any]) -> list[str]:
    """Egg moves an owned Pokemon hatched with (`owned_pokemon.egg_moves`,
    JSON list; unknown keys are ignored, like `tms.taught_moves`)."""
    try:
        keys = json.loads(owned.get("egg_moves") or "null") or []
    except ValueError:
        return []
    known = all_moves()
    return [key for key in keys if isinstance(key, str) and key in known]


# Pure status moves have power=None (no damage) - for selection, a comparison
# value is still needed as if it were a move with this power. Lower than most
# real moves, so status is an option but damage isn't routinely displaced.
STATUS_MOVE_SCORE_POWER = 55


def _score(move: dict[str, Any], species: dict[str, Any]) -> float:
    stat = species.get("base_atk", 50) if move["category"] == "physical" else species.get("base_spa", 50)
    accuracy = move["accuracy"] if move["accuracy"] is not None else 100
    power = move["power"] if move["power"] is not None else STATUS_MOVE_SCORE_POWER
    score = power * accuracy / 100 * stat
    if move.get("recoil"):
        score *= 0.9
    if move.get("drain"):
        score *= 1.1
    if move.get("high_crit"):
        score *= 1.05
    if move.get("priority"):
        score *= 1.05
    if move.get("flinch_chance"):
        score *= 1.05
    return score


def _ranked(pool: list[dict[str, Any]], species: dict[str, Any]) -> list[dict[str, Any]]:
    """Score descending, ties broken stably by key."""
    return sorted(pool, key=lambda m: (-_score(m, species), m["key"]))


def _type_rule_moves(species: dict[str, Any], level: int) -> list[dict[str, Any]]:
    """The old rule: every move of the species' own types + normal, up to the
    move's `min_level`. Only for species without a learnset and as fallback."""
    types = {species.get("type1"), species.get("type2"), "normal"}
    return [m for m in all_moves().values() if m["type"] in types and m["min_level"] <= level]


def _fallback_attack(species: dict[str, Any], level: int) -> str:
    """The species' earliest damaging level-up move (Magikarp: Tackle), or -
    if it learns none at all - the best damaging move of the type rule."""
    entry = learnset(species) or {"level": {}}
    known = all_moves()
    own = [(lvl, key) for key, lvl in entry["level"].items() if known[key]["power"] is not None]
    if own:
        return min(own)[1]
    damaging = [m for m in _type_rule_moves(species, level) if m["power"] is not None]
    return _ranked(damaging, species)[0]["key"]


def _natural_moves(species: dict[str, Any], level: int) -> list[dict[str, Any]]:
    """Level-up moves up to `level` (incl. the fallback attack, see module doc)."""
    entry = learnset(species)
    if entry is None:
        return _type_rule_moves(species, level)
    known = all_moves()
    pool = [known[key] for key, lvl in entry["level"].items() if lvl <= level]
    if not any(m["power"] is not None for m in pool):
        pool.append(known[_fallback_attack(species, level)])
    return pool


def moveset_for(species: dict[str, Any], level: int) -> list[str]:
    """Deterministic moveset (list of move keys, max 4) from the level-up moves.

    The "strongest move per type" (step 1) and filler moves (step 3) come
    deliberately ONLY from damaging moves - otherwise one or two types with
    many status options (e.g. poison) could dominate a moveset that then hardly
    deals damage. Instead, each Pokemon gets at most ONE status move (step 2)
    of its own types, if it has learned one at all.
    """
    types = [t for t in (species.get("type1"), species.get("type2")) if t]
    unlocked = _natural_moves(species, level)
    damaging = [m for m in unlocked if m["power"] is not None]
    status = [m for m in unlocked if m["power"] is None]

    chosen: list[str] = []
    for move_type in types + ["normal"]:
        for move in _ranked([m for m in damaging if m["type"] == move_type], species):
            if move["key"] not in chosen:
                chosen.append(move["key"])
                break

    status_candidates = _ranked([m for m in status if m["type"] in types], species)
    if status_candidates and len(chosen) < MAX_MOVES:
        best = status_candidates[0]["key"]
        if best not in chosen:
            chosen.append(best)

    # Fill: next best damaging moves of any type the species learned.
    for move in _ranked(damaging, species):
        if len(chosen) >= MAX_MOVES:
            break
        if move["key"] not in chosen:
            chosen.append(move["key"])
    return chosen[:MAX_MOVES]


def learned_moves(
    species: dict[str, Any], level: int, taught: list[str] | tuple[str, ...] = (),
    egg: list[str] | tuple[str, ...] = (),
) -> list[str]:
    """All moves the Pokemon can pick its battle moves from at `level`.

    `taught`: moves learned from a TM (`tms.taught_moves`) - level-gated by the
    move's `min_level`, like when they were taught (see `tms.teach`).
    `egg`: egg moves it hatched with (`inherited_egg_moves`) - known from level 1.
    """
    natural = _natural_moves(species, level)
    keys = [m["key"] for m in sorted(
        natural, key=lambda m: (learn_level(species, m["key"]) or m["min_level"], m["key"]))]
    known = all_moves()
    for key in egg:
        if key in known and key not in keys:
            keys.append(key)
    for key in taught:
        if key in known and key not in keys and known[key]["min_level"] <= level:
            keys.append(key)
    return keys


def owned_learned_moves(species: dict[str, Any], owned: dict[str, Any],
                        level: int | None = None) -> list[str]:
    """`learned_moves` of an owned Pokemon (its TM and egg moves included)."""
    from . import tms  # local import: tms imports this module

    return learned_moves(species, owned["level"] if level is None else level,
                         tms.taught_moves(owned), inherited_egg_moves(owned))


def learners(key: str) -> dict[str, Any]:
    """Who learns `key` how: level-up (species id -> level), TM and egg
    (species ids) - for the AttackDex."""
    level: dict[int, int] = {}
    tm: list[int] = []
    egg: list[int] = []
    for species_id, entry in all_learnsets().items():
        sid = int(species_id)
        if key in entry["level"]:
            level[sid] = entry["level"][key]
        if key in entry["tm"]:
            tm.append(sid)
        if key in entry["egg"]:
            egg.append(sid)
    return {"level": level, "tm": tm, "egg": egg}


def describe(key: str) -> dict[str, Any]:
    """Move for the frontend."""
    move = get(key)
    return {
        "key": key,
        "name": move["name"],
        "name_de": move["name_de"],
        "type": move["type"],
        "power": move["power"],
        "accuracy": move["accuracy"],
        "category": move["category"],
        "priority": move.get("priority", 0),
        "min_level": move["min_level"],
        "status": move.get("status"),
        "status_chance": move.get("status_chance") if move.get("status") else None,
        "stat_changes": move.get("stat_changes"),
        "stat_chance": move.get("stat_chance") if move.get("stat_changes") else None,
        "heal": move.get("heal"),
        "drain": move.get("drain"),
        "recoil": move.get("recoil"),
        "high_crit": bool(move.get("high_crit")),
        "trap": bool(move.get("trap")),
        "flinch_chance": move.get("flinch_chance"),
        "reset_stats": move.get("reset_stats"),
        "curse": move.get("curse"),
    }
