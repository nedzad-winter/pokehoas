"""Training: a sparring fight of one of your Pokemon against six level-100 Pokemon.

Started from the Pokemon's summary (`POST /api/training/{owned_id}/battle`),
for any own Pokemon - team or box, it doesn't have to join the team.

Unlocked once you have raised UNLOCK_COUNT Pokemon to level 100 - counted by
`achievements.level100_count` (`pokemon_level100`, permanent: releasing them
again doesn't take it back). The sparring team is drawn at random from the
species that the chosen Pokemon's damaging moves hit super effectively
(at least x2) - with only Thunder, an Electric Pokemon faces e.g. Flying or Water
Pokemon, or a x4 combination like Water/Flying. Species that every move hits
super effectively come first (see `roster`). Every one of them is level 100,
has no ability and knows only Splash.

It runs as a regular trainer battle (`trainer_battle.py`) with the trainer
TRAINER_ID. Only EXP is earned (see `trainer_battle._finish`): no money, EVs,
Pokedex entries, battle history or achievements - except the level-100 count
when the EXP gets a Pokemon there.
"""

from __future__ import annotations

import random
from typing import Any

from . import achievements, battle, db, god_mode
from . import moves as moves_mod

KIND = "training"
# Sparring partner selection: by the Pokemon's first type (x4 weaknesses to that
# type come first) or by its damaging moves. DEFAULT_MODE is what the summary
# preselects and what the API uses when no mode is given.
MODE_MOVES = "moves"
MODE_TYPE = "type"
MODES = (MODE_MOVES, MODE_TYPE)
DEFAULT_MODE = MODE_TYPE
TRAINER_ID = "training"
# Same threshold as the level-100 achievement that announces it.
UNLOCK_COUNT = achievements.LEVEL100_THRESHOLD[achievements.LEVEL100_101ST]
LEVEL = 100
TEAM_SIZE = 6
IV = 31
MOVES = ["splash"]

# The trainer the battle engine sees, next to the catalog trainers
# (trainers.get). `title` comes from the kind (trainers.KIND_TITLE_KEYS).
TRAINER: dict[str, Any] = {
    "id": TRAINER_ID,
    "kind": KIND,
    "name": "Sparring partner",
    "name_de": "Trainingspartner",
    "f": False,
    "type": None,
    "region": None,
    "region_label": "",
    "max_level": LEVEL,
    "iv": IV,
    "moves": MOVES,
    "sprite_url": None,
}


def unlocked(user_id: str) -> bool:
    return (god_mode.active(user_id)
            or achievements.level100_count(user_id) >= UNLOCK_COUNT)


def overview(user_id: str) -> dict[str, Any]:
    return {
        "unlocked": unlocked(user_id),
        "count": achievements.level100_count(user_id),
        "required": UNLOCK_COUNT,
        "level": LEVEL,
    }


def attack_types(species_id: int, move_keys: list[str]) -> list[str]:
    """Types of the member's damaging moves - its own types if it has none."""
    types = []
    for key in move_keys:
        move = moves_mod.get(key)
        if move["category"] != "status" and move["type"] not in types:
            types.append(move["type"])
    if types:
        return types
    member = db.query_one("SELECT type1, type2 FROM species WHERE id = ?", (species_id,))
    return [t for t in (member["type1"], member["type2"]) if t]


def _multipliers(types: list[str], species: dict[str, Any]) -> list[float]:
    """What each of `types` does against `species` (type chart only)."""
    defender = [t for t in (species["type1"], species["type2"]) if t]
    return [battle.type_effectiveness(t, defender) for t in types]


def _draw(pool: list[dict[str, Any]]) -> list[tuple[dict[str, Any], int]]:
    picks = (random.sample(pool, TEAM_SIZE) if len(pool) >= TEAM_SIZE
             else random.choices(pool, k=TEAM_SIZE))
    return [(row, LEVEL) for row in picks]


def roster(species_id: int, move_keys: list[str],
           mode: str = MODE_MOVES) -> list[tuple[dict[str, Any], int]]:
    """[(species-Zeile, Level), ...]: TEAM_SIZE Pokemon the chosen Pokemon's
    moves hit super effectively (>= x2).

    Best first: species that EVERY damaging move hits super effectively; if
    there are none, those hit super effectively by the most move types, and
    among them preferably the ones immune to none of the moves. If no move
    is super effective against anything (e.g. only Normal moves), species it
    hits at least neutrally with every move. Repeats only if that pool has
    fewer than TEAM_SIZE species.

    mode MODE_TYPE ignores the moves and uses the Pokemon's first type as the
    attack type instead: species with a x4 weakness to it come first (e.g. Rock
    types face Fire/Flying), otherwise the same fallbacks as above."""
    every = db.query("SELECT * FROM species ORDER BY id")
    if mode == MODE_TYPE:
        first = db.query_one("SELECT type1 FROM species WHERE id = ?", (species_id,))["type1"]
        types = [first]
        quad = [row for row in every if _multipliers(types, row)[0] >= 4]
        if quad:
            return _draw(quad)
    else:
        types = attack_types(species_id, move_keys)

    def score(row: dict[str, Any]) -> tuple[int, bool]:
        mults = _multipliers(types, row)
        return sum(m >= 2 for m in mults), all(m > 0 for m in mults)

    scores = [score(row) for row in every]
    best = max(scores)
    if best[0] >= 1:
        pool = [row for row, sc in zip(every, scores) if sc == best]
    else:
        pool = [row for row in every if min(_multipliers(types, row)) >= 1]
    return _draw(pool)
