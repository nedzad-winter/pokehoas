"""Player vs player - always asynchronous.

You never battle a live co-player, but against an AI-controlled COPY of their
team: at start a snapshot is taken (species, level, moves, IVs, ability, held
item) and written to session state. From then on the battle runs via the same
engine and AI as a gym/league battle (`trainer_battle.py` -> `battle.py`);
the opponent's Pokemon, items and HP are NEVER touched - there is not a single
write path to foreign data in this module.

Your own team takes damage as in gym battles and gets EXP by the same rules
(`trainer_exp_multiplier`), plus deliberately lower prize money on a win
(`pvp_money_per_level`). Unlike gym battles, your own team is NOT level-limited
- opponents are co-players, not fixed league level.

Opponent can be:
* a household member (`start_vs_user`) - counts in the record (`pvp_record`),
* an inserted team code (`start_vs_code`, see `team_code.py`) - does not count
  in the record because there's no account behind it.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException

from . import achievements, battle, battle_history, db, economy, held_items, i18n, medicine
from . import moves as moves_mod, team_code, trainer_battle

_lock = threading.RLock()

LOG_LIMIT = trainer_battle.LOG_LIMIT
CHALLENGE_LEVEL = 100  # daily challenges (challenge.py): the opponent fights at this level


def _now() -> datetime:
    return datetime.now().astimezone()


# ------------------------------------------------------------ Snapshot

def snapshot_for_user(user_id: str) -> list[dict[str, Any]]:
    """Battle copy of a user's current team - read-only.

    HP is intentionally left out: the copy always enters fully healed, otherwise
    the result would depend on how recently the other player played (and the
    opponent couldn't defend themselves).
    """
    rows = db.query(
        "SELECT * FROM owned_pokemon WHERE user_id = ? AND party_slot IS NOT NULL "
        "ORDER BY party_slot",
        (user_id,),
    )
    mons = []
    for row in rows:
        member = battle.combatant_from_owned(row)
        mons.append({
            "species_id": row["species_id"],
            "level": member.level,
            "ivs": {key: int(member.ivs.get(key, 0)) for key in team_code.IV_KEYS},
            # EVs (app/evs.py) are part of the copy - the opponent fights with
            # the team as it was trained. Team codes don't carry them (see
            # team_code.py), an imported team therefore fights with 0 EVs.
            "evs": {key: int(member.evs.get(key, 0)) for key in team_code.IV_KEYS},
            "ability": member.ability,
            "held_item": member.held_item,
            "moves": list(member.moves),
        })
    return mons


def _exportable(mons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim snapshot so it passes code validation.

    Existing Pokemon may have an ability that `species_ability` doesn't (yet)
    know for their species - in code it would be unreadable. Such values are
    dropped on export instead of making the code unusable.
    """
    clean = []
    for mon in mons:
        known = {
            row["ability_key"] for row in db.query(
                "SELECT ability_key FROM species_ability WHERE species_id = ?",
                (mon["species_id"],),
            )
        }
        entry = dict(mon)
        if entry.get("ability") not in known:
            entry["ability"] = None
        clean.append(entry)
    return clean


def team_code_for_user(user_id: str) -> dict[str, Any]:
    lang = db.user_language(user_id)
    mons = _exportable(snapshot_for_user(user_id))
    if not mons:
        raise HTTPException(status_code=409, detail=i18n.t(lang, "error.no_team"))
    user = db.get_user(user_id) or {}
    name = user.get("display_name") or user.get("name")
    return {"code": team_code.encode(mons, name), "team": preview(mons), "owner": name}


def preview(mons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Kurzansicht eines Schnappschusses (Sprite, Name, Level) fuers Frontend."""
    items = []
    for mon in mons:
        species = db.query_one("SELECT * FROM species WHERE id = ?", (mon["species_id"],))
        if species is None:
            continue
        items.append({
            "species_id": species["id"],
            # Beide Namensfelder, damit das Frontend (speciesName) die
            # eingestellte Sprache waehlen kann.
            "name_en": species.get("name_en") or species["name"],
            "name_de": species.get("name_de"),
            "sprite_url": species.get("sprite_url"),
            "level": mon["level"],
            "types": [t for t in (species["type1"], species.get("type2")) if t],
            "held_item": mon.get("held_item"),
            "moves": list(mon.get("moves") or []),
        })
    return items


def full_preview(mons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gym-style preview of a snapshot: types, stats and full move details, like
    `trainer_battle.preview_member` (used for the daily challenge opponent
    preview - see `challenge._quest_view`). Ability is left out on purpose,
    same as the gym preview: it plays no role in planning a lineup here."""
    items = []
    for mon in mons:
        species = db.query_one("SELECT * FROM species WHERE id = ?", (mon["species_id"],))
        if species is None:
            continue
        member = _combatant(mon, "")
        items.append({
            "species_id": species["id"],
            "name_en": species.get("name_en") or species["name"],
            "name_de": species.get("name_de"),
            "sprite_url": species.get("sprite_url"),
            "level": mon["level"],
            "types": list(member.types),
            "base_stats": {key: species[f"base_{key}"] for key in ("hp", "atk", "def", "spa", "spd", "spe")},
            "stats": {"hp": member.max_hp, **{key: member.stat(key) for key in ("atk", "def", "spa", "spd", "spe")}},
            "moves": [moves_mod.describe(key) for key in member.moves],
        })
    return items


def decode_preview(code: str, user_id: str) -> dict[str, Any]:
    lang = db.user_language(user_id)
    try:
        mons, owner = team_code.decode(code, lang)
    except team_code.CodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"owner": owner, "team": preview(mons), "size": len(mons)}


# ---------------------------------------------------------------- Zustand

def _combatant(mon: dict[str, Any], trainer_name: str) -> battle.Combatant:
    species = db.query_one("SELECT * FROM species WHERE id = ?", (mon["species_id"],))
    if species is None:
        raise HTTPException(status_code=409, detail="unknown species in snapshot")
    return battle.Combatant(
        species=species, level=mon["level"], ivs=dict(mon["ivs"]),
        evs=dict(mon.get("evs") or {}),
        moves=list(mon["moves"]), ability=mon.get("ability"),
        held_item=mon.get("held_item"), trainer=trainer_name,
    )


def _challenge_member(row: dict[str, Any]) -> battle.Combatant:
    """Full-HP battle copy of an owned Pokemon for daily challenges: real level,
    IVs, EVs and moves - only the opponent is leveled to 100 (see challenge.py).
    Nothing of this is ever written back to owned_pokemon."""
    member = battle.combatant_from_owned(row)
    member.current_hp = member.max_hp
    member.status = None
    member.status_counter = 0
    return member


def ongoing_for_user(user_id: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM pvp_battle_session WHERE user_id = ? AND status = 'ongoing' "
        "ORDER BY id DESC LIMIT 1",
        (user_id,),
    )


def release_stale_battles(now: datetime | None = None) -> int:
    """Verwaiste Kaempfe verfallen ohne Folgen - wie im Arenakampf."""
    minutes = float(db.get_setting("battle_timeout_minutes", 30) or 0)
    if minutes <= 0:
        return 0
    cutoff = ((now or _now()) - timedelta(minutes=minutes)).isoformat()
    with _lock, db.cursor() as cur:
        cur.execute(
            "UPDATE pvp_battle_session SET status = 'abandoned' "
            "WHERE status = 'ongoing' AND updated_at < ?",
            (cutoff,),
        )
        return cur.rowcount


def _to_state(b: battle.Battle, opponents: list[battle.Combatant],
              base: dict[str, Any]) -> dict[str, Any]:
    state = dict(base)
    state.update({
        "team": [{"owned_id": m.owned_id, **trainer_battle.mon_state(m)} for m in b.team],
        "opponents": [trainer_battle.mon_state(o) for o in opponents],
        "opp_index": len(opponents) - len(b.bench) - 1,
        "active": b.active,
        "participants": sorted(b.participants),
        "needs_switch": b.needs_switch,
        "shift_offer": b.shift_offer,
        "fought": b.fought,
        "turn": b.turn,
        "outcome": b.outcome,
        "mega_used": b.mega_used,
        "foe_mega_used": b.foe_mega_used,
        "knockouts": b.knockouts,
        "log": b.log[-LOG_LIMIT:],
    })
    return state


def _load(session: dict[str, Any]) -> tuple[battle.Battle, list[battle.Combatant], dict[str, Any]]:
    state = json.loads(session["state"] or "{}")
    lang = db.user_language(session["user_id"])

    opponents = []
    for mon, entry in zip(state["snapshot"], state["opponents"]):
        opp = _combatant(mon, session["opponent_name"])
        trainer_battle.apply_mon_state(opp, entry)
        opponents.append(opp)

    team = []
    for entry in state["team"]:
        row = db.query_one(
            "SELECT * FROM owned_pokemon WHERE id = ? AND user_id = ?",
            (entry["owned_id"], session["user_id"]),
        )
        if row is None:
            _close(session, "abandoned")
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.team_changed"))
        member = _challenge_member(row) if state.get("challenge") else battle.combatant_from_owned(row)
        trainer_battle.apply_mon_state(member, entry)
        team.append(member)

    idx = state["opp_index"]
    b = battle.Battle(
        wild=opponents[idx], team=team, active=state["active"], turn=state["turn"],
        log=list(state["log"]), participants=set(state["participants"]),
        needs_switch=state["needs_switch"], fought=state["fought"], outcome=state["outcome"],
        shift_mode=bool(state.get("shift_mode")), shift_offer=bool(state.get("shift_offer")),
        bench=opponents[idx + 1:], knockouts=list(state["knockouts"]),
        mega_used=bool(state.get("mega_used")), foe_mega_used=bool(state.get("foe_mega_used")),
        lang=lang,
    )
    return b, opponents, state


def _close(session: dict[str, Any], status: str) -> None:
    db.execute(
        "UPDATE pvp_battle_session SET status = ?, updated_at = ? WHERE id = ? AND status = 'ongoing'",
        (status, _now().isoformat(), session["id"]),
    )


def _save(session: dict[str, Any], b: battle.Battle, opponents: list[battle.Combatant],
          state: dict[str, Any]) -> None:
    db.execute(
        "UPDATE pvp_battle_session SET state = ?, status = ?, updated_at = ? WHERE id = ?",
        (json.dumps(_to_state(b, opponents, state)), b.outcome or "ongoing",
         _now().isoformat(), session["id"]),
    )


# ----------------------------------------------------------------- Ansicht

def _view(session: dict[str, Any], b: battle.Battle, opponents: list[battle.Combatant],
          state: dict[str, Any], new_from: int | None = None) -> dict[str, Any]:
    opp = battle.describe(b.wild)
    opp.update(shiny=False, catchable=False)
    return {
        "id": session["id"],
        "kind": "pvp",
        "challenge": state.get("challenge"),
        "opponent": {
            "user_id": session["opponent_user_id"],
            "name": session["opponent_name"],
            "from_code": session["opponent_user_id"] is None,
        },
        "status": session["status"] if b.outcome is None else b.outcome,
        "over": b.outcome is not None,
        "outcome": b.outcome,
        "turn": b.turn,
        "needs_switch": b.needs_switch,
        "shift_offer": b.shift_offer,
        "fought": b.fought,
        "wild": opp,
        "opponents": [{"fainted": o.fainted, "active": o is b.wild} for o in opponents],
        "active": b.active_mon.owned_id,
        "team": [
            dict(battle.describe(member, battle.available_moves(member)), active=idx == b.active)
            for idx, member in enumerate(b.team)
        ],
        "mega": battle.mega_view(b),
        "balls": [],
        "medicine": medicine.battle_inventory(session["user_id"]),
        "log": b.log[-LOG_LIMIT:],
        "new_log": b.log[new_from:] if new_from is not None else [],
        "result": state.get("result"),
    }


def _session(session_id: int, user_id: str) -> dict[str, Any]:
    session = db.query_one("SELECT * FROM pvp_battle_session WHERE id = ?", (session_id,))
    if session is None:
        raise HTTPException(status_code=404,
                             detail=i18n.t(db.user_language(user_id), "error.battle_not_found"))
    if session["user_id"] != user_id:
        raise HTTPException(status_code=403,
                             detail=i18n.t(db.user_language(user_id), "error.not_your_battle"))
    return session


def get_view(session_id: int, user_id: str) -> dict[str, Any]:
    session = _session(session_id, user_id)
    b, opponents, state = _load(session)
    return _view(session, b, opponents, state)


def current_view(user_id: str) -> dict[str, Any] | None:
    release_stale_battles()
    session = ongoing_for_user(user_id)
    return get_view(session["id"], user_id) if session else None


# ------------------------------------------------------------------- Start

def _start(user_id: str, mons: list[dict[str, Any]], opponent_user_id: str | None,
           opponent_name: str, challenge: dict[str, Any] | None = None,
           team_ids: list[int] | None = None) -> dict[str, Any]:
    """`challenge` ({day, slot, tier}, see challenge.py) makes this a daily
    challenge: the opponent fights at level 100, the own team keeps its real
    level, and nothing is saved for the own team (no EXP, no HP cost).
    `team_ids`: fight with exactly these owned Pokemon
    instead of the live party - a one-off lineup for this fight that is never
    written to party_slot, so picking it doesn't change the real team."""
    lang = db.user_language(user_id)
    with _lock:
        release_stale_battles()
        trainer_battle.release_stale_battles()
        own = ongoing_for_user(user_id)
        if own is not None:
            # Same opponent -> continue fighting, different -> finish old battle first.
            same_challenge = json.loads(own["state"] or "{}").get("challenge") == challenge
            if (own["opponent_user_id"] == opponent_user_id and own["opponent_name"] == opponent_name
                    and same_challenge):
                return get_view(own["id"], user_id)
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.pvp_already_fighting",
                                                                name=own["opponent_name"]))
        if trainer_battle.ongoing_for_user(user_id) is not None:
            raise HTTPException(status_code=409,
                                 detail=i18n.t(lang, "error.already_fighting_other_trainer"))
        if db.query_one(
            "SELECT 1 FROM battle_session WHERE user_id = ? AND status = 'ongoing'", (user_id,)
        ):
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.finish_wild_battle_first"))
        if not mons:
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.pvp_opponent_no_team"))

        # also triggers HP regeneration
        rows = battle.owned_selection(user_id, team_ids) if team_ids else battle.party(user_id)
        if not rows:
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.no_team"))
        if challenge:
            # Full-HP copies at the real level: the real team's HP does not matter.
            team = [_challenge_member(row) for row in rows]
        else:
            team = [battle.combatant_from_owned(row) for row in rows]
        alive = [idx for idx, member in enumerate(team) if not member.fainted]
        if not alive:
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.team_fainted"))

        opponents = [_combatant(mon, opponent_name) for mon in mons]
        shift = db.user_battle_style(user_id) == "shift"
        b = battle.Battle(wild=opponents[0], team=team, active=alive[0],
                          bench=opponents[1:], shift_mode=shift, lang=lang)
        if challenge:
            b.log.append(i18n.t(lang, "battle.log.challenge_start", name=opponent_name,
                                 level=CHALLENGE_LEVEL))
        else:
            b.log.append(i18n.t(lang, "battle.log.pvp_challenge", name=opponent_name))
        b.log.append(i18n.t(lang, "battle.log.trainer_sends_out", trainer=opponent_name,
                             name=b.wild.name, level=b.wild.level))
        b.log.append(i18n.t(lang, "battle.log.send_out", name=b.active_mon.name))
        battle.battle_start_effects(b)

        base = {"snapshot": mons, "shift_mode": shift}
        if challenge:
            base.update(challenge=challenge)
        now = _now().isoformat()
        session_id = db.execute(
            "INSERT INTO pvp_battle_session (user_id, opponent_user_id, opponent_name, status, "
            "state, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (user_id, opponent_user_id, opponent_name, "ongoing",
             json.dumps(_to_state(b, opponents, base)), now, now),
        )
        session = db.query_one("SELECT * FROM pvp_battle_session WHERE id = ?", (session_id,))
        return _view(session, b, opponents, _to_state(b, opponents, base), new_from=0)


def start_vs_user(user_id: str, opponent_user_id: str,
                  team_ids: list[int] | None = None) -> dict[str, Any]:
    lang = db.user_language(user_id)
    if opponent_user_id == user_id:
        raise HTTPException(status_code=400, detail=i18n.t(lang, "error.pvp_self"))
    opponent = db.get_user(opponent_user_id)
    if opponent is None:
        raise HTTPException(status_code=404, detail=i18n.t(lang, "error.user_not_found"))
    name = opponent["display_name"] or opponent["name"]
    return _start(user_id, snapshot_for_user(opponent_user_id), opponent_user_id, name,
                  team_ids=team_ids)


def start_vs_code(user_id: str, code: str,
                  team_ids: list[int] | None = None) -> dict[str, Any]:
    lang = db.user_language(user_id)
    try:
        mons, owner = team_code.decode(code, lang)
    except team_code.CodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _start(user_id, mons, None, owner or i18n.t(lang, "pvp.code_opponent"),
                  team_ids=team_ids)


# --------------------------------------------------------------- Battle End

def _prize_money(mons: list[dict[str, Any]]) -> int:
    per_level = float(db.get_setting("pvp_money_per_level", 40))
    top = max((int(mon["level"]) for mon in mons), default=0)
    return int(round(per_level * top / 10.0)) * 10


def record_of(user_id: str, opponent_user_id: str) -> dict[str, Any]:
    row = db.query_one(
        "SELECT wins, losses, last_at FROM pvp_record WHERE user_id = ? AND opponent_user_id = ?",
        (user_id, opponent_user_id),
    ) or {}
    return {"wins": int(row.get("wins") or 0), "losses": int(row.get("losses") or 0),
            "last_at": row.get("last_at")}


def _bump_record(user_id: str, opponent_user_id: str, outcome: str) -> None:
    won = 1 if outcome == "won" else 0
    db.execute(
        """
        INSERT INTO pvp_record (user_id, opponent_user_id, wins, losses, last_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(user_id, opponent_user_id) DO UPDATE SET
            wins = wins + excluded.wins,
            losses = losses + excluded.losses,
            last_at = excluded.last_at
        """,
        (user_id, opponent_user_id, won, 1 - won, _now().isoformat()),
    )


def _finish(b: battle.Battle, state: dict[str, Any], session: dict[str, Any]) -> None:
    """Save HP/items of OWN team, distribute EXP, prize money on victory.

    Nothing is saved for the opponent's team - it only exists as a snapshot
    in the session state. The own team then always leaves the duel fully healed:
    unlike a wild battle or gym/league, a player battle is just sparring against
    an AI copy and should have no permanent HP costs (see README, section
    "Player vs Player").
    """
    user_id = session["user_id"]
    name = session["opponent_name"]
    b.log.append(i18n.t(b.lang, "battle.log.pvp_won" if b.outcome == "won"
                        else "battle.log.pvp_lost", name=name))

    if state.get("challenge"):
        # Daily challenge: level-100 copies, so no EXP and nothing to write
        # back - only the quest bookkeeping and its reward (challenge.py).
        from . import challenge  # lazy: challenge.py imports this module
        state["result"] = challenge.settle(user_id, state["challenge"], b.outcome or "lost", b)
        battle_history.record(
            user_id, battle_history.KIND_PVP, b.outcome,
            opponent=name,
            money_earned=(state["result"]["reward"] or {}).get("money_earned"),
            log=b.log[-LOG_LIMIT:],
        )
        return

    multiplier = float(db.get_setting("trainer_exp_multiplier", 1.5))
    share_all = achievements.share_all_exp(user_id)
    totals: dict[int, int] = {}
    for ko in b.knockouts:
        species = db.query_one("SELECT * FROM species WHERE id = ?", (ko["species_id"],))
        loser = battle.Combatant(species=species, level=ko["level"],
                                 ivs={key: 15 for key in team_code.IV_KEYS})
        for idx, amount in battle.exp_shares(b.team, loser, set(ko["participants"]),
                                             multiplier, share_all=share_all).items():
            totals[idx] = totals.get(idx, 0) + amount
    exp_details: list[dict[str, Any]] = []
    exp_by_owned = battle.grant_exp(b.log, b.team, dict(sorted(totals.items())), b.lang,
                                    details=exp_details)
    new_achievements = achievements.check_level100(user_id, exp_details, b.lang)
    for ach in new_achievements:
        b.log.append(i18n.t(b.lang, "battle.log.achievement_unlocked", title=ach["title"]))
        if ach.get("reward"):
            b.log.append(i18n.t(b.lang, "battle.log.achievement_reward", label=ach["reward"]["label"]))
    for member in b.team:
        member.current_hp = member.max_hp
        member.status = None
        member.status_counter = 0
    battle.save_team(b.team)
    b.log.append(i18n.t(b.lang, "battle.log.pvp_team_healed"))

    result: dict[str, Any] = {"exp": exp_by_owned, "reward": None, "achievements": new_achievements}
    if b.outcome == "won":
        earned = _prize_money(state["snapshot"])
        if earned and held_items.any_holds(b.team, "amulet_coin"):
            earned *= 2
        money = economy.credit(user_id, earned) if earned else economy.balance(user_id)
        if earned:
            b.log.append(i18n.t(b.lang, "battle.log.prize_money", amount=earned))
        result["reward"] = {"money_earned": earned, "money": money}
    if session["opponent_user_id"]:
        _bump_record(user_id, session["opponent_user_id"], b.outcome or "lost")
        result["record"] = record_of(user_id, session["opponent_user_id"])
    state["result"] = result

    battle_history.record(
        user_id, battle_history.KIND_PVP, b.outcome,
        opponent=name,
        money_earned=(result["reward"] or {}).get("money_earned"),
        exp_details=exp_details,
        log=b.log[-LOG_LIMIT:],
    )


# ---------------------------------------------------------------- Actions

def act(session_id: int, user_id: str, action: dict[str, Any]) -> dict[str, Any]:
    """A player action: move | switch | mega | item | run (= give up)."""
    with _lock:
        session = _session(session_id, user_id)
        if session["status"] != "ongoing":
            raise HTTPException(status_code=409,
                                 detail=i18n.t(db.user_language(user_id), "error.battle_already_over"))

        b, opponents, state = _load(session)
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
                free = battle.switch_is_free(b)
                battle.switch_to(b, index, forced=b.needs_switch)
                if not free:
                    battle.run_turn(b, None)
            elif kind == "mega":
                # Kostet die Runde nicht (siehe battle.mega_evolve).
                battle.mega_evolve(b, b.active_mon)
            elif kind == "run":
                b.log.append(i18n.t(b.lang, "battle.log.give_up"))
                b.outcome = "lost"
            elif kind == "ball":
                raise ValueError(i18n.t(b.lang, "battle.error.cant_catch_trainer_pokemon"))
            elif kind == "item":
                medicine.use_in_battle(b, user_id, action.get("item"), action.get("target"))
            else:
                raise HTTPException(status_code=400,
                                     detail=i18n.t(b.lang, "error.unknown_action", kind=kind))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if b.outcome is not None:
            _finish(b, state, session)
        _save(session, b, opponents, state)
        session = db.query_one("SELECT * FROM pvp_battle_session WHERE id = ?", (session_id,))
        return _view(session, b, opponents, state, new_from=new_from)


# ------------------------------------------------------------------- List

def opponents_overview(user_id: str) -> dict[str, Any]:
    """Challengeable household members along with own record against them."""
    release_stale_battles()
    ongoing = ongoing_for_user(user_id)
    items = []
    for user in db.eligible_users():
        if user["id"] == user_id:
            continue
        team = snapshot_for_user(user["id"])
        items.append({
            "user_id": user["id"],
            "display_name": user["display_name"] or user["name"],
            "team_size": len(team),
            "max_level": max((mon["level"] for mon in team), default=0),
            "prize": _prize_money(team) if team else 0,
            "record": record_of(user_id, user["id"]),
            "team": preview(team),
        })
    return {
        "opponents": items,
        "ongoing_opponent_id": ongoing["opponent_user_id"] if ongoing else None,
        "ongoing_battle_id": ongoing["id"] if ongoing else None,
    }
