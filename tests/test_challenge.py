"""Tests for the daily challenge quests (app/challenge.py): tier IVs/EVs, rolling,
level-100 opponents while the own team keeps its real level, nothing written to
the real team, retry after a loss and the once-per-day reward.

Run:  python3 tests/test_challenge.py
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-challenge-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import abilities, battle, challenge, db, economy, pvp, seed, spawn  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


def bag(user_id: str, key: str) -> int:
    row = db.query_one("SELECT quantity FROM inventory WHERE item_key = ? AND user_id = ?", (key, user_id))
    return int(row["quantity"]) if row else 0


# ------------------------------------------------------ Tier IVs / EVs
STATS = challenge.STATS
ivs, evs = challenge.roll_stats(1)
check("easy: no IVs, no EVs", set(ivs.values()) == {0} and set(evs.values()) == {0})
ivs, evs = challenge.roll_stats(3)
check("hard: all IVs 31, all EVs 252", set(ivs.values()) == {31} and set(evs.values()) == {252})
rng = random.Random(1)
ok_iv = ok_ev = True
varied = set()
for _ in range(200):
    ivs, evs = challenge.roll_stats(2, rng)
    ok_iv &= sum(1 for v in ivs.values() if v == 31) == 2 and all(0 <= v <= 31 for v in ivs.values())
    ok_ev &= sum(evs.values()) == 250 and max(evs.values()) <= 252
    varied.add(tuple(sorted(ivs.items())))
check("normal: exactly two IVs maxed, others random", ok_iv)
check("normal: 250 EVs distributed", ok_ev)
check("normal: IVs differ between rolls", len(varied) > 50)
check("rewards per slot", [(challenge.REWARDS[s]["money"], challenge.REWARDS[s]["balls"],
                            challenge.REWARDS[s]["candy"]) for s in (1, 2, 3)]
      == [(20000, 10, 0), (50000, 15, 5), (100000, 20, 10)])

# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()
abilities.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")
now = datetime.now().astimezone()
pika = db.query_one("SELECT * FROM species WHERE name = 'pikachu'")
karp = db.query_one("SELECT * FROM species WHERE name = 'magikarp'")


def give(user_id: str, species: dict, level: int, slot: int | None = 1) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, current_hp, "
        "caught_at, party_slot) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, species["id"], level, battle.exp_for_level(level), 0,
         json.dumps({key: 15 for key in spawn.STAT_KEYS}), "hardy",
         battle.stat_hp(species["base_hp"], 15, level), now.isoformat(), slot),
    )


alice_pika = give("alice", pika, 30)
give("bob", karp, 4)
row_before = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (alice_pika,))

# -------------------------------------------------------------- Rolling
data = challenge.overview("alice")
quests = data["quests"]
check("quests are rolled", 1 <= len(quests) <= 3, len(quests))
check("quests come with tiers and rewards", all(q["tier"] == challenge.TIERS[q["slot"]]
      and q["reward"]["money"] == challenge.REWARDS[q["slot"]]["money"] for q in quests))
stored = db.query("SELECT * FROM daily_challenge WHERE user_id = 'alice'")
check("every opponent Pokemon is level 100", all(
    m["level"] == 100 for r in stored for m in json.loads(r["snapshot"])))
check("easy opponent has at most 3 Pokemon", all(
    len(json.loads(r["snapshot"])) <= 3 for r in stored if r["slot"] == 1))
first = challenge.overview("alice")
check("quests stay the same all day", [q["opponent"]["name"] for q in first["quests"]]
      == [q["opponent"]["name"] for q in quests])
check("distinct opponents", len({(r["opponent_kind"], r["opponent_ref"]) for r in stored}) == len(stored))
check("own user is never an opponent", all(r["opponent_ref"] != "alice" for r in stored))
preview_mon = quests[0]["opponent"]["team"][0]
check("opponent preview is gym-style: base stats, computed stats and full move details",
      "base_stats" in preview_mon
      and all(k in preview_mon["stats"] for k in ("hp", "atk", "def", "spa", "spd", "spe"))
      and bool(preview_mon["moves"])
      and all("type" in mv and "power" in mv for mv in preview_mon["moves"]), preview_mon)

# ---------------------------------------------------------------- Battle
slot = quests[0]["slot"]
view = challenge.start("alice", slot)
check("challenge battle is a pvp battle with challenge info",
      view["kind"] == "pvp" and view["challenge"]["slot"] == slot, view["challenge"])
check("own team fights at its real level with full HP",
      view["team"][0]["level"] == 30 and view["team"][0]["current_hp"] == view["team"][0]["max_hp"])
check("opponent fights at level 100", view["wild"]["level"] == 100)
again = challenge.start("alice", slot)
check("starting the same quest again resumes it", again["id"] == view["id"])
other = [q["slot"] for q in quests if q["slot"] != slot]
if other:
    try:
        challenge.start("alice", other[0])
        check("a second quest cannot start during a fight", False)
    except HTTPException as exc:
        check("a second quest cannot start during a fight", exc.status_code == 409, exc.detail)

lost = pvp.act(view["id"], "alice", {"action": "run"})
check("giving up loses the fight without reward", lost["outcome"] == "lost"
      and (lost["result"]["reward"] is None), lost["result"])
q = db.query_one("SELECT * FROM daily_challenge WHERE user_id = 'alice' AND slot = ?", (slot,))
check("lost quest stays open, attempt counted", q["status"] == "open" and q["attempts"] == 1)
row_after = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (alice_pika,))
check("real Pokemon untouched (level, exp, HP)", all(
    row_after[k] == row_before[k] for k in ("level", "exp", "current_hp", "ivs", "evs")))

money_before = economy.balance("alice")
balls_before = {k: bag("alice", k) for k in challenge.BALLS}
candy_before = bag("alice", economy.RARE_CANDY)
retry = challenge.start("alice", slot)
session = db.query_one("SELECT * FROM pvp_battle_session WHERE id = ?", (retry["id"],))
b, opponents, state = pvp._load(session)
b.outcome = "won"
pvp._finish(b, state, session)
result = state["result"]
cfg = challenge.REWARDS[slot]
check("win pays the money", economy.balance("alice") == money_before + cfg["money"], result["reward"])
check("win pays 'balls' of each ball type", all(
    bag("alice", k) == balls_before[k] + cfg["balls"] for k in challenge.BALLS))
check("win pays rare candy", bag("alice", economy.RARE_CANDY) == candy_before + cfg["candy"])
q = db.query_one("SELECT * FROM daily_challenge WHERE user_id = 'alice' AND slot = ?", (slot,))
check("won quest is marked done", q["status"] == "won" and q["attempts"] == 2)
b2 = type("B", (), {"lang": "en", "log": []})()
again = challenge.settle("alice", {"day": q["day"], "slot": slot, "tier": "x"}, "won", b2)
check("reward is paid only once", again["reward"] is None
      and economy.balance("alice") == money_before + cfg["money"])
try:
    challenge.start("alice", slot)
    check("a completed quest cannot be started again", False)
except HTTPException as exc:
    check("a completed quest cannot be started again", exc.status_code == 409, exc.detail)
try:
    challenge.start("alice", 9)
    check("unknown slot is rejected", False)
except HTTPException as exc:
    check("unknown slot is rejected", exc.status_code == 404)
check("overview counts completed quests", challenge.overview("alice")["completed"] == 1)

# Hard tier fights use the stored stats.
if any(r["slot"] == 3 for r in stored):
    hard = json.loads(next(r for r in stored if r["slot"] == 3)["snapshot"])
    check("hard opponents have maxed IVs/EVs", all(
        set(m["ivs"].values()) == {31} and set(m["evs"].values()) == {252} for m in hard))

# --------------------------------------- Temporary team (team_ids), no bug:
# picking a lineup for a quest must never touch the real party/box.
carol = db.upsert_user("carol", "Carol")
carol_pika = give("carol", pika, 25, slot=1)   # lives in the party
carol_karp = give("carol", karp, 12, slot=None)  # sits in the box only
carol_slots_before = {
    row["id"]: row["party_slot"] for row in db.query(
        "SELECT id, party_slot FROM owned_pokemon WHERE user_id = 'carol'")
}
carol_slot = challenge.overview("carol")["quests"][0]["slot"]
view = challenge.start("carol", carol_slot, team_ids=[carol_karp])
check("team_ids fields exactly the given Pokemon, box member included",
      [m["owned_id"] for m in view["team"]] == [carol_karp], view["team"])
carol_slots_mid = {
    row["id"]: row["party_slot"] for row in db.query(
        "SELECT id, party_slot FROM owned_pokemon WHERE user_id = 'carol'")
}
check("party/box membership is untouched while the quest is picked",
      carol_slots_mid == carol_slots_before, carol_slots_mid)
session = db.query_one("SELECT * FROM pvp_battle_session WHERE id = ?", (view["id"],))
b, opponents, state = pvp._load(session)
b.outcome = "lost"
pvp._finish(b, state, session)
carol_slots_after = {
    row["id"]: row["party_slot"] for row in db.query(
        "SELECT id, party_slot FROM owned_pokemon WHERE user_id = 'carol'")
}
check("party/box membership is still untouched after the quest ends",
      carol_slots_after == carol_slots_before, carol_slots_after)

# Wild, gym/trainer and PvP battles take the same team_ids: no party rewrite.
from app import battle_session, trainer_battle  # noqa: E402
dave = db.upsert_user("dave", "Dave")
dave_pika = give("dave", pika, 25, slot=1)
dave_karp = give("dave", karp, 12, slot=None)
dave_slots = {r["id"]: r["party_slot"] for r in db.query(
    "SELECT id, party_slot FROM owned_pokemon WHERE user_id = 'dave'")}
check("owned_selection returns the box member", [r["id"] for r in battle.owned_selection("dave", [dave_karp])] == [dave_karp])
view = pvp.start_vs_user("dave", "carol", team_ids=[dave_karp])
check("pvp start_vs_user honours team_ids", [m["owned_id"] for m in view["team"]] == [dave_karp], view["team"])
check("pvp team_ids leaves party untouched", {r["id"]: r["party_slot"] for r in db.query(
    "SELECT id, party_slot FROM owned_pokemon WHERE user_id = 'dave'")} == dave_slots)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all challenge tests passed")
