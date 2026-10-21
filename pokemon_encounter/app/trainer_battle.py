"""Gym and league battles against gym leaders, elite four, and champions.

All trainers (data/trainers.json) can be challenged at any time - for this,
your team is LIMITED to the opponent's highest level: whoever is above fights
as a copy at that level (HP proportional, moves above the level are dropped).
Lower Pokemon are not leveled up.

Because the team fights scaled, its battle state lives entirely in session
`state`. Only at battle end are HP (recalculated proportionally), consumed items
and EXP written to owned_pokemon. EXP is earned like in the main games per
defeated opponent Pokemon - also on loss and also for fainted team members (see
`battle.exp_shares`).

Win: prize money plus the achievement for this trainer and the items it pays
out the first time - a TM and balls per gym badge, a Master Ball for a fully
cleared region (achievements.py, rewards.py).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException

from . import achievements, battle, battle_history, db, economy, evs, healing, held_items, i18n, medicine, rewards, spawn, training, trainers
from . import moves as moves_mod

_lock = threading.RLock()

LOG_LIMIT = 200
KIND_PRIZE = {"gym_leader": 1.0, "elite_four": 1.5, "champion": 2.0, "training": 0.0}


def _now() -> datetime:
    return datetime.now().astimezone()


def _scaled_hp(hp: int, from_max: int, to_max: int) -> int:
    if hp <= 0:
        return 0
    return max(1, min(to_max, round(hp * to_max / max(1, from_max))))


def prize_money(trainer: dict[str, Any]) -> int:
    per_level = float(db.get_setting("trainer_money_per_level", 100))
    return int(round(per_level * trainer["max_level"] * KIND_PRIZE[trainer["kind"]] / 10.0)) * 10


# ------------------------------------------------------------- Queries

def ongoing_for_user(user_id: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM trainer_battle_session WHERE user_id = ? AND status = 'ongoing' "
        "ORDER BY id DESC LIMIT 1",
        (user_id,),
    )


def release_stale_battles(now: datetime | None = None) -> int:
    """Abandoned battles expire without consequences for the team."""
    minutes = float(db.get_setting("battle_timeout_minutes", 30) or 0)
    if minutes <= 0:
        return 0
    cutoff = ((now or _now()) - timedelta(minutes=minutes)).isoformat()
    with _lock, db.cursor() as cur:
        cur.execute(
            "UPDATE trainer_battle_session SET status = 'abandoned' "
            "WHERE status = 'ongoing' AND updated_at < ?",
            (cutoff,),
        )
        return cur.rowcount


# ----------------------------------------------------------- State

def mon_state(c: battle.Combatant) -> dict[str, Any]:
    """Battle state of a Pokemon for the session JSON (also used by pvp.py)."""
    return {
        "current_hp": c.current_hp, "status": c.status, "status_counter": c.status_counter,
        "choice_lock": c.choice_lock, "held_item": c.held_item, "mega": c.mega,
        "stat_stages": {k: v for k, v in c.stat_stages.items() if v},
        "volatile": c.volatile,
    }


def apply_mon_state(c: battle.Combatant, s: dict[str, Any]) -> None:
    c.held_item = s.get("held_item")
    # Mega-Form vor den KP: sie bestimmt Art, Typen, Faehigkeit (und damit
    # theoretisch max_hp), auf die die KP geklemmt werden. Braucht das
    # getragene Item, steht deshalb hinter ihm (siehe mega.py).
    battle.restore_mega(c, s.get("mega"))
    c.current_hp = min(int(s["current_hp"]), c.max_hp)
    c.status = s.get("status")
    c.status_counter = s.get("status_counter", 0)
    c.choice_lock = s.get("choice_lock")
    c.stat_stages = {k: 0 for k in battle.STAT_STAGE_KEYS} | (s.get("stat_stages") or {})
    c.volatile = dict(s.get("volatile") or {})


def _trainer_for(trainer_id: str) -> dict[str, Any] | None:
    """Catalog trainer, or the training sparring partner (see training.py)."""
    if trainer_id == training.TRAINER_ID:
        return training.TRAINER
    return trainers.get(trainer_id)


def _label(trainer: dict[str, Any], lang: str) -> str:
    info = trainer_info(trainer, lang)
    return f"{info['title']} {info['name']}"


def _locked_by(trainer: dict[str, Any], done: set[str]) -> dict[str, Any] | None:
    """The unbeaten predecessor that still locks `trainer`, or None when it is
    open. Gyms, Elite Four and champion of a region are fought in catalog
    order, regardless of any gameplay setting; a trainer beaten before (e.g.
    by a save from before this rule) stays open for rematches."""
    if achievements.trainer_key(trainer["id"]) in done:
        return None
    previous = trainers.prerequisite(trainer["id"])
    if previous is None or achievements.trainer_key(previous["id"]) in done:
        return None
    return previous


GENERATION_GYM_DIFFICULTIES = ("generation_limit", "same_generation")


def _generation_eligible(mode: str, trainer_gen: int, species_gen: int) -> bool:
    """Whether a species' generation qualifies to fight a trainer of
    `trainer_gen` (see README "Gym difficulty"): 'generation_limit' allows the
    trainer's own generation or exactly one generation older, 'same_generation'
    only the trainer's own (the league's) generation."""
    if mode == "same_generation":
        return species_gen == trainer_gen
    return trainer_gen - 1 <= species_gen <= trainer_gen


def _opponent(
    trainer: dict[str, Any], species: dict[str, Any], level: int, ability: str | None
) -> battle.Combatant:
    iv = trainer["iv"]
    opp = battle.Combatant(
        species=species, level=level,
        ivs={key: iv for key in ("hp", "atk", "def", "spa", "spd", "spe")},
        trainer=trainer["name"], ability=ability,
    )
    if trainer.get("moves"):
        opp.moves = list(trainer["moves"])
    return opp


def _to_state(
    b: battle.Battle, opponents: list[battle.Combatant], base: dict[str, Any]
) -> dict[str, Any]:
    state = dict(base)
    team_meta = {entry["owned_id"]: entry for entry in base.get("team", [])}
    state.update({
        "team": [
            {"owned_id": m.owned_id, "level": m.level,
             "real_level": team_meta.get(m.owned_id, {}).get("real_level", m.level),
             **mon_state(m)}
            for m in b.team
        ],
        "opponents": [
            # base_ability: eine aktive Mega-Form darf die gespeicherte
            # Grundform-Faehigkeit nicht ueberschreiben (siehe mega.py).
            {"species_id": o.species["id"], "level": o.level, "ability": o.base_ability,
             **mon_state(o)}
            for o in opponents
        ],
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


def _load(
    session: dict[str, Any],
) -> tuple[battle.Battle, list[battle.Combatant], dict[str, Any], dict[str, Any]]:
    state = json.loads(session["state"] or "{}")
    trainer = _trainer_for(session["trainer_id"])
    if trainer is None:
        _close(session, "abandoned")
        raise HTTPException(status_code=410,
                             detail=i18n.t(db.user_language(session["user_id"]), "error.trainer_gone"))

    opponents = []
    for entry in state["opponents"]:
        species = db.query_one("SELECT * FROM species WHERE id = ?", (entry["species_id"],))
        opp = _opponent(trainer, species, entry["level"], entry.get("ability"))
        apply_mon_state(opp, entry)
        opponents.append(opp)

    team = []
    for entry in state["team"]:
        row = db.query_one(
            "SELECT * FROM owned_pokemon WHERE id = ? AND user_id = ?",
            (entry["owned_id"], session["user_id"]),
        )
        if row is None:
            _close(session, "abandoned")
            raise HTTPException(status_code=409,
                                 detail=i18n.t(db.user_language(session["user_id"]), "error.team_changed"))
        member = battle.combatant_from_owned(row, level=entry["level"])
        apply_mon_state(member, entry)
        team.append(member)

    idx = state["opp_index"]
    b = battle.Battle(
        wild=opponents[idx], team=team, active=state["active"], turn=state["turn"],
        log=list(state["log"]), participants=set(state["participants"]),
        needs_switch=state["needs_switch"], fought=state["fought"], outcome=state["outcome"],
        shift_mode=bool(state.get("shift_mode")), shift_offer=bool(state.get("shift_offer")),
        bench=opponents[idx + 1:], knockouts=list(state["knockouts"]),
        mega_used=bool(state.get("mega_used")), foe_mega_used=bool(state.get("foe_mega_used")),
        lang=db.user_language(session["user_id"]),
    )
    return b, opponents, state, trainer


def _close(session: dict[str, Any], status: str) -> None:
    db.execute(
        "UPDATE trainer_battle_session SET status = ?, updated_at = ? WHERE id = ? AND status = 'ongoing'",
        (status, _now().isoformat(), session["id"]),
    )


def _save(session: dict[str, Any], b: battle.Battle, opponents: list[battle.Combatant],
          state: dict[str, Any]) -> None:
    db.execute(
        "UPDATE trainer_battle_session SET state = ?, status = ?, updated_at = ? WHERE id = ?",
        (json.dumps(_to_state(b, opponents, state)), b.outcome or "ongoing",
         _now().isoformat(), session["id"]),
    )


# ------------------------------------------------------------ Ansicht

def trainer_info(trainer: dict[str, Any], lang: str = "en") -> dict[str, Any]:
    return {
        "id": trainer["id"],
        "name": (trainer.get("name_de") if lang == "de" else None) or trainer["name"],
        "title": trainers.title_for(trainer, lang),
        "sprite_url": trainer.get("sprite_url"),
        "kind": trainer["kind"], "stage": trainer.get("stage"), "type": trainer.get("type"),
        "region": trainer["region"], "region_label": trainer["region_label"],
        "max_level": trainer["max_level"], "prize": prize_money(trainer),
    }


def _view(
    session: dict[str, Any], b: battle.Battle, opponents: list[battle.Combatant],
    state: dict[str, Any], trainer: dict[str, Any], new_from: int | None = None,
) -> dict[str, Any]:
    real_levels = {e["owned_id"]: e.get("real_level", e["level"]) for e in state["team"]}
    opp = battle.describe(b.wild)
    opp.update(shiny=False, catchable=False)
    return {
        "id": session["id"],
        "kind": "trainer",
        "trainer": trainer_info(trainer, b.lang),
        "level_cap": trainer["max_level"],
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
            dict(battle.describe(member, battle.available_moves(member)), active=idx == b.active,
                 real_level=real_levels.get(member.owned_id, member.level))
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
    session = db.query_one("SELECT * FROM trainer_battle_session WHERE id = ?", (session_id,))
    if session is None:
        raise HTTPException(status_code=404, detail=i18n.t(db.user_language(user_id), "error.battle_not_found"))
    if session["user_id"] != user_id:
        raise HTTPException(status_code=403, detail=i18n.t(db.user_language(user_id), "error.not_your_battle"))
    return session


def get_view(session_id: int, user_id: str) -> dict[str, Any]:
    session = _session(session_id, user_id)
    b, opponents, state, trainer = _load(session)
    return _view(session, b, opponents, state, trainer)


def current_view(user_id: str) -> dict[str, Any] | None:
    release_stale_battles()
    session = ongoing_for_user(user_id)
    return get_view(session["id"], user_id) if session else None


# ------------------------------------------------------------- Start

def start(trainer_id: str, user_id: str, owned_id: int | None = None,
          training_mode: str = training.DEFAULT_MODE,
          team_ids: list[int] | None = None) -> dict[str, Any]:
    """Start (or resume) a battle against `trainer_id`. Training (see
    training.py) needs `owned_id`: the one Pokemon that spars - from the team
    or the box; `training_mode` picks how the sparring team is chosen. `team_ids` (not training): fight with exactly these
    owned Pokemon instead of the live party - never written to party_slot."""
    lang = db.user_language(user_id)
    with _lock:
        release_stale_battles()
        trainer = _trainer_for(trainer_id)
        if trainer is None:
            raise HTTPException(status_code=404, detail=i18n.t(lang, "error.trainer_not_found"))
        is_training = trainer["kind"] == training.KIND
        if is_training and not training.unlocked(user_id):
            raise HTTPException(status_code=409, detail=i18n.t(
                lang, "error.training_locked", required=training.UNLOCK_COUNT))
        if not is_training:
            blocker = _locked_by(trainer, achievements.unlocked(user_id))
            if blocker is not None:
                raise HTTPException(status_code=409, detail=i18n.t(
                    lang, "error.trainer_locked", name=_label(blocker, lang)))

        own = ongoing_for_user(user_id)
        if own is not None:
            if own["trainer_id"] == trainer_id:
                return get_view(own["id"], user_id)
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.already_fighting_other_trainer"))
        if db.query_one(
            "SELECT 1 FROM battle_session WHERE user_id = ? AND status = 'ongoing'", (user_id,)
        ):
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.finish_wild_battle_first"))
        if db.query_one("SELECT 1 FROM pvp_battle_session WHERE user_id = ? AND status = 'ongoing'",
                        (user_id,)):
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.finish_pvp_battle_first"))

        cap = trainer["max_level"]

        rows = battle.owned_selection(user_id, team_ids) if team_ids else battle.party(user_id)  # zieht auch die KP-Regeneration nach
        if is_training:
            # Sparring: only the chosen Pokemon fights (team or box), against
            # a roster its moves hit super effectively (see training.py).
            row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ? AND user_id = ?",
                               (owned_id, user_id)) if owned_id is not None else None
            if row is None:
                raise HTTPException(status_code=404, detail=i18n.t(lang, "error.pokemon_not_found"))
            rows = [row]
            roster = training.roster(row["species_id"], battle.combatant_from_owned(row).moves,
                                     training_mode)
        elif not rows:
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.no_team"))
        else:
            roster = trainers.resolve_team(trainer)
            if roster is None:
                raise HTTPException(status_code=409, detail=i18n.t(lang, "error.pokedex_still_loading"))

        # Difficulty option (see README "Gym difficulty"): 'generation_limit'
        # restricts the fielded team to Pokemon from the trainer's own
        # generation or exactly one generation older, 'same_generation' to
        # exactly the trainer's (the league's) own generation - anything else
        # sits this fight out. Not for training.
        gen_limited = False
        trainer_gen = None
        gym_difficulty = None if is_training else db.user_gym_difficulty(user_id)
        if gym_difficulty in GENERATION_GYM_DIFFICULTIES:
            trainer_gen = trainers.generation_for_region(trainer["region"]) or 9
            species_ids = [row["species_id"] for row in rows]
            gens = {
                s["id"]: s["generation"]
                for s in db.query(
                    f"SELECT id, generation FROM species WHERE id IN ({','.join('?' * len(species_ids))})",
                    tuple(species_ids),
                )
            }
            eligible = [row for row in rows
                        if _generation_eligible(gym_difficulty, trainer_gen, gens.get(row["species_id"], 1))]
            if not eligible:
                raise HTTPException(status_code=409, detail=i18n.t(
                    lang, "error.no_team_for_gym_difficulty_exact"
                    if gym_difficulty == "same_generation" else "error.no_team_for_gym_difficulty"))
            gen_limited = len(eligible) < len(rows)
            rows = eligible

        team: list[battle.Combatant] = []
        team_meta = []
        scaled = False
        for row in rows:
            real = battle.combatant_from_owned(row)
            member = real
            if row["level"] > cap:
                member = battle.combatant_from_owned(row, level=cap)
                member.current_hp = _scaled_hp(real.current_hp, real.max_hp, member.max_hp)
                scaled = True
            team.append(member)
            team_meta.append({"owned_id": row["id"], "real_level": row["level"]})
        alive = [idx for idx, member in enumerate(team) if not member.fainted]
        if not alive:
            if is_training:
                raise HTTPException(status_code=409, detail=i18n.t(
                    lang, "error.training_fainted", name=team[0].name))
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.team_fainted"))

        opponents = [
            # Sparring partners have no ability - Levitate & co. would undo
            # the weakness to the member's moves they were picked for.
            _opponent(trainer, species, level,
                      None if is_training else spawn.roll_ability(species["id"]))
            for species, level in roster
        ]
        # Every Pokemon of the trainer's team counts as seen for the Pokedex -
        # not the sparring partners of training.
        for species, _level in ([] if is_training else roster):
            db.execute(
                """
                INSERT INTO dex_entry (species_id, user_id, seen_count, first_seen_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(species_id, user_id) DO UPDATE SET
                    seen_count = MAX(seen_count, 1),
                    first_seen_at = COALESCE(first_seen_at, excluded.first_seen_at)
                """,
                (species["id"], user_id, _now().isoformat()),
            )
        shift = db.user_battle_style(user_id) == "shift"
        b = battle.Battle(wild=opponents[0], team=team, active=alive[0], bench=opponents[1:],
                          shift_mode=shift, lang=lang)
        info = trainer_info(trainer, lang)
        b.log.append(i18n.t(lang, "battle.log.trainer_challenge", title=info["title"], name=info["name"]))
        if scaled:
            b.log.append(i18n.t(lang, "battle.log.level_capped", cap=cap))
        if gen_limited:
            log_key = ("battle.log.gym_difficulty_restricted_exact" if gym_difficulty == "same_generation"
                       else "battle.log.gym_difficulty_restricted")
            b.log.append(i18n.t(lang, log_key, generation=trainer_gen))
        b.log.append(i18n.t(lang, "battle.log.trainer_sends_out",
                             trainer=info["name"], name=b.wild.name, level=b.wild.level))
        b.log.append(i18n.t(lang, "battle.log.send_out", name=b.active_mon.name))
        battle.battle_start_effects(b)

        base = {"trainer_id": trainer_id, "team": team_meta, "shift_mode": shift}
        now = _now().isoformat()
        session_id = db.execute(
            "INSERT INTO trainer_battle_session (trainer_id, user_id, status, state, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (trainer_id, user_id, "ongoing", json.dumps(_to_state(b, opponents, base)), now, now),
        )
        session = db.query_one("SELECT * FROM trainer_battle_session WHERE id = ?", (session_id,))
        return _view(session, b, opponents, _to_state(b, opponents, base), trainer, new_from=0)


# ------------------------------------------------------------ Battle End

def _finish(b: battle.Battle, state: dict[str, Any], trainer: dict[str, Any],
            user_id: str) -> None:
    """KP/Items zurueckschreiben (auf das echte Level umgerechnet), EXP fuer
    alle besiegten Gegner-Pokémon verteilen, bei Sieg Preisgeld + Erfolge.
    Training: only EXP (and the level-100 count it may lead to)."""
    is_training = trainer["kind"] == training.KIND
    real_team: list[battle.Combatant] = []
    index_map: dict[int, int] = {}
    for idx, member in enumerate(b.team):
        row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ? AND user_id = ?",
                           (member.owned_id, user_id))
        if row is None:
            continue
        real = battle.combatant_from_owned(row)
        real.current_hp = _scaled_hp(member.current_hp, member.max_hp, real.max_hp)
        real.held_item = member.held_item
        index_map[idx] = len(real_team)
        real_team.append(real)

    info = trainer_info(trainer, b.lang)
    if not is_training:
        b.log.append(i18n.t(b.lang, "battle.log.trainer_defeated_you", title=info["title"], name=info["name"])
                     if b.outcome == "won"
                     else i18n.t(b.lang, "battle.log.trainer_beat_you", name=info["name"]))
    multiplier = float(db.get_setting("trainer_exp_multiplier", 1.5))
    share_all = achievements.share_all_exp(user_id)
    totals: dict[int, int] = {}
    ev_knockouts: list[tuple[dict[str, Any], set[int]]] = []
    for ko in b.knockouts:
        species = db.query_one("SELECT * FROM species WHERE id = ?", (ko["species_id"],))
        loser = _opponent(trainer, species, ko["level"], None)
        participants = {index_map[i] for i in ko["participants"] if i in index_map}
        # Standing at the KO (see `_check_faints`); sessions started before
        # that field existed fall back to the still-standing members now.
        ev_idx = ko.get("ev_participants",
                        [i for i in ko["participants"] if not b.team[i].fainted])
        ev_knockouts.append((species, {index_map[i] for i in ev_idx if i in index_map}))
        for idx, amount in battle.exp_shares(real_team, loser, participants, multiplier,
                                             share_all=share_all).items():
            totals[idx] = totals.get(idx, 0) + amount
    exp_details: list[dict[str, Any]] = []
    exp_by_owned = battle.grant_exp(b.log, real_team, dict(sorted(totals.items())), b.lang,
                                    details=exp_details)
    # Training (see training.py) earns EXP only - no EVs, money, other
    # achievements, Pokedex sync or battle history.
    ev_gains = {} if is_training else evs.grant_battle_evs(b.log, real_team, ev_knockouts, b.lang)
    battle.save_team(real_team)

    # Level-100 milestones can unlock even on a loss - EXP (and with it a
    # level up) is granted per knockout regardless of the battle's outcome.
    new = achievements.check_level100(user_id, exp_details, b.lang)
    result: dict[str, Any] = {"exp": exp_by_owned, "evs": ev_gains, "reward": None}
    if b.outcome == "won" and not is_training:
        earned = prize_money(trainer)
        if earned and held_items.any_holds(real_team, "amulet_coin"):
            earned *= 2
        money = economy.credit(user_id, earned) if earned else economy.balance(user_id)
        b.log.append(i18n.t(b.lang, "battle.log.prize_money", amount=earned))
        result["reward"] = {"money_earned": earned, "money": money}
        new += achievements.on_trainer_defeated(user_id, trainer["id"], b.lang)
        new += achievements.sync_pokedex(user_id, b.lang)
        new += achievements.sync_milestones(user_id, b.lang, ("dex_caught",))
    for ach in new:
        b.log.append(i18n.t(b.lang, "battle.log.achievement_unlocked", title=ach["title"]))
        if ach.get("reward"):
            b.log.append(i18n.t(b.lang, "battle.log.achievement_reward", label=ach["reward"]["label"]))
    result["achievements"] = new
    state["result"] = result
    if is_training:
        return
    # Gym/League battles also end up in the battle history - without
    # encounter_id, which don't exist here.
    battle_history.record(
        user_id, battle_history.KIND_TRAINER, b.outcome,
        opponent=f"{info['title']} {info['name']}",
        money_earned=(result["reward"] or {}).get("money_earned"),
        exp_details=exp_details,
        log=b.log[-LOG_LIMIT:],
    )


# ------------------------------------------------------------ Actions

def act(session_id: int, user_id: str, action: dict[str, Any]) -> dict[str, Any]:
    """A player action: move | switch | mega | item | run (= give up)."""
    with _lock:
        session = _session(session_id, user_id)
        if session["status"] != "ongoing":
            raise HTTPException(status_code=409,
                                 detail=i18n.t(db.user_language(user_id), "error.battle_already_over"))

        b, opponents, state, trainer = _load(session)
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
                raise HTTPException(status_code=400, detail=i18n.t(b.lang, "error.unknown_action", kind=kind))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if b.outcome is not None:
            _finish(b, state, trainer, user_id)
        _save(session, b, opponents, state)
        session = db.query_one("SELECT * FROM trainer_battle_session WHERE id = ?", (session_id,))
        return _view(session, b, opponents, state, trainer, new_from=new_from)


# ------------------------------------------------------------- List

def preview_member(trainer: dict[str, Any], species: dict[str, Any], level: int) -> dict[str, Any]:
    """One roster Pokemon for the gym preview and its summary popup.

    Types, stats and moves are deterministic (the trainer's IVs and
    `moves.moveset_for`), so the player can plan ahead. The ability is not
    included: it is rolled anew when the battle starts (see `start`).
    """
    mon = _opponent(trainer, species, level, None)
    return {
        "species_id": species["id"], "name": mon.name,
        "name_en": species.get("name_en") or species["name"], "name_de": species.get("name_de"),
        "sprite_url": species.get("sprite_url"), "level": level, "types": list(mon.types),
        "base_stats": {key: species[f"base_{key}"] for key in ("hp", "atk", "def", "spa", "spd", "spe")},
        "stats": {"hp": mon.max_hp, **{key: mon.stat(key) for key in ("atk", "def", "spa", "spd", "spe")}},
        "moves": [moves_mod.describe(key) for key in mon.moves],
    }


def overview(user_id: str) -> dict[str, Any]:
    lang = db.user_language(user_id)
    release_stale_battles()
    done = achievements.unlocked(user_id)
    index = trainers.species_index()
    party = db.query(
        "SELECT o.level AS level, s.generation AS generation FROM owned_pokemon o "
        "JOIN species s ON s.id = o.species_id "
        "WHERE o.user_id = ? AND o.party_slot IS NOT NULL",
        (user_id,),
    )
    party_max = max((row["level"] for row in party), default=0)
    party_generations = [row["generation"] for row in party]
    # 'generation_limit' / 'same_generation' (see README "Gym difficulty"):
    # whether the current party has ANY member eligible to fight a trainer of
    # a given generation.
    gym_difficulty = db.user_gym_difficulty(user_id)
    gen_limit_active = gym_difficulty in GENERATION_GYM_DIFFICULTIES
    ongoing = ongoing_for_user(user_id)
    # Freischaltung ist pro Nutzer (siehe achievements.unlocked_generations).
    spawn_unlocked = achievements.unlocked_generations(user_id)
    region_lock = db.user_region_lock(user_id)
    regions = []
    for region in trainers.regions():
        gen = trainers.generation_for_region(region["key"])
        unlocks = achievements.unlock_generation(region["key"], region_lock)
        items = []
        for t in trainers.all_trainers():
            if t["region"] != region["key"]:
                continue
            roster = trainers.resolve_team(t, index)
            blocker = _locked_by(t, done)
            items.append({
                **trainer_info(t, lang),
                # Fought in order: the unbeaten predecessor (None = open).
                "locked_by": _label(blocker, lang) if blocker else None,
                "available": roster is not None,
                "defeated": achievements.trainer_key(t["id"]) in done,
                "scaled": party_max > t["max_level"],
                # 'generation_limit' / 'same_generation': no party member is
                # eligible to fight this trainer (its generation, via the
                # region) at all.
                "gen_restricted": gen_limit_active and gen is not None
                and not any(_generation_eligible(gym_difficulty, gen, g) for g in party_generations),
                # Items fuer den ERSTEN Sieg (Arenaleiter: TM + Baelle, siehe
                # rewards.py) - danach gibt es nur noch Preisgeld.
                "reward": rewards.build(rewards.gym_items(t["id"]), lang),
                # Elite Four members also raise the HP regeneration rate
                # (healing.regen_percent); shown next to the item reward.
                "regen_bonus": healing.ELITE_FOUR_REGEN_BONUS if t["kind"] == "elite_four" else 0,
                # Elite Four members AND Champions also shrink the "heal team"
                # cooldown (healing.cooldown_minutes); shown next to the item reward.
                "cooldown_bonus": healing.ELITE_FOUR_CHAMPION_COOLDOWN_REDUCTION_MINUTES
                if t["kind"] in ("elite_four", "champion") else 0,
                "team": [preview_member(t, sp, level) for sp, level in (roster or [])],
            })
        regions.append({
            **region,
            "generation": gen,
            "spawn_unlocked": gen is not None and gen in spawn_unlocked,
            # The generation that clearing this region adds to the spawn pool
            # under the player's region lock mode (None = it adds none) and
            # whether that generation is open already.
            "unlocks_generation": unlocks,
            "unlock_active": unlocks is not None and unlocks in spawn_unlocked,
            "reward": rewards.build(rewards.region_items(region["key"]), lang),
            "cleared": achievements.region_cleared(user_id, region["key"]),
            "trainers": items,
        })
    return {
        "region_lock": region_lock,
        "regions": regions,
        "defeated": sum(1 for r in regions for t in r["trainers"] if t["defeated"]),
        "total": sum(len(r["trainers"]) for r in regions),
        "party_max_level": party_max,
        "ongoing_trainer_id": ongoing["trainer_id"] if ongoing else None,
    }
