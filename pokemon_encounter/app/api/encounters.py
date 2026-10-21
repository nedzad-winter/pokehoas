"""Queue, battle and catch.

The queue is per user (`encounter.user_id`, see db.py): a trigger event creates
one encounter per player, each rolled from THEIR own unlocked regions. An
encounter disappears from the queue as soon as its owner is done with it
(caught/defeated/fled).

Old encounters from before that split (`user_id IS NULL`) stay household-wide:
every eligible user gets their own attempt at the same encounter, each from the
original state (see encounter_state.py), and it's only fully closed when all of
them are done or retention expires.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import (battle, battle_session, db, encounter_state, i18n, rarity, roaming, spawn,
                trainer_battle)
from .. import achievements, breeding, economy
from ..auth import get_current_user

router = APIRouter(prefix="/api", tags=["encounters"])


class BallThrow(BaseModel):
    ball: str = "poke_ball"


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _decorate(
    encounter: dict[str, Any], user_id: str, source_labels: dict[str, str] | None = None
) -> dict[str, Any]:
    # HP/level from THIS user's perspective - another may have already damaged
    # the Pokemon, their own attempt still starts fresh.
    encounter = encounter_state.for_user(encounter, user_id)
    species = db.query_one("SELECT * FROM species WHERE id = ?", (encounter["species_id"],))
    if species is not None:
        species["rarity_tier"] = rarity.tier_for(species)
    encounter["species"] = species
    # Anzeigename des ausloesenden Geraets statt der Entity-ID (die rohe ID
    # bleibt fuer Debug/Filter erhalten).
    encounter["source_label"] = spawn.source_label(encounter.get("source_entity"), source_labels)
    event_gen = spawn.event_generation(encounter.get("source_entity"))
    if event_gen is not None:
        encounter["source_label"] = i18n.t(db.user_language(user_id), "queue.source_event",
                                           gen=event_gen)
    encounter["ivs"] = json.loads(encounter["ivs"])
    encounter["shiny"] = bool(encounter["shiny"])
    encounter["known_species"] = battle.species_is_known(encounter["species_id"], user_id)

    combatant = battle.combatant_from_encounter(encounter)
    # Current special ball multipliers for a throw from the queue (web app marks
    # boosted balls in the selection).
    context = queue_catch_context(encounter, combatant, user_id)
    encounter["ball_boosts"] = {
        option["key"]: option["multiplier"]
        for option in battle.ball_options(combatant, context)
        if option["boosted"]
    }
    # Only override AFTER context - `current_hp is None` means "untouched"
    # (Quick Ball).
    encounter["max_hp"] = combatant.max_hp
    encounter["current_hp"] = combatant.current_hp
    return encounter


@router.get("/encounters")
def list_encounters(
    status: str = "waiting", limit: int = 100, current_user: dict = Depends(get_current_user)
) -> list[dict[str, Any]]:
    spawn.expire_stale_encounters()
    battle_session.release_stale_battles()
    # Own queue plus the old shared encounters (user_id NULL). Encounters this
    # user is done with are hidden - a shared one stays visible for all others.
    rows = db.query(
        "SELECT e.* FROM encounter e "
        "LEFT JOIN encounter_user eu ON eu.encounter_id = e.id AND eu.user_id = ? "
        "WHERE e.status = ? AND eu.outcome IS NULL "
        "AND (e.user_id IS NULL OR e.user_id = ?) "
        "ORDER BY e.created_at DESC LIMIT ?",
        (current_user["id"], status, current_user["id"], limit),
    )
    labels = spawn.source_labels()
    return [_decorate(row, current_user["id"], labels) for row in rows]


@router.get("/encounters/{encounter_id}")
def get_encounter(
    encounter_id: int, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))
    if row is None or not encounter_state.belongs_to(row, current_user["id"]):
        raise HTTPException(status_code=404,
                             detail=i18n.t(current_user["language"], "error.encounter_not_found"))
    return _decorate(row, current_user["id"])


def open_for_user(encounter_id: int, user_id: str, lang: str) -> dict[str, Any]:
    """Encounter that is still open for THIS user - otherwise 404/409.

    Ein Encounter aus der Warteschlange eines anderen Nutzers existiert fuer
    diesen Nutzer nicht (404), nur die alten geteilten gehoeren allen."""
    row = db.query_one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))
    if row is None or not encounter_state.belongs_to(row, user_id):
        raise HTTPException(status_code=404, detail=i18n.t(lang, "error.encounter_not_found"))
    if row["status"] not in ("waiting", "active") or encounter_state.is_done(encounter_id, user_id):
        raise HTTPException(status_code=409, detail=i18n.t(lang, "error.encounter_already_done"))
    return row


@router.post("/encounters/{encounter_id}/flee")
def flee(
    encounter_id: int, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    user_id = current_user["id"]
    open_for_user(encounter_id, user_id, current_user["language"])
    own = battle_session.ensure_free(encounter_id, user_id, allow_own=True)
    if own is not None:
        battle_session.close_own(own, "fled")
    # Fleeing only applies to this user's attempt - others in the
    # household can still try.
    closed = encounter_state.finish(encounter_id, user_id, "fled")
    return {"ok": True, "closed": closed}


@router.post("/encounters/{encounter_id}/throw")
def throw(
    encounter_id: int, payload: BallThrow, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    user_id = current_user["id"]
    lang = current_user["language"]
    row = open_for_user(encounter_id, user_id, lang)
    battle_session.ensure_free(encounter_id, user_id)
    # Own progress: a ball can be the first action anytime.
    row = encounter_state.for_user(row, user_id)

    if payload.ball not in battle.BALLS:
        raise HTTPException(status_code=400, detail=i18n.t(lang, "error.unknown_ball", ball=payload.ball))
    if not consume_ball(payload.ball, user_id):
        raise HTTPException(status_code=409, detail=i18n.t(lang, "error.no_balls_of_kind"))

    wild = battle.combatant_from_encounter(row)
    result = battle.throw_ball(wild, user_id, payload.ball,
                               context=queue_catch_context(row, wild, user_id))

    if not result.caught:
        response = {
            "caught": False,
            "critical": result.critical,
            "shakes": result.shakes,
            "probability": round(result.probability, 4),
            "reason": result.reason,
        }
        if wild.roams:
            # A roaming legendary runs off after a missed throw (see roaming.py).
            response["fled"] = True
            response["closed"] = roaming.escape(encounter_id, user_id, wild)
            return response
        # Miss outside battle: there's no team on the field for the opponent
        # to attack - the encounter stays in the queue. (In manual battle, the
        # opponent gets their turn, see battle_session._throw.)
        encounter_state.save_progress(encounter_id, user_id, current_hp=wild.current_hp)
        return response

    caught = finalize_catch(row, wild, user_id, payload.ball)
    return {
        "caught": True,
        "critical": result.critical,
        "shakes": result.shakes,
        "probability": round(result.probability, 4),
        **caught,
    }


def queue_catch_context(
    row: dict[str, Any], wild: battle.Combatant, user_id: str
) -> battle.CatchContext:
    """Catch context for a throw from the queue (without battle): no
    turns played; "first turn" (Quick Ball) only while the Pokémon is
    untouched - after a battle or miss, `current_hp` is set."""
    return battle.catch_context(
        wild, user_id, turn=0, first_turn=row.get("current_hp") is None,
        biome=row.get("biome"),
    )


def finalize_catch(
    row: dict[str, Any], wild: battle.Combatant, user_id: str,
    ball_key: str | None = None,
) -> dict[str, Any]:
    """Common success path for ANY successful catch - ball from the
    queue (`throw`) as well as ball in manual battle (`battle_session._throw`):
    create Pokémon, finalize this user's attempt (`encounter_state.finish`),
    update Pokédex. New Pokémon go to the team if a slot (1-6) is free,
    otherwise to the box, like in the main series.

    Returns `{"owned_id", "reward"}`; catch rewards belong here.
    Heal Ball: the caught Pokémon arrives with full HP. A species' very first
    catch starts locked if the player opted into `auto_lock_first_catch`
    (db.user_auto_lock_first_catch), same as legendary/mythical catches
    always do (rarity.auto_lock). Every shiny catch also starts locked unless
    the player switched `auto_lock_shiny` off (db.user_auto_lock_shiny).
    """
    if ball_key and battle.BALLS.get(ball_key, {}).get("effect") == "heal":
        wild.current_hp = wild.max_hp
        wild.status = None
    encounter_id = row["id"]
    now = _now()
    # Never below the species' IV minimum - also for encounters that were
    # rolled before it rose. Must happen before economy.on_catch raises it.
    caught_ivs = economy.clamp_ivs_to_floor(battle._as_ivs(row["ivs"]), row["species_id"])
    # Eier im Team belegen ebenfalls Teamplaetze (siehe breeding.py).
    free_slot = breeding.free_party_slot(user_id)
    caught_level = min(wild.level, battle.MAX_LEVEL)
    # Must be read BEFORE the dex_entry upsert below creates/bumps the row.
    existing_dex = db.query_one(
        "SELECT caught_count FROM dex_entry WHERE species_id = ? AND user_id = ?",
        (row["species_id"], user_id),
    )
    first_catch = existing_dex is None or not existing_dex["caught_count"]
    auto_lock = rarity.auto_lock(wild.species) or (
        first_catch and db.user_auto_lock_first_catch(user_id)
    ) or (
        bool(row["shiny"]) and db.user_auto_lock_shiny(user_id)
    )
    owned_id = db.execute(
        """
        INSERT INTO owned_pokemon (
            user_id, species_id, level, exp, shiny, ivs, nature, gender, ability,
            current_hp, party_slot, caught_at, caught_from, hp_updated_at, locked
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            # `ability` comes 1:1 from the encounter - a caught Pokémon
            # keeps the individual it had in battle, instead of rolling a new
            # ability (see spawn.roll_ability).
            user_id, row["species_id"], caught_level,
            battle.exp_for_level(caught_level, wild.species.get("growth_rate", "medium_fast")),
            row["shiny"], json.dumps(caught_ivs), row["nature"], row["gender"], row.get("ability"),
            wild.current_hp, free_slot, now, encounter_id, now,
            int(auto_lock),
        ),
    )
    encounter_state.finish(encounter_id, user_id, "caught",
                           current_hp=wild.current_hp, level=wild.level)
    db.execute(
        """
        INSERT INTO dex_entry (species_id, user_id, seen_count, caught_count, shiny_caught,
                                first_seen_at, first_caught_at)
        VALUES (?, ?, 1, 1, ?, ?, ?)
        ON CONFLICT(species_id, user_id) DO UPDATE SET
            caught_count = caught_count + 1,
            shiny_caught = MAX(dex_entry.shiny_caught, excluded.shiny_caught),
            first_caught_at = COALESCE(dex_entry.first_caught_at, excluded.first_caught_at)
        """,
        (row["species_id"], user_id, int(row["shiny"]), now, now),
    )
    rewards = economy.on_catch(user_id, {**row, "level": wild.level}, owned_id)
    lang = db.user_language(user_id)
    new_achievements = (achievements.sync_pokedex(user_id, lang)
                        + achievements.sync_milestones(user_id, lang, ("catches", "dex_caught", "roamers")))
    return {"owned_id": owned_id, "reward": rewards, **rewards,
            "achievements": [a["title"] for a in new_achievements]}


@router.post("/encounters/{encounter_id}/auto-battle")
def run_auto_battle(
    encounter_id: int, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    """One-click battle with the current team until the wild Pokemon faints
    (EXP, EVs, money). Catching automatically is an auto-rule action
    (app/auto_rules.py), not part of this button."""
    from .. import auto_rules  # auto_rules imports this module

    user_id = current_user["id"]
    lang = current_user["language"]
    if not db.get_setting("auto_battle_enabled", False):
        raise HTTPException(status_code=409, detail=i18n.t(lang, "error.auto_battle_disabled"))

    row = open_for_user(encounter_id, user_id, lang)
    battle_session.ensure_free(encounter_id, user_id)
    if trainer_battle.ongoing_for_user(user_id) is not None:
        raise HTTPException(status_code=409, detail=i18n.t(lang, "error.finish_gym_battle_first"))

    result = auto_rules.execute(user_id, row, auto_rules.Actions(mode="defeat"))
    # Battle-engine wording for the queue card (won/lost/timeout/no_party).
    result["outcome"] = {"defeated": "won"}.get(result["outcome"], result["outcome"])
    return result


@router.get("/party")
def get_party(current_user: dict = Depends(get_current_user)) -> list[dict[str, Any]]:
    rows = battle.party(current_user["id"])
    for row in rows:
        row["species"] = db.query_one("SELECT * FROM species WHERE id = ?", (row["species_id"],))
        row["ivs"] = json.loads(row["ivs"])
    return rows


@router.get("/inventory")
def get_inventory(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Balls + inventory of the current user, plus ball master data (label,
    bonus) - the web app shows selection and inventory per ball from this."""
    rows = db.query(
        "SELECT item_key, quantity FROM inventory WHERE user_id = ?", (current_user["id"],)
    )
    return {
        "balls": battle.BALLS,
        "items": {row["item_key"]: row["quantity"] for row in rows},
    }


def consume_ball(ball_key: str, user_id: str) -> bool:
    """Takes one ball from the bag. The check and the decrement happen in one
    statement: two throws in parallel (double click) can't both spend the last ball."""
    with db.cursor() as cur:
        cur.execute(
            "UPDATE inventory SET quantity = quantity - 1 "
            "WHERE item_key = ? AND user_id = ? AND quantity > 0",
            (ball_key, user_id),
        )
        return cur.rowcount == 1
