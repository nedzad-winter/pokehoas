"""Tests for god mode (app/god_mode.py): admin-only switch that lifts costs,
cooldowns and unlock gates for that one account.

Run:  python3 tests/test_god_mode.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-godmode-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from fastapi.testclient import TestClient  # noqa: E402

from app import (  # noqa: E402
    achievements, admin, db, evolution, god_mode, healing, seed, tms, training,
)
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()
client = TestClient(app)  # no `with` -> no lifespan (poller / seeding)
logging.getLogger("httpx").setLevel(logging.WARNING)

HA_USERS = [
    {"id": "alice", "name": "Alice", "username": "alice", "is_owner": False,
     "is_active": True, "group_ids": ["system-admin"]},
    {"id": "bob", "name": "Bob", "username": "bob", "is_owner": False,
     "is_active": True, "group_ids": ["system-users"]},
]


async def fake_fetch() -> list[dict]:
    return [dict(user) for user in HA_USERS]


admin._fetch_ha_users = fake_fetch


def as_user(user_id: str) -> dict[str, str]:
    return {"X-Remote-User-Id": user_id, "X-Remote-User-Name": user_id}


ALICE, BOB = as_user("alice"), as_user("bob")
for headers in (ALICE, BOB):
    client.get("/api/users/me", headers=headers)  # creates the accounts
db.execute("UPDATE app_user SET money = 1000")


def give(user_id: str, species_id: int, level: int) -> int:
    r = client.post(f"/api/admin/users/{user_id}/pokemon", headers=ALICE,
                    json={"species_id": species_id, "level": level, "placement": "box"})
    return r.json()["owned_id"]


def owned(owned_id: int) -> dict:
    return db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))


# --------------------------------------------------------- The switch
check("Off by default", db.get_user("alice")["god_mode"] == 0 and not god_mode.active("alice"))
check("Can't be switched on for a non-admin", client.patch(
    "/api/admin/users/bob", headers=ALICE, json={"god_mode": True}).status_code == 400)
check("A non-admin can't switch it on at all", client.patch(
    "/api/admin/users/bob", headers=BOB, json={"god_mode": True}).status_code == 403)
r = client.patch("/api/admin/users/alice", headers=ALICE, json={"god_mode": True})
check("Admin switches it on for an admin account", r.status_code == 200 and god_mode.active("alice"))
check("/api/users/me reports it", client.get("/api/users/me", headers=ALICE).json()["god_mode"] is True)

# ----------------------------------------------------------------- Shop
shop = client.get("/api/shop", headers=ALICE).json()
master = next((it for it in shop["items"] if it["key"] == "master_ball"), None)
check("Shop sells the Master Ball, for free", master is not None and master["price"] == 0
      and all(it["price"] == 0 for it in shop["items"]))
r = client.post("/api/shop/buy", headers=ALICE, json={"item_key": "master_ball", "quantity": 3})
check("Buying costs nothing", r.status_code == 200 and r.json()["money"] == 1000
      and r.json()["owned"] == 3, r.text)
check("Others still pay and can't buy a Master Ball",
      all(it["key"] != "master_ball" for it in client.get("/api/shop", headers=BOB).json()["items"])
      and client.post("/api/shop/buy", headers=BOB,
                      json={"item_key": "master_ball", "quantity": 1}).status_code == 400)
r = client.post("/api/shop/buy", headers=BOB, json={"item_key": "poke_ball", "quantity": 1})
check("Others are charged", r.status_code == 200 and r.json()["money"] < 1000)

# ----------------------------------------------------------------- Heal
give("alice", 129, 10)
give("bob", 129, 10)
healing.heal_team("alice")
healing.heal_team("bob")
try:
    healing.heal_team("alice")
    alice_again = True
except ValueError:
    alice_again = False
try:
    healing.heal_team("bob")
    bob_again = True
except ValueError:
    bob_again = False
check("No heal cooldown in god mode", alice_again and healing.cooldown_minutes("alice") == 0)
check("Cooldown still applies to others", not bob_again)

# ------------------------------------------------------------------ TMs
karp_alice, karp_bob = give("alice", 129, 5), give("bob", 129, 5)
locked_tm = next(
    (key for key in tms.all_tms() if not tms.teachable(owned(karp_bob), key)
     and tms.move_key(key) not in tms.moves_mod.owned_learned_moves(
         db.query_one("SELECT * FROM species WHERE id = 129"), owned(karp_bob))),
    None,
)
check("There is a TM a level-5 Magikarp can't learn normally", locked_tm is not None)
check("God mode teaches it anyway", tms.teachable(owned(karp_alice), locked_tm))
db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES (?, 'alice', 1)", (locked_tm,))
tms.teach("alice", karp_alice, locked_tm)
check("Teach succeeds", tms.move_key(locked_tm) in json.loads(owned(karp_alice)["taught_moves"]))

# ------------------------------------------------------------ Evolution
db.execute("INSERT OR IGNORE INTO evolution (from_species_id, to_species_id, trigger_kind, min_level) "
           "VALUES (129, 25, 'level-up', 20)")
db.execute("INSERT OR IGNORE INTO evolution (from_species_id, to_species_id, trigger_kind, item_key) "
           "VALUES (129, 4, 'use-item', 'fire_stone')")
check("Normally no evolution below the level", evolution.level_evolutions(owned(karp_bob)) == [])
targets = {e["to_species_id"] for e in evolution.level_evolutions(owned(karp_alice))}
check("God mode: every evolution, any trigger, right away", {25, 4} <= targets, targets)
evolution.evolve_by_level("alice", karp_alice, 4)
check("Evolve via an item edge without the item", owned(karp_alice)["species_id"] == 4)

# ------------------------------------------------- Training / sprites
check("Training unlocked", training.unlocked("alice") and not training.unlocked("bob"))
me = client.get("/api/users/me", headers=ALICE).json()
check("Every sprite style available", me["sprite_styles"] == achievements.generations())
check("Others only get earned styles",
      client.get("/api/users/me", headers=BOB).json()["sprite_styles"] == [])

# -------------------------------------- Losing admin rights ends it
HA_USERS[0]["group_ids"] = ["system-users"]
admin.clear_cache()
me = client.get("/api/users/me", headers=ALICE).json()
check("No longer admin -> switched off on next visit",
      me["god_mode"] is False and not god_mode.active("alice"))

# --------------------------------------------------------------------- End
print()
if failures:
    print(f"{len(failures)} test(s) failed: {failures}")
    sys.exit(1)
print("All tests passed.")
