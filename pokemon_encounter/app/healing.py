"""Healing.

Two simple ways for defeated or damaged Pokemon to get back in shape (both values
are settings):

* Lazy regeneration: each Pokemon of a user regenerates `heal_regen_percent_per_hour`
  percent of its max HP per hour (+1 point per defeated Elite Four trainer, see
  `regen_percent`) - including fainted ones. Calculated when reading
  (team view, battle start), based on `owned_pokemon.hp_updated_at`. During an ongoing
  battle, nothing regenerates.
* "Heal team": heals all of the user's Pokemon to full immediately, then
  `heal_team_cooldown_minutes` cooldown (per user, `app_user.last_team_heal_at`),
  reduced by 1 minute per defeated Elite Four member or Champion (see
  `cooldown_minutes`).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from . import db, i18n


def _now() -> datetime:
    return datetime.now().astimezone()


def max_hp_of(row: dict[str, Any], species: dict[str, Any] | None = None) -> int:
    from .battle import stat_hp  # Local: battle imports this module too

    species = species or db.query_one(
        "SELECT base_hp FROM species WHERE id = ?", (row["species_id"],)
    )
    ivs = row["ivs"] if isinstance(row["ivs"], dict) else json.loads(row["ivs"] or "{}")
    ev_hp = _ev_hp(row.get("evs"))
    return stat_hp(species["base_hp"], ivs.get("hp", 15), row["level"], ev_hp,
                   species_id=row["species_id"])


def _ev_hp(value: Any) -> int:
    """HP EV of a row (see app/evs.py) - missing/NULL = 0. Deliberately
    inlined instead of importing evs, which imports this module."""
    if not isinstance(value, dict):
        value = json.loads(value or "{}")
    return max(0, int(value.get("hp") or 0))


def has_ongoing_battle(user_id: str) -> bool:
    """Wild battle OR gym/league battle (trainer_battle.py)."""
    return db.query_one(
        "SELECT 1 FROM battle_session WHERE user_id = ? AND status = 'ongoing' "
        "UNION ALL SELECT 1 FROM trainer_battle_session WHERE user_id = ? AND status = 'ongoing'",
        (user_id, user_id),
    ) is not None


# Percentage points of regeneration per hour each defeated Elite Four member adds.
ELITE_FOUR_REGEN_BONUS = 1

# Minutes the "heal team" cooldown shrinks by per defeated Elite Four member or Champion.
ELITE_FOUR_CHAMPION_COOLDOWN_REDUCTION_MINUTES = 1


def regen_percent(user_id: str) -> float:
    """Regeneration rate in % of max HP per hour: the base setting plus +1 percentage
    point per Elite Four trainer this user has defeated."""
    from . import achievements  # Local: achievements -> rewards -> tms imports this module

    percent = float(db.get_setting("heal_regen_percent_per_hour", 20) or 0)
    return percent + ELITE_FOUR_REGEN_BONUS * achievements.elite_four_defeated(user_id)


def cooldown_minutes(user_id: str) -> float:
    """"Heal team" cooldown for this user: the base setting minus 1 minute per
    Elite Four member or Champion this user has defeated, never below 0.
    Computed live from the achievement table, so it applies retroactively to
    every trainer already beaten before this reduction existed."""
    from . import achievements  # Local: achievements -> rewards -> tms imports this module

    from . import god_mode  # Local like achievements (import order)

    if god_mode.active(user_id):
        return 0.0
    base = float(db.get_setting("heal_team_cooldown_minutes", 60) or 0)
    reduction = (
        ELITE_FOUR_CHAMPION_COOLDOWN_REDUCTION_MINUTES
        * achievements.elite_four_and_champion_defeated(user_id)
    )
    return max(0.0, base - reduction)


def apply_regen(user_id: str, now: datetime | None = None) -> int:
    """Applies time-based regeneration for all Pokemon of a user.
    Returns the number of healed Pokemon."""
    if has_ongoing_battle(user_id):
        return 0
    now = now or _now()
    percent = regen_percent(user_id)
    rows = db.query(
        "SELECT o.id, o.species_id, o.level, o.ivs, o.evs, o.current_hp, o.hp_updated_at, s.base_hp "
        "FROM owned_pokemon o JOIN species s ON s.id = o.species_id "
        "WHERE o.user_id = ? AND o.current_hp IS NOT NULL",
        (user_id,),
    )
    healed = 0
    for row in rows:
        max_hp = max_hp_of(row, row)
        if row["current_hp"] >= max_hp:
            continue
        if not row["hp_updated_at"] or percent <= 0:
            # Start clock (old data without timestamp) - regeneration starts now.
            if not row["hp_updated_at"]:
                db.execute("UPDATE owned_pokemon SET hp_updated_at = ? WHERE id = ?",
                           (now.isoformat(), row["id"]))
            continue
        since = datetime.fromisoformat(row["hp_updated_at"])
        per_hour = max_hp * percent / 100
        hours = max(0.0, (now - since).total_seconds() / 3600)
        gain = int(hours * per_hour)
        if gain < 1:
            continue
        new_hp = min(max_hp, row["current_hp"] + gain)
        # Only advance clock by "used" time so partial hours aren't lost.
        stamp = now if new_hp >= max_hp else since + timedelta(hours=gain / per_hour)
        db.execute(
            "UPDATE owned_pokemon SET current_hp = ?, hp_updated_at = ? WHERE id = ?",
            (new_hp, stamp.isoformat(), row["id"]),
        )
        healed += 1
    return healed


def heal_status(user_id: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    cooldown = cooldown_minutes(user_id)
    user = db.get_user(user_id) or {}
    last = user.get("last_team_heal_at")
    next_at = None
    if last and cooldown > 0:
        candidate = datetime.fromisoformat(last) + timedelta(minutes=cooldown)
        if candidate > now:
            next_at = candidate
    return {
        "available": next_at is None,
        "next_heal_at": next_at.isoformat() if next_at else None,
        "cooldown_minutes": cooldown,
        "regen_percent_per_hour": regen_percent(user_id),
    }


def heal_team(user_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Heals all of the user's Pokemon to full. Raises ValueError on cooldown
    or ongoing battle (the endpoint converts to 409)."""
    now = now or _now()
    if has_ongoing_battle(user_id):
        raise ValueError(i18n.t(db.user_language(user_id), "error.cant_heal_in_battle"))
    status = heal_status(user_id, now)
    if not status["available"]:
        raise ValueError(i18n.t(db.user_language(user_id), "error.heal_cooldown"))
    rows = db.query(
        "SELECT o.id, o.species_id, o.level, o.ivs, o.evs, s.base_hp FROM owned_pokemon o "
        "JOIN species s ON s.id = o.species_id WHERE o.user_id = ?",
        (user_id,),
    )
    for row in rows:
        db.execute(
            "UPDATE owned_pokemon SET current_hp = ?, hp_updated_at = ? WHERE id = ?",
            (max_hp_of(row, row), now.isoformat(), row["id"]),
        )
    db.execute("UPDATE app_user SET last_team_heal_at = ? WHERE id = ?",
               (now.isoformat(), user_id))
    return {"healed": len(rows), **heal_status(user_id, now)}
