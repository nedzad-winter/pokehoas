"""Roaming legendaries: they run away instead of waiting for you.

Like the roaming legendaries of the main series (Raikou, Entei, Suicune,
Latias/Latios, Tornadus/Thundurus/Landorus) they don't stay put:

* Queue: a throw that doesn't catch it makes it run off. The attempt is over
  for this player and the encounter leaves their queue (`fled`).
* Battle: on its turn a roamer escapes at once (`battle.run_turn` ends the
  battle with `fled`). Only a turn it cannot use keeps it there: asleep
  (Hypnosis), paralysed or frozen (`battle._status_prevents_action`), or
  trapped by a move like Mean Look. A faster Pokemon gets its attack in first;
  if that doesn't knock it out, the roamer is gone with the damage.
* Damage survives the escape: the HP fraction is noted per player and species
  (`roamer_wound`), and the next spawn of that species starts from it - see
  `spawn.create_encounter`.

Only wild encounters are affected. No import from `battle` here (battle imports
this module).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from . import db, encounter_state

if TYPE_CHECKING:
    from .battle import Combatant

# National Dex numbers: Raikou, Entei, Suicune (Gen 2), Latias, Latios (Gen 3),
# Tornadus, Thundurus, Landorus (Gen 5 - the Incarnate forms).
ROAMING_IDS = frozenset({243, 244, 245, 380, 381, 641, 642, 645})


def is_roaming(species_id: int) -> bool:
    return species_id in ROAMING_IDS


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def remember(user_id: str, species_id: int, current_hp: int, max_hp: int) -> None:
    """Notes how hurt a roamer is after it escaped from this player. Full HP
    leaves no wound behind."""
    if current_hp >= max_hp:
        db.execute("DELETE FROM roamer_wound WHERE user_id = ? AND species_id = ?",
                   (user_id, species_id))
        return
    db.execute(
        """
        INSERT INTO roamer_wound (user_id, species_id, hp_fraction, updated_at)
        VALUES (?,?,?,?)
        ON CONFLICT(user_id, species_id) DO UPDATE SET
            hp_fraction = excluded.hp_fraction,
            updated_at = excluded.updated_at
        """,
        (user_id, species_id, current_hp / max_hp, _now()),
    )


def take_wound(user_id: str, species_id: int) -> float | None:
    """HP fraction this species escaped with from this player - consumed, so the
    next spawn of it starts from there and the note doesn't carry further."""
    row = db.query_one(
        "SELECT hp_fraction FROM roamer_wound WHERE user_id = ? AND species_id = ?",
        (user_id, species_id),
    )
    if row is None:
        return None
    db.execute("DELETE FROM roamer_wound WHERE user_id = ? AND species_id = ?",
               (user_id, species_id))
    return float(row["hp_fraction"])


def escape(encounter_id: int, user_id: str, wild: Combatant) -> bool:
    """Ends this player's attempt because the roamer ran off. Returns True if the
    encounter left the queue entirely (see `encounter_state.settle`)."""
    remember(user_id, wild.species["id"], wild.current_hp, wild.max_hp)
    return encounter_state.finish(encounter_id, user_id, "fled",
                                  current_hp=wild.current_hp, level=wild.level)
