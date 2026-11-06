"""Tests for the one-battle-at-a-time rule across wild/gym-league/PvP battles
(battle_session.py / trainer_battle.py / pvp.py, all guarded by
`battle_timeout_minutes`): starting one battle type must only be blocked by a
genuinely ongoing battle of ANOTHER type, never by a session that is already
past the timeout but hasn't been swept into 'abandoned' yet by its own
module (see db.has_fresh_ongoing_session). Each battle type only sweeps its
OWN table on `start()`/`current_view()`, so a stale row left behind by, say,
a dropped wild battle must not lock the player out of gym/league or PvP
battles (or daily challenges, which run through pvp.py) until that other
module happens to be visited too.

Run:  python3 tests/test_battle_lock_timeout.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-battle-lock-timeout-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, battle_session, db, pvp, seed, spawn, trainer_battle, trainers  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


def expect_ok(label: str, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
        check(label, True)
    except HTTPException as exc:
        check(label, False, f"{exc.status_code}: {exc.detail}")


def expect_blocked(label: str, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
        check(label, False, "keine Exception")
    except HTTPException as exc:
        check(label, exc.status_code == 409, f"{exc.status_code}: {exc.detail}")


# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()

# A single gym with no predecessor (see `a92fae8`: trainers of a region must
# be beaten in order) so trainer_battle.start never fails for that reason.
trainers._raw = {
    "regions": [{"key": "kanto", "label": "Kanto"}],
    "trainers": [
        {"id": "test_gym", "kind": "gym_leader", "region": "kanto",
         "name": "Test Leader", "team": [["magikarp", 5]]},
    ],
}
trainers._trainers = None

alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")
now = datetime.now().astimezone()
pika = db.query_one("SELECT * FROM species WHERE name = 'pikachu'")
karp = db.query_one("SELECT * FROM species WHERE name = 'magikarp'")


def give(user_id: str, species: dict, level: int = 25) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "caught_at, party_slot) VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, species["id"], level, battle.exp_for_level(level), 0,
         json.dumps({k: 20 for k in spawn.STAT_KEYS}), "hardy", now.isoformat(), 1),
    )


give("alice", pika)
give("bob", karp)


def make_encounter() -> int:
    return spawn.create_encounter(karp, source_entity="test.trigger")


def set_session(table: str, status: str, age_minutes: float, **extra) -> int:
    """Insert a raw row for alice directly (bypassing the owning module), as if
    an earlier battle of that type was left behind - exactly the state a
    dropped connection or a closed tab would leave, without going through
    that module's own start()/act()/current_view()."""
    updated_at = (now - timedelta(minutes=age_minutes)).isoformat()
    if table == "battle_session":
        return db.execute(
            "INSERT INTO battle_session (encounter_id, user_id, status, state, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (extra["encounter_id"], "alice", status, "{}", updated_at, updated_at),
        )
    if table == "trainer_battle_session":
        return db.execute(
            "INSERT INTO trainer_battle_session (trainer_id, user_id, status, state, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("test_gym", "alice", status, "{}", updated_at, updated_at),
        )
    if table == "pvp_battle_session":
        return db.execute(
            "INSERT INTO pvp_battle_session (user_id, opponent_user_id, opponent_name, status, state, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("alice", "bob", "Bob", status, "{}", updated_at, updated_at),
        )
    raise ValueError(table)


def clear(table: str) -> None:
    db.execute(f"DELETE FROM {table} WHERE user_id = 'alice'")  # noqa: S608 - fixed set of literals


def start_wild() -> None:
    encounter_id = make_encounter()
    battle_session.start(encounter_id, "alice")


def start_trainer() -> None:
    trainer_battle.start("test_gym", "alice")


def start_pvp() -> None:
    pvp.start_vs_user("alice", "bob")


# --------------------------------------------- Stale battle_session (wild)
set_session("battle_session", "ongoing", age_minutes=45, encounter_id=make_encounter())
expect_ok("Stale wild battle does not block a gym/league battle", start_trainer)
clear("trainer_battle_session")
expect_ok("Stale wild battle does not block a PvP battle", start_pvp)
clear("pvp_battle_session")
clear("battle_session")

# --------------------------------------------- Fresh battle_session (wild)
set_session("battle_session", "ongoing", age_minutes=1, encounter_id=make_encounter())
expect_blocked("Fresh wild battle still blocks a gym/league battle", start_trainer)
expect_blocked("Fresh wild battle still blocks a PvP battle", start_pvp)
clear("battle_session")

# --------------------------------------------- Stale trainer_battle_session
set_session("trainer_battle_session", "ongoing", age_minutes=45)
expect_ok("Stale gym/league battle does not block a wild battle", start_wild)
clear("battle_session")
expect_ok("Stale gym/league battle does not block a PvP battle", start_pvp)
clear("pvp_battle_session")
clear("trainer_battle_session")

# --------------------------------------------- Fresh trainer_battle_session
set_session("trainer_battle_session", "ongoing", age_minutes=1)
expect_blocked("Fresh gym/league battle still blocks a wild battle", start_wild)
expect_blocked("Fresh gym/league battle still blocks a PvP battle", start_pvp)
clear("trainer_battle_session")

# --------------------------------------------- Stale pvp_battle_session
set_session("pvp_battle_session", "ongoing", age_minutes=45)
expect_ok("Stale PvP battle does not block a wild battle", start_wild)
clear("battle_session")
expect_ok("Stale PvP battle does not block a gym/league battle", start_trainer)
clear("trainer_battle_session")
clear("pvp_battle_session")

# --------------------------------------------- Fresh pvp_battle_session
set_session("pvp_battle_session", "ongoing", age_minutes=1)
expect_blocked("Fresh PvP battle still blocks a wild battle", start_wild)
expect_blocked("Fresh PvP battle still blocks a gym/league battle", start_trainer)
clear("pvp_battle_session")

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all battle lock timeout tests passed")
