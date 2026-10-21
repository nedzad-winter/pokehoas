"""Daily challenge quests: up to three level-100 fights per day.

Every user gets three quests per (local) day - easy, normal, hard. Each one is
an AI-controlled team drawn at random from the pool of all gym leaders, elite
four members, champions (`data/trainers.json`) and the other household users'
teams. The opponent fights at level 100; your own team keeps its real level,
IVs, EVs and moves, just as a full-HP copy (see `pvp._challenge_member`);
nothing of the fight is written back to your real Pokemon (no EXP, no HP cost).

    easy    all IVs and EVs 0
    normal  random IVs of which two are maxed (31), 250 EVs spread randomly
    hard    all IVs 31 and all EVs 252

Team size: the easy opponent brings at most 3 Pokemon (see TEAM_LIMITS).

The battle itself runs through `pvp.py` (state key `challenge`), this module
rolls the quests, previews them and settles the reward when a fight ends. A
lost quest can be retried; the reward is paid once per quest per day.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException

from . import achievements, db, economy, i18n, moves as moves_mod, pvp, rewards, spawn, team_code, trainer_battle, trainers

LEVEL = pvp.CHALLENGE_LEVEL
STATS = team_code.IV_KEYS
SLOTS = (1, 2, 3)
TIERS = {1: "easy", 2: "normal", 3: "hard"}
BALLS = ("poke_ball", "great_ball", "ultra_ball")
RARE_CANDY = economy.RARE_CANDY

# slot -> reward: money, count of EACH ball type, Rare Candy
REWARDS: dict[int, dict[str, int]] = {
    1: {"money": 20000, "balls": 10, "candy": 0},
    2: {"money": 50000, "balls": 15, "candy": 5},
    3: {"money": 100000, "balls": 20, "candy": 10},
}
# slot -> max opponent team size (slots not listed keep the full team)
TEAM_LIMITS: dict[int, int] = {1: 3}
NORMAL_MAXED_STATS = 2
NORMAL_EV_TOTAL = 250
MAX_IV = 31
MAX_EV = 252


def _now() -> datetime:
    return datetime.now().astimezone()


def today() -> str:
    return _now().date().isoformat()


def next_reset() -> str:
    """ISO timestamp of the next local midnight, when new quests are rolled."""
    now = _now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return midnight.isoformat()


# ---------------------------------------------------------------- Rewards

def reward_items(slot: int) -> list[dict[str, Any]]:
    cfg = REWARDS[slot]
    items = [{"key": key, "quantity": cfg["balls"]} for key in BALLS]
    if cfg["candy"]:
        items.append({"key": RARE_CANDY, "quantity": cfg["candy"]})
    return items


def reward_view(slot: int, lang: str) -> dict[str, Any]:
    return {"money": REWARDS[slot]["money"], "items": reward_items(slot),
            "label": rewards.label(reward_items(slot), lang)}


# ------------------------------------------------------------ IVs and EVs

def roll_stats(slot: int, rng: random.Random | Any = random) -> tuple[dict[str, int], dict[str, int]]:
    """(ivs, evs) for one opponent Pokemon of the given quest slot."""
    if slot == 1:
        return dict.fromkeys(STATS, 0), dict.fromkeys(STATS, 0)
    if slot == 3:
        return dict.fromkeys(STATS, MAX_IV), dict.fromkeys(STATS, MAX_EV)
    ivs = {key: rng.randint(0, MAX_IV - 1) for key in STATS}
    for key in rng.sample(STATS, NORMAL_MAXED_STATS):
        ivs[key] = MAX_IV
    evs = dict.fromkeys(STATS, 0)
    for _ in range(NORMAL_EV_TOTAL):
        evs[rng.choice(STATS)] += 1
    return ivs, evs


def _fill_moves(species: dict[str, Any], moves: list[str]) -> list[str]:
    """Keep the given moves, fill free slots (up to 4) with the level-100 set."""
    result = list(moves)[:4]
    for key in moves_mod.moveset_for(species, LEVEL):
        if len(result) >= 4:
            break
        if key not in result:
            result.append(key)
    return result


# --------------------------------------------------------------- Rolling

def _trainer_team(trainer: dict[str, Any], slot: int, index: dict[str, Any],
                  rng: random.Random | Any) -> list[dict[str, Any]] | None:
    roster = trainers.resolve_team(trainer, index)
    if roster is None:
        return None
    mons = []
    for species, _level in roster[:TEAM_LIMITS.get(slot, len(roster))]:
        ivs, evs = roll_stats(slot, rng)
        mons.append({
            "species_id": species["id"], "level": LEVEL, "ivs": ivs, "evs": evs,
            "ability": spawn.roll_ability(species["id"]), "held_item": None,
            "moves": _fill_moves(species, []),
        })
    return mons


def _user_team(user_id: str, slot: int, rng: random.Random | Any) -> list[dict[str, Any]]:
    mons = []
    snapshot = pvp.snapshot_for_user(user_id)
    for mon in snapshot[:TEAM_LIMITS.get(slot, len(snapshot))]:
        species = db.query_one("SELECT * FROM species WHERE id = ?", (mon["species_id"],))
        if species is None:
            continue
        ivs, evs = roll_stats(slot, rng)
        mons.append({**mon, "level": LEVEL, "ivs": ivs, "evs": evs,
                     "moves": _fill_moves(species, mon.get("moves") or [])})
    return mons


def _pool(user_id: str) -> list[tuple[str, str, str]]:
    """(kind, ref, fallback name) of everything a quest can be drawn from."""
    pool = [("trainer", t["id"], t["name"]) for t in trainers.all_trainers()]
    for user in db.eligible_users():
        if user["id"] != user_id:
            pool.append(("user", user["id"], user["display_name"] or user["name"]))
    return pool


def roll(user_id: str, day: str, rng: random.Random | Any = random) -> None:
    """Create today's quests for the user (idempotent per slot)."""
    rows = db.query("SELECT slot, opponent_kind, opponent_ref FROM daily_challenge "
                    "WHERE user_id = ? AND day = ?", (user_id, day))
    missing = [slot for slot in SLOTS if slot not in {row["slot"] for row in rows}]
    if not missing:
        return
    index = trainers.species_index()
    used = {(row["opponent_kind"], row["opponent_ref"]) for row in rows}
    pool = [entry for entry in _pool(user_id) if entry[:2] not in used]
    rng.shuffle(pool)
    for slot in missing:
        while pool:
            kind, ref, name = pool.pop()
            if kind == "trainer":
                team = _trainer_team(trainers.get(ref), slot, index, rng)
            else:
                team = _user_team(ref, slot, rng)
            if team:
                break
        else:
            break  # nothing usable left (Pokedex still seeding / empty pool)
        db.execute(
            "INSERT OR IGNORE INTO daily_challenge (user_id, day, slot, opponent_kind, "
            "opponent_ref, opponent_name, snapshot) VALUES (?,?,?,?,?,?,?)",
            (user_id, day, slot, kind, ref, name, json.dumps(team)),
        )


# ------------------------------------------------------------------ View

def _opponent_name(row: dict[str, Any], lang: str) -> str:
    if row["opponent_kind"] == "trainer":
        trainer = trainers.get(row["opponent_ref"])
        if trainer:
            info = trainer_battle.trainer_info(trainer, lang)
            return f"{info['title']} {info['name']}"
    elif row["opponent_kind"] == "user":
        user = db.get_user(row["opponent_ref"])
        if user:
            return user["display_name"] or user["name"]
    return row["opponent_name"]


def _quest_view(row: dict[str, Any], lang: str) -> dict[str, Any]:
    slot = row["slot"]
    snapshot = json.loads(row["snapshot"])
    return {
        "slot": slot, "tier": TIERS[slot], "status": row["status"],
        "attempts": row["attempts"], "completed_at": row["completed_at"],
        "opponent": {"name": _opponent_name(row, lang), "kind": row["opponent_kind"],
                     "team": pvp.full_preview(snapshot)},
        "reward": reward_view(slot, lang),
    }


def overview(user_id: str) -> dict[str, Any]:
    lang = db.user_language(user_id)
    pvp.release_stale_battles()
    day = today()
    with pvp._lock:
        roll(user_id, day)
    rows = db.query(
        "SELECT * FROM daily_challenge WHERE user_id = ? AND day = ? ORDER BY slot", (user_id, day))
    ongoing = pvp.ongoing_for_user(user_id)
    ongoing_challenge = json.loads(ongoing["state"] or "{}").get("challenge") if ongoing else None
    return {
        "day": day, "resets_at": next_reset(), "level": LEVEL,
        "quests": [_quest_view(row, lang) for row in rows],
        "completed": sum(1 for row in rows if row["status"] == "won"),
        "ongoing_slot": ongoing_challenge["slot"] if ongoing_challenge else None,
    }


# ----------------------------------------------------------------- Battle

def start(user_id: str, slot: int, team_ids: list[int] | None = None) -> dict[str, Any]:
    """`team_ids` (optional): fight with exactly these owned Pokemon instead
    of the live party - a one-off lineup for just this fight (see
    `pvp._start`), so picking it doesn't permanently change the real team."""
    lang = db.user_language(user_id)
    if slot not in SLOTS:
        raise HTTPException(status_code=404, detail=i18n.t(lang, "error.challenge_not_found"))
    day = today()
    with pvp._lock:
        roll(user_id, day)
        row = db.query_one(
            "SELECT * FROM daily_challenge WHERE user_id = ? AND day = ? AND slot = ?",
            (user_id, day, slot))
        if row is None:
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.pokedex_still_loading"))
        if row["status"] == "won":
            raise HTTPException(status_code=409, detail=i18n.t(lang, "error.challenge_done"))
        return pvp._start(user_id, json.loads(row["snapshot"]), None,
                          _opponent_name(row, lang),
                          challenge={"day": day, "slot": slot, "tier": TIERS[slot]},
                          team_ids=team_ids)


def settle(user_id: str, challenge: dict[str, Any], outcome: str, b: Any) -> dict[str, Any]:
    """Book a finished challenge fight: count the attempt, on the first win of
    the quest pay the reward. Returns the battle `result` dict."""
    slot, day = int(challenge["slot"]), challenge["day"]
    result: dict[str, Any] = {"exp": {}, "reward": None, "achievements": [],
                              "challenge": {"slot": slot, "tier": TIERS[slot], "won": False}}
    db.execute("UPDATE daily_challenge SET attempts = attempts + 1 "
               "WHERE user_id = ? AND day = ? AND slot = ?", (user_id, day, slot))
    if outcome != "won":
        return result
    with db.cursor() as cur:
        cur.execute(
            "UPDATE daily_challenge SET status = 'won', completed_at = ? "
            "WHERE user_id = ? AND day = ? AND slot = ? AND status = 'open'",
            (_now().isoformat(), user_id, day, slot))
        marked = cur.rowcount
    if not marked:
        return result  # already paid out (e.g. a second session of the same quest)
    cfg = REWARDS[slot]
    money = economy.credit(user_id, cfg["money"])
    rewards.grant(user_id, reward_items(slot))
    label = rewards.label(reward_items(slot), b.lang)
    b.log.append(i18n.t(b.lang, "battle.log.prize_money", amount=cfg["money"]))
    b.log.append(i18n.t(b.lang, "battle.log.challenge_reward", label=label))
    result["reward"] = {"money_earned": cfg["money"], "money": money,
                        "items": reward_items(slot), "label": label}
    result["challenge"]["won"] = True
    new = achievements.sync_milestones(user_id, b.lang, ("streak",))
    for ach in new:
        b.log.append(i18n.t(b.lang, "battle.log.achievement_unlocked", title=ach["title"]))
        if ach.get("reward"):
            b.log.append(i18n.t(b.lang, "battle.log.achievement_reward", label=ach["reward"]["label"]))
    result["achievements"] = new
    return result
