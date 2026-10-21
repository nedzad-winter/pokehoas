"""Tests: the level cap of 100 holds for every source of levels - battle EXP
(team + wild opponent), Rare Candy, buddy steps, spawn rolls and the settings
that feed them.

Aufruf:  python3 tests/test_max_level.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-maxlevel-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, buddy, db, economy, seed, spawn  # noqa: E402
from app.api import config_routes, shop as shop_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


def expect_http(label: str, status: int, fn, *args) -> None:
    try:
        fn(*args)
        check(label, False, "no exception")
    except HTTPException as exc:
        check(label, exc.status_code == status, f"{exc.status_code}: {exc.detail}")


NOW = datetime.now(timezone.utc).isoformat()
IVS = {k: 10 for k in ("hp", "atk", "def", "spa", "spd", "spe")}

db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
SPECIES = db.query_one("SELECT * FROM species WHERE id = 1")
GROWTH = SPECIES.get("growth_rate") or "medium_fast"


def add_owned(level: int, party_slot: int | None = 1) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "party_slot, current_hp, caught_at) VALUES ('alice',1,?,?,0,?,?,?,?,?)",
        (level, battle.exp_for_level(level, GROWTH), json.dumps(IVS), "hardy", party_slot, None, NOW),
    )


def owned(owned_id: int) -> dict:
    return db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))


check("Cap is 100", battle.MAX_LEVEL == 100 and economy.MAX_LEVEL == 100 and buddy.MAX_LEVEL == 100)
CAP_EXP = battle.exp_for_level(100, GROWTH)

# ------------------------------------------------------------ Battle EXP
mon = add_owned(99)
combatant = battle.combatant_from_owned(owned(mon))
gained = battle.apply_exp(combatant, 10_000_000)
check("EXP: huge reward stops at level 100", owned(mon)["level"] == 100 and gained == [100], gained)
check("EXP: stored EXP does not run past the level-100 threshold", owned(mon)["exp"] == CAP_EXP, owned(mon)["exp"])

gained = battle.apply_exp(combatant, 5_000)
check("EXP: reward at level 100 gives no level", gained == [] and owned(mon)["level"] == 100, gained)
check("EXP: reward at level 100 does not pile up", owned(mon)["exp"] == CAP_EXP, owned(mon)["exp"])

low = add_owned(1, None)
battle.apply_exp(battle.combatant_from_owned(owned(low)), 10 ** 12)
check("EXP: from level 1 with absurd EXP -> 100", owned(low)["level"] == 100, owned(low)["level"])

# ----------------------------------------------------- Wild opponent EXP
wild = battle.Combatant(species=SPECIES, level=100, ivs=dict(IVS))
loser = battle.Combatant(species={**SPECIES, "base_exp": 10_000}, level=100, ivs=dict(IVS))
wild_battle = battle.Battle.__new__(battle.Battle)
wild_battle.wild, wild_battle.log, wild_battle.lang = wild, [], "en"
wild.exp = 0
battle.wild_gain_exp(wild_battle, loser)
check("Wild: level 100 wild Pokémon never levels up", wild.level == 100, wild.level)

# ------------------------------------------------------------ Rare Candy
db.execute(
    "INSERT INTO inventory (item_key, user_id, quantity) VALUES ('rare_candy','alice',3) "
    "ON CONFLICT(item_key, user_id) DO UPDATE SET quantity = excluded.quantity"
)
expect_http("Candy: level 100 -> 400", 400, shop_api.use_item,
            mon, shop_api.UseItemIn(item_key="rare_candy"), alice)
qty = db.query_one("SELECT quantity FROM inventory WHERE user_id='alice' AND item_key='rare_candy'")["quantity"]
check("Candy: not consumed at level 100", qty == 3, qty)

mid = add_owned(99, None)
shop_api.use_item(mid, shop_api.UseItemIn(item_key="rare_candy"), alice)
check("Candy: 99 -> 100", owned(mid)["level"] == 100)
expect_http("Candy: second candy on the same Pokémon -> 400", 400, shop_api.use_item,
            mid, shop_api.UseItemIn(item_key="rare_candy"), alice)
check("Candy: still level 100", owned(mid)["level"] == 100)

# Bulk use: capped by stock, by the level cap, and consumes exactly what it uses.
db.execute("UPDATE inventory SET quantity = 2 WHERE user_id='alice' AND item_key='rare_candy'")
bulk = add_owned(50, None)
shop_api.use_item(bulk, shop_api.UseItemIn(item_key="rare_candy", quantity=5), alice)
qty = db.query_one("SELECT quantity FROM inventory WHERE user_id='alice' AND item_key='rare_candy'")["quantity"]
check("Candy bulk: capped by stock (50 -> 52, bag empty)", owned(bulk)["level"] == 52 and qty == 0,
      (owned(bulk)["level"], qty))
expect_http("Candy bulk: empty bag -> 409", 409, shop_api.use_item,
            bulk, shop_api.UseItemIn(item_key="rare_candy", quantity=3), alice)
db.execute("UPDATE inventory SET quantity = 10 WHERE user_id='alice' AND item_key='rare_candy'")
near_cap = add_owned(97, None)
shop_api.use_item(near_cap, shop_api.UseItemIn(item_key="rare_candy", quantity=10), alice)
qty = db.query_one("SELECT quantity FROM inventory WHERE user_id='alice' AND item_key='rare_candy'")["quantity"]
check("Candy bulk: capped at level 100, only 3 consumed", owned(near_cap)["level"] == 100 and qty == 7,
      (owned(near_cap)["level"], qty))

# ----------------------------------------------------------------- Buddy
db.set_setting("buddy_steps_per_level", 1000)
near = add_owned(98, None)
user = db.query_one("SELECT * FROM app_user WHERE id = 'alice'")
gained = buddy._credit_steps(user, owned(near), 500_000)
check("Buddy: huge step credit stops at 100", owned(near)["level"] == 100 and gained == [99, 100], gained)
user = db.query_one("SELECT * FROM app_user WHERE id = 'alice'")
gained = buddy._credit_steps(user, owned(near), 500_000)
check("Buddy: no further level at 100", gained == [] and owned(near)["level"] == 100, gained)

# ----------------------------------------------------------- Spawn rolls
db.set_setting("wild_level_min", 500)
db.set_setting("wild_level_max", 900)
rolls = {spawn.roll_level() for _ in range(200)}
check("Spawn: even a bad stored setting never rolls above 100", max(rolls) <= 100, max(rolls))
db.set_setting("wild_level_min", 2)
db.set_setting("wild_level_max", 30)

# ------------------------------------------------------ Settings validation
for label, payload in (
    ("max above 100", {"wild_level_max": 101}),
    ("min above 100", {"wild_level_min": 150, "wild_level_max": 100}),
    ("min below 1", {"wild_level_min": 0}),
    ("min above max", {"wild_level_min": 50, "wild_level_max": 40}),
    ("fractional", {"wild_level_max": 30.5}),
    ("min above stored max", {"wild_level_min": 31}),
):
    expect_http(f"Settings: wild level {label} -> 400", 400, config_routes.patch_settings, payload)
check("Settings: rejected values were not stored",
      db.get_setting("wild_level_min", 0) == 2 and db.get_setting("wild_level_max", 0) == 30)
config_routes.patch_settings({"wild_level_min": 1, "wild_level_max": 100})
check("Settings: 1-100 is accepted",
      db.get_setting("wild_level_min", 0) == 1 and db.get_setting("wild_level_max", 0) == 100)

# -------------------------------------------------------------------- End
print()
if failures:
    print(f"{len(failures)} test(s) failed: {failures}")
    sys.exit(1)
print("All tests passed.")
