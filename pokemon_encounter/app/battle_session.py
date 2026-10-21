"""Manual, turn-based battle against an encounter from the queue.

The state lives server-side in `battle_session`: whoever starts a battle claims
the encounter (`status='active'`, `claimed_by`). While the battle runs, no one
else can battle it, throw a ball, auto-battle it, or flee - which only matters
for the old household-wide encounters (`encounter.user_id IS NULL`), since an
own encounter belongs to one player anyway.

Battle end:
* Win (wild Pokemon defeated): EXP via `battle.on_battle_won`, this user's
  attempt is `defeated`.
* Catch (ball in battle): same success path as `/encounters/{id}/throw`
  (`encounters.finalize_catch`), the attempt is `caught`. A ball is allowed
  even as the very first action - only a wild Pokemon that leveled in this
  battle is no longer catchable.
* Flee (voluntary `run`): the encounter returns to the queue (`waiting`,
  `claimed_by` empty). Current HP (and possibly new level) are remembered by
  `encounter_state` PER USER - the player encounters "their" weakened
  Pokemon again, others start from the original state.
* Loss (your active Pokemon faints): unlike the mainline games there is no
  forced switch to the next team member - the fight ends immediately and the
  wild Pokemon flees, same as a roaming legendary escaping. This player's
  attempt is over; the encounter leaves their queue for good.
* Roaming legendary runs away on its turn (outcome `fled`): the attempt is over
  for this player and the encounter leaves their queue; its HP is noted for the
  next spawn (see roaming.py).
* Unused battles time out after `battle_timeout_minutes` and free the encounter
  the same way.

HP, level, EXP and items of your own team are written directly to `owned_pokemon`
after every turn; `state` only holds what doesn't exist outside of battle.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException

from . import (achievements, battle, battle_history, db, encounter_state, i18n, medicine, roaming,
               trainer_battle)

# One process, multiple threads (FastAPI thread pool): actions processed
# serially so double-clicks or two users don't interfere.
_lock = threading.RLock()

LOG_LIMIT = 200


def _now() -> datetime:
    return datetime.now().astimezone()


# ------------------------------------------------------------- Queries

def ongoing_for_encounter(encounter_id: int) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM battle_session WHERE encounter_id = ? AND status = 'ongoing' "
        "ORDER BY id DESC LIMIT 1",
        (encounter_id,),
    )


def ongoing_for_user(user_id: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM battle_session WHERE user_id = ? AND status = 'ongoing' "
        "ORDER BY id DESC LIMIT 1",
        (user_id,),
    )


def _user_label(user_id: str) -> str:
    user = db.get_user(user_id) or {}
    return user.get("display_name") or user.get("name") or user_id


def ensure_free(
    encounter_id: int, user_id: str, allow_own: bool = False
) -> dict[str, Any] | None:
    """409 if an ongoing battle holds the encounter.

    With `allow_own=True` the own ongoing battle is returned instead of being
    rejected (e.g. for "flee" from within battle)."""
    release_stale_battles()
    session = ongoing_for_encounter(encounter_id)
    if session is None:
        return None
    if session["user_id"] != user_id:
        raise HTTPException(
            status_code=409,
            detail=i18n.t(db.user_language(user_id), "error.already_fighting_this",
                           user=_user_label(session["user_id"])),
        )
    if allow_own:
        return session
    raise HTTPException(
        status_code=409,
        detail=i18n.t(db.user_language(user_id), "error.already_fighting_continue"),
    )


def close_own(session: dict[str, Any], status: str) -> None:
    """Ends an own ongoing battle without further consequences (the caller
    sets the encounter status themselves)."""
    db.execute(
        "UPDATE battle_session SET status = ?, updated_at = ? WHERE id = ? AND status = 'ongoing'",
        (status, _now().isoformat(), session["id"]),
    )


def release_stale_battles(now: datetime | None = None) -> int:
    minutes = float(db.get_setting("battle_timeout_minutes", 30) or 0)
    if minutes <= 0:
        return 0
    now = now or _now()
    cutoff = (now - timedelta(minutes=minutes)).isoformat()
    released = 0
    with _lock:
        for session in db.query(
            "SELECT * FROM battle_session WHERE status = 'ongoing' AND updated_at < ?", (cutoff,)
        ):
            _release_encounter(session["encounter_id"])
            close_own(session, "abandoned")
            released += 1
    return released


def _release_encounter(encounter_id: int) -> None:
    db.execute(
        "UPDATE encounter SET status = 'waiting', claimed_by = NULL "
        "WHERE id = ? AND status = 'active'",
        (encounter_id,),
    )


# ----------------------------------------------------------- State

def _to_state(b: battle.Battle, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    state = dict(extra or {})
    state.update({
        "team": [member.owned_id for member in b.team],
        "active": b.active,
        "participants": sorted(b.participants),
        "needs_switch": b.needs_switch,
        "fought": b.fought,
        "turn": b.turn,
        "outcome": b.outcome,
        "log": b.log[-LOG_LIMIT:],
        "choice_locks": {
            str(member.owned_id): member.choice_lock for member in b.team if member.choice_lock
        },
        # Mega-Entwicklung (siehe mega.py): welches Teammitglied gerade in
        # welcher Form kaempft und ob die Seite ihre eine Mega-Entwicklung
        # schon genutzt hat. Wie Status reiner Kampfzustand, lebt nur hier.
        "mega_used": b.mega_used,
        "megas": {
            str(member.owned_id): member.mega for member in b.team if member.mega
        },
        # Status is pure battle-internal state (see battle.py) - lives only here
        # in session state, not in owned_pokemon. Only set if actually affected,
        # so old sessions without this key don't interfere.
        "statuses": {
            str(member.owned_id): [member.status, member.status_counter]
            for member in b.team if member.status
        },
        "stat_stages": {
            str(member.owned_id): member.stat_stages
            for member in b.team if any(member.stat_stages.values())
        },
        # Ability state until the holder leaves the field (see Combatant.volatile).
        "volatiles": {
            str(member.owned_id): member.volatile for member in b.team if member.volatile
        },
        "wild": {
            "current_hp": b.wild.current_hp,
            "level": b.wild.level,
            "exp": b.wild.exp,
            "leveled": b.wild.leveled_in_battle,
            "choice_lock": b.wild.choice_lock,
            "status": b.wild.status,
            "status_counter": b.wild.status_counter,
            "stat_stages": {k: v for k, v in b.wild.stat_stages.items() if v},
            "volatile": b.wild.volatile,
            "trapped": b.wild.trapped,
        },
    })
    return state


def _load(session: dict[str, Any]) -> tuple[battle.Battle, dict[str, Any], dict[str, Any]]:
    state = json.loads(session["state"] or "{}")
    encounter = db.query_one("SELECT * FROM encounter WHERE id = ?", (session["encounter_id"],))
    if encounter is None:
        close_own(session, "abandoned")
        raise HTTPException(status_code=410,
                             detail=i18n.t(db.user_language(session["user_id"]), "error.encounter_gone"))
    encounter = encounter_state.for_user(encounter, session["user_id"])

    wild_state = state.get("wild", {})
    wild = battle.combatant_from_encounter(
        dict(encounter, level=wild_state.get("level", encounter["level"]))
    )
    if "current_hp" in wild_state:
        wild.current_hp = wild_state["current_hp"]
    wild.exp = wild_state.get("exp", 0)
    wild.leveled_in_battle = bool(wild_state.get("leveled"))
    wild.choice_lock = wild_state.get("choice_lock")
    wild.status = wild_state.get("status")
    wild.status_counter = wild_state.get("status_counter", 0)
    wild.stat_stages = {k: 0 for k in battle.STAT_STAGE_KEYS} | (wild_state.get("stat_stages") or {})
    wild.trapped = bool(wild_state.get("trapped"))
    wild.volatile = dict(wild_state.get("volatile") or {})

    team: list[battle.Combatant] = []
    locks = state.get("choice_locks", {})
    statuses = state.get("statuses", {})
    stat_stages = state.get("stat_stages", {})
    volatiles = state.get("volatiles", {})
    megas = state.get("megas", {})
    for owned_id in state.get("team", []):
        row = db.query_one(
            "SELECT * FROM owned_pokemon WHERE id = ? AND user_id = ?",
            (owned_id, session["user_id"]),
        )
        if row is None:
            # Team changed outside of battle - abort battle.
            _release_encounter(session["encounter_id"])
            close_own(session, "abandoned")
            raise HTTPException(status_code=409,
                                 detail=i18n.t(db.user_language(session["user_id"]), "error.team_changed"))
        member = battle.combatant_from_owned(row)
        member.choice_lock = locks.get(str(owned_id))
        member.status, member.status_counter = statuses.get(str(owned_id), (None, 0))
        member.stat_stages = {k: 0 for k in battle.STAT_STAGE_KEYS} | (stat_stages.get(str(owned_id)) or {})
        member.volatile = dict(volatiles.get(str(owned_id)) or {})
        battle.restore_mega(member, megas.get(str(owned_id)))
        team.append(member)

    b = battle.Battle(
        wild=wild, team=team, active=state.get("active", 0), turn=state.get("turn", 0),
        log=list(state.get("log", [])), participants=set(state.get("participants", [])),
        needs_switch=bool(state.get("needs_switch")), fought=bool(state.get("fought")),
        outcome=state.get("outcome"), mega_used=bool(state.get("mega_used")),
        lang=db.user_language(session["user_id"]),
    )
    return b, state, encounter


def _save(session: dict[str, Any], b: battle.Battle, state: dict[str, Any], status: str) -> None:
    battle.save_team(b.team)
    # HP/Level of the wild Pokemon belong to THIS user's attempt, not the
    # shared encounter (see encounter_state.py).
    encounter_state.save_progress(
        session["encounter_id"], session["user_id"],
        current_hp=b.wild.current_hp if b.fought else None, level=b.wild.level,
    )
    db.execute(
        "UPDATE battle_session SET state = ?, status = ?, updated_at = ? WHERE id = ?",
        (json.dumps(_to_state(b, state)), status, _now().isoformat(), session["id"]),
    )


# ------------------------------------------------------------ View

def _view(
    session: dict[str, Any], b: battle.Battle, state: dict[str, Any],
    encounter: dict[str, Any], user_id: str, new_from: int | None = None,
) -> dict[str, Any]:
    inventory = {
        row["item_key"]: row["quantity"]
        for row in db.query("SELECT item_key, quantity FROM inventory WHERE user_id = ?", (user_id,))
    }
    wild = battle.describe(b.wild)
    wild["shiny"] = bool(encounter["shiny"])
    wild["catchable"] = not b.wild.leveled_in_battle
    return {
        "id": session["id"],
        "encounter_id": session["encounter_id"],
        "status": session["status"] if b.outcome is None else b.outcome,
        "over": b.outcome is not None,
        "outcome": b.outcome,
        "turn": b.turn,
        "needs_switch": b.needs_switch,
        "fought": b.fought,
        "wild": wild,
        "active": b.active_mon.owned_id,
        "team": [
            dict(battle.describe(member, battle.available_moves(member)), active=idx == b.active)
            for idx, member in enumerate(b.team)
        ],
        "mega": battle.mega_view(b),
        "balls": battle.ball_options(b.wild, _catch_context(b, encounter, user_id), inventory),
        "medicine": medicine.battle_inventory(user_id),
        "log": b.log[-LOG_LIMIT:],
        "new_log": b.log[new_from:] if new_from is not None else [],
        "result": state.get("result"),
    }


def get_view(session_id: int, user_id: str) -> dict[str, Any]:
    session = db.query_one("SELECT * FROM battle_session WHERE id = ?", (session_id,))
    if session is None:
        raise HTTPException(status_code=404, detail=i18n.t(db.user_language(user_id), "error.battle_not_found"))
    if session["user_id"] != user_id:
        raise HTTPException(status_code=403, detail=i18n.t(db.user_language(user_id), "error.not_your_battle"))
    b, state, encounter = _load(session)
    return _view(session, b, state, encounter, user_id)


def current_view(user_id: str) -> dict[str, Any] | None:
    release_stale_battles()
    session = ongoing_for_user(user_id)
    if session is None:
        return None
    return get_view(session["id"], user_id)


# ------------------------------------------------------------- Start

def start(encounter_id: int, user_id: str,
          team_ids: list[int] | None = None) -> dict[str, Any]:
    """`team_ids` (optional): fight with exactly these owned Pokemon instead of
    the live party - a one-off lineup that is never written to party_slot."""
    lang = db.user_language(user_id)
    with _lock:
        release_stale_battles()
        encounter = db.query_one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))
        # Fremde Warteschlangen sind unsichtbar (siehe encounter_state.belongs_to).
        if encounter is None or not encounter_state.belongs_to(encounter, user_id):
            raise HTTPException(status_code=404, detail=i18n.t(lang, "error.encounter_not_found"))

        own = ongoing_for_user(user_id)
        if own is not None:
            if own["encounter_id"] == encounter_id:
                return get_view(own["id"], user_id)  # fortsetzen
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.already_fighting_other"))
        ensure_free(encounter_id, user_id)
        if trainer_battle.ongoing_for_user(user_id) is not None:
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.finish_gym_battle_first"))
        if db.query_one("SELECT 1 FROM pvp_battle_session WHERE user_id = ? AND status = 'ongoing'",
                        (user_id,)):
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.finish_pvp_battle_first"))
        if encounter["status"] != "waiting":
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.encounter_unavailable"))
        if encounter_state.is_done(encounter_id, user_id):
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.encounter_already_done"))
        # Own progress: only someone who has already fought encounters a
        # weakened Pokemon.
        encounter = encounter_state.for_user(encounter, user_id)

        rows = battle.owned_selection(user_id, team_ids) if team_ids else battle.party(user_id)  # also triggers HP regeneration
        if not rows:
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.no_team"))
        team = [battle.combatant_from_owned(row) for row in rows]
        alive = [idx for idx, member in enumerate(team) if not member.fainted]
        if not alive:
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.team_fainted"))

        with db.cursor() as cur:
            cur.execute(
                "UPDATE encounter SET status = 'active', claimed_by = ? "
                "WHERE id = ? AND status = 'waiting'",
                (user_id, encounter_id),
            )
            claimed = cur.rowcount == 1
        if not claimed:
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.someone_else_faster"))

        wild = battle.combatant_from_encounter(encounter)
        b = battle.Battle(wild=wild, team=team, active=alive[0], lang=lang)
        b.fought = encounter["current_hp"] is not None
        battle.start_log(b)
        now = _now().isoformat()
        session_id = db.execute(
            "INSERT INTO battle_session (encounter_id, user_id, status, state, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (encounter_id, user_id, "ongoing", json.dumps(_to_state(b)), now, now),
        )
        session = db.query_one("SELECT * FROM battle_session WHERE id = ?", (session_id,))
        encounter = encounter_state.for_user(
            db.query_one("SELECT * FROM encounter WHERE id = ?", (encounter_id,)), user_id
        )
        return _view(session, b, _to_state(b), encounter, user_id, new_from=0)


# ------------------------------------------------------------ Actions

def _catch_context(b: battle.Battle, encounter: dict[str, Any], user_id: str) -> battle.CatchContext:
    """Catch context in manual battle: turn count of this battle, first turn
    only if the Pokemon has never been fought/thrown at (`b.fought` is
    set from the start), Level Ball against the active Pokemon."""
    return battle.catch_context(
        b.wild, user_id, turn=b.turn, first_turn=b.turn == 0 and not b.fought,
        player_level=b.active_mon.level, biome=encounter.get("biome"),
    )


def _throw(b: battle.Battle, state: dict[str, Any], encounter: dict[str, Any],
           user_id: str, ball_key: str | None) -> None:
    from .api import encounters as encounters_api  # local: avoid circular import

    if b.needs_switch:
        raise ValueError(i18n.t(b.lang, "battle.error.must_switch"))
    ball_key = ball_key or "poke_ball"
    if ball_key not in battle.BALLS:
        raise ValueError(i18n.t(b.lang, "error.unknown_ball", ball=ball_key))
    if not encounters_api.consume_ball(ball_key, user_id):
        raise ValueError(i18n.t(b.lang, "error.no_balls_of_kind"))

    b.log.append(i18n.t(b.lang, "battle.log.throw_ball", ball=battle.ball_label(ball_key, b.lang)))
    result = battle.throw_ball(b.wild, user_id, ball_key,
                               context=_catch_context(b, encounter, user_id))
    throw_info = {
        "ball": ball_key, "caught": result.caught, "critical": result.critical,
        "shakes": result.shakes, "probability": round(result.probability, 4),
        "reason": result.reason,
    }
    if result.critical:
        b.log.append(i18n.t(b.lang, "battle.log.critical_catch"))
    if result.caught:
        b.log.append(i18n.t(b.lang, "battle.log.caught", name=b.wild.name))
        # Like in the main games: a catch gives as much EXP as a win,
        # as long as at least one move has been used.
        exp_by_owned = battle.award_battle_exp(b, user_id) if b.fought else {}
        level100 = achievements.check_level100(user_id, b.exp_details, b.lang) if b.fought else []
        for ach in level100:
            b.log.append(i18n.t(b.lang, "battle.log.achievement_unlocked", title=ach["title"]))
            if ach.get("reward"):
                b.log.append(i18n.t(b.lang, "battle.log.achievement_reward", label=ach["reward"]["label"]))
        catch = encounters_api.finalize_catch(encounter, b.wild, user_id, ball_key)
        catch["achievements"] = catch.get("achievements", []) + [a["title"] for a in level100]
        b.outcome = "caught"
        state["result"] = {**throw_info, **catch, "exp": exp_by_owned}
        return

    if result.reason:
        b.log.append(result.reason)
    else:
        b.log.append(i18n.t(b.lang, "battle.log.broke_free", shakes=result.shakes))
    state["last_throw"] = throw_info
    # Missed throw: the wild Pokemon is up.
    battle.run_turn(b, None)


def _finish(session: dict[str, Any], b: battle.Battle, state: dict[str, Any],
            encounter: dict[str, Any], user_id: str) -> None:
    now = _now().isoformat()
    encounter_id = session["encounter_id"]
    if b.outcome == "won":
        won = battle.on_battle_won(user_id, b, encounter_id)
        state["result"] = won
        encounter_state.finish(encounter_id, user_id, "defeated", current_hp=0,
                               level=b.wild.level)
    elif b.outcome == "ran":
        # Zurueck in die Warteschlange; KP (und ggf. Level) bleiben im eigenen
        # Fortschritt erhalten - `_save` schreibt sie gleich mit.
        _release_encounter(encounter_id)
        state["result"] = {"returned_to_queue": True}
    elif b.outcome == "lost":
        # Your active Pokemon fainted: not like the mainline games - the
        # fight ends right there and the wild Pokemon flees, done for this
        # player (not a rematch), same as a roamer escaping.
        encounter_state.finish(encounter_id, user_id, "fled",
                               current_hp=b.wild.current_hp, level=b.wild.level)
        state["result"] = {"escaped": True}
    elif b.outcome == "fled":
        # Roaming legendary ran off (see roaming.py): gone from this player's
        # queue, its damage is kept for the next spawn.
        roaming.escape(encounter_id, user_id, b.wild)
        state["result"] = {"escaped": True}
    # 'caught': finalize_catch hat den Encounter bereits abgeschlossen.
    result = state.get("result") or {}
    battle_history.record(
        user_id, battle_history.KIND_WILD, b.outcome,
        encounter_id=encounter_id,
        opponent=b.wild.name,
        money_earned=(result.get("reward") or result).get("money_earned"),
        caught=b.outcome == "caught",
        exp_details=b.exp_details,
        log=b.log[-LOG_LIMIT:],
        created_at=now,
    )


def act(session_id: int, user_id: str, action: dict[str, Any]) -> dict[str, Any]:
    """Eine Spieler-Aktion: move | switch | ball | mega | item | run."""
    with _lock:
        session = db.query_one("SELECT * FROM battle_session WHERE id = ?", (session_id,))
        if session is None:
            raise HTTPException(status_code=404, detail=i18n.t(db.user_language(user_id), "error.battle_not_found"))
        if session["user_id"] != user_id:
            raise HTTPException(status_code=403, detail=i18n.t(db.user_language(user_id), "error.not_your_battle"))
        if session["status"] != "ongoing":
            raise HTTPException(status_code=409,
                                 detail=i18n.t(db.user_language(user_id), "error.battle_already_over"))

        b, state, encounter = _load(session)
        new_from = len(b.log)
        kind = action.get("action")
        try:
            if kind == "move":
                move_key = action.get("move")
                if move_key not in b.active_mon.moves:
                    raise ValueError(i18n.t(b.lang, "error.move_not_known"))
                battle.run_turn(b, move_key)
            elif kind == "switch":
                target = action.get("target")
                index = next(
                    (idx for idx, member in enumerate(b.team) if member.owned_id == target), -1
                )
                forced = b.needs_switch
                battle.switch_to(b, index, forced=forced)
                if not forced:
                    battle.run_turn(b, None)  # voluntary switch costs the turn
            elif kind == "ball":
                _throw(b, state, encounter, user_id, action.get("ball"))
            elif kind == "mega":
                # Kostet die Runde nicht (siehe battle.mega_evolve).
                battle.mega_evolve(b, b.active_mon)
            elif kind == "item":
                medicine.use_in_battle(b, user_id, action.get("item"), action.get("target"))
            elif kind == "run":
                b.log.append(i18n.t(b.lang, "battle.log.ran_away"))
                b.outcome = "ran"
            else:
                raise HTTPException(status_code=400, detail=i18n.t(b.lang, "error.unknown_action", kind=kind))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if b.outcome is not None:
            _finish(session, b, state, encounter, user_id)
        _save(session, b, state, "ongoing" if b.outcome is None else b.outcome)
        # On 'won', _finish already set the encounter to 0 HP; _save
        # writes the same values, so no contradiction.
        session = db.query_one("SELECT * FROM battle_session WHERE id = ?", (session_id,))
        encounter = db.query_one("SELECT * FROM encounter WHERE id = ?", (session["encounter_id"],))
        return _view(session, b, state, encounter, user_id, new_from=new_from)
