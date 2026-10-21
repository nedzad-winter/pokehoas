"""Per-generation Pokedex achievements (gen_caught_<n> / gen_living_<n>):
money + Ultra Balls, generation shiny factor, mythical event spawns, perfect
IVs and the sprite style.

Aufruf:  python3 tests/test_generation_achievements.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import achievements, db, economy, rewards, seed, spawn  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")["id"]
bob = db.upsert_user("bob", "Bob")["id"]
now = "2026-01-01T00:00:00+00:00"

# The fallback Pokedex has three species per generation (starters); add two
# generation-2 mythicals (one base form, one evolution of it).
gen2 = [r["id"] for r in db.query("SELECT id FROM species WHERE generation = 2 ORDER BY id")]
template = db.query_one("SELECT * FROM species WHERE id = ?", (gen2[0],))
for sid, name, evolves_from in ((9001, "mythbase", None), (9002, "mythevo", 9001)):
    row = {**template, "id": sid, "name": name, "name_en": name.title(), "name_de": name.title(),
           "generation": 2, "is_mythical": 1, "evolves_from": evolves_from}
    db.execute(f"INSERT INTO species ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
               tuple(row.values()))


def dex(user: str, sid: int) -> None:
    db.execute(
        "INSERT INTO dex_entry (species_id, user_id, seen_count, caught_count, first_seen_at) "
        "VALUES (?,?,1,1,?) ON CONFLICT(species_id, user_id) DO UPDATE SET caught_count = 1",
        (sid, user, now),
    )


def own(user: str, sid: int) -> None:
    db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, gender, "
        "current_hp, caught_at, hp_updated_at) VALUES (?,?,5,0,0,'{}','hardy','male',10,?,?)",
        (user, sid, now, now),
    )


def keys(defs: list[dict]) -> set[str]:
    return {d["key"] for d in defs}


# --- definitions ------------------------------------------------------------
defs = {d["key"]: d for d in achievements.definitions("en")}
check("two achievements per generation",
      all(achievements.gen_caught_key(g) in defs and achievements.gen_living_key(g) in defs
          for g in achievements.generations()))
check("caught reward: 200k + 40 Ultra Balls",
      defs["gen_caught_2"]["reward"]["money"] == 200_000
      and defs["gen_caught_2"]["reward"]["items"] == [{"key": "ultra_ball", "quantity": 40}],
      str(defs["gen_caught_2"]["reward"]))
check("money in the reward label", "₽200,000" in defs["gen_caught_2"]["reward"]["label"],
      defs["gen_caught_2"]["reward"]["label"])
check("german money label", rewards.money_label(200_000, "de") == "₽200.000")
check("living reward names only the mythical base form",
      "Mythbase" in defs["gen_living_2"]["reward"]["label"]
      and "Mythevo" not in defs["gen_living_2"]["reward"]["label"],
      defs["gen_living_2"]["reward"]["label"])
check("generation without mythicals has no living reward", defs["gen_living_1"]["reward"] is None)
check("event mythicals = base forms", [m["id"] for m in achievements.event_mythicals(2)] == [9001])

# --- progress ignores mythicals ---------------------------------------------
progress = achievements.generation_progress(alice)
check("gen 2 total excludes mythicals", progress[2]["total"] == len(gen2), str(progress[2]))
check("gen 2 total counts its regular species", len(gen2) == 3 and progress[1]["total"] > 0)

# --- caught -----------------------------------------------------------------
money_before = economy.balance(alice)
for sid in gen2[:-1]:
    dex(alice, sid)
check("one missing: nothing", achievements.sync_generations(alice) == [])
dex(alice, gen2[-1])
new = keys(achievements.sync_pokedex(alice))
check("all of gen 2 caught unlocks gen_caught_2 (via sync_pokedex)", new == {"gen_caught_2"}, str(new))
check("money credited", economy.balance(alice) == money_before + 200_000)
balls = db.query_one("SELECT quantity FROM inventory WHERE user_id = ? AND item_key = 'ultra_ball'",
                     (alice,))
check("40 Ultra Balls", balls is not None and balls["quantity"] >= 40)
check("reward paid once", achievements.sync_generations(alice) == []
      and economy.balance(alice) == money_before + 200_000)
check("mythicals not needed for caught", "gen_caught_2" in achievements.unlocked(alice))
check("bob unaffected", achievements.caught_generations(bob) == set())

boost = achievements.shiny_boost(alice)
check("gen factor only for gen 2", boost["gen_factors"] == {2: 1.5}, str(boost["gen_factors"]))

db.set_setting("shiny_rate", 0.1)
n = 4000
r2 = sum(spawn.roll_shiny(alice, 2) for _ in range(n)) / n
r1 = sum(spawn.roll_shiny(alice, 1) for _ in range(n)) / n
check("gen 2 shiny ~x1.5 without charm", 0.13 < r2 < 0.17, f"{r2:.3f}")
check("gen 1 unchanged", 0.08 < r1 < 0.12, f"{r1:.3f}")
db.execute("INSERT OR REPLACE INTO inventory (item_key, user_id, quantity) VALUES ('shiny_charm',?,1)",
           (alice,))
db.set_setting("shiny_rate", 0.05)
r2c = sum(spawn.roll_shiny(alice, 2) for _ in range(n)) / n
check("multiplies with the charm (0.05 x2 x1.5 = 0.15)", 0.13 < r2c < 0.17, f"{r2c:.3f}")
db.execute("DELETE FROM inventory WHERE user_id = ? AND item_key = 'shiny_charm'", (alice,))
db.set_setting("shiny_rate", 1 / 4096)

# --- living -----------------------------------------------------------------
spawn_now = datetime.fromisoformat("2026-03-01T12:00:00+00:00")
db.set_setting("max_spawns_per_day", 1)
for sid in gen2:
    own(alice, sid)
before = {r["id"] for r in db.query("SELECT id FROM encounter WHERE user_id = ?", (alice,))}
new = keys(achievements.sync_generations(alice))
check("owning all of gen 2 unlocks gen_living_2", new == {"gen_living_2"}, str(new))
events = db.query("SELECT * FROM encounter WHERE user_id = ? AND source_kind = 'event'", (alice,))
check("one event encounter per mythical base form",
      [e["species_id"] for e in events] == [9001], str([e["species_id"] for e in events]))
check("event encounter is new", all(e["id"] not in before for e in events))
check("event source names the generation",
      spawn.event_generation(events[0]["source_entity"]) == 2, events[0]["source_entity"])
created = datetime.fromisoformat(events[0]["created_at"])
expires = datetime.fromisoformat(events[0]["expires_at"])
check("event encounter stays a year", (expires - created).days == spawn.EVENT_RETENTION_DAYS)
check("events don't count for the daily cap",
      not spawn._daily_cap_reached(alice, datetime.now().astimezone()))
check("events don't trigger the global cooldown",
      not spawn._cooldown_blocked(alice, datetime.now().astimezone()))
check("no second event on resync", achievements.sync_generations(alice) == []
      and len(db.query("SELECT id FROM encounter WHERE user_id = ? AND source_kind = 'event'",
                       (alice,))) == 1)
db.set_setting("max_spawns_per_day", 15)

check("living gen 2 gives no perfect IV perk", not hasattr(achievements, "perfect_ivs"))

db.execute("DELETE FROM owned_pokemon WHERE user_id = ?", (alice,))
check("living perk permanent after release", achievements.living_generations(alice) == {2})
check("living generations", achievements.living_generations(alice) == {2})

# --- overview ---------------------------------------------------------------
ov = achievements.overview(alice)
gen_items = [a for a in ov["achievements"] if a["group"] == "generations"]
check("overview lists the generations group", len(gen_items) == 2 * len(achievements.generations()))
check("generation achievements carry progress", all("progress" in a for a in gen_items))
check("overview shows the gen factors", ov["shiny_boost"]["gen_factors"] == {2: 1.5})

# --- sprite style API -------------------------------------------------------
from fastapi import HTTPException  # noqa: E402

from app.api import users as users_api  # noqa: E402

alice_user = db.get_user(alice)
SP = gen2[0]
res = users_api.set_sprite_style(SP, users_api.SpriteStyleChoice(sprite_style=2), alice_user)
check("unlocked style can be picked per species",
      res == {"sprite_styles": [2], "species_sprite_styles": {SP: 2}}, str(res))
try:
    users_api.set_sprite_style(SP, users_api.SpriteStyleChoice(sprite_style=1), alice_user)
    check("locked style rejected", False)
except HTTPException as e:
    check("locked style rejected", e.status_code == 400)
res = users_api.set_sprite_style(SP + 1, users_api.SpriteStyleChoice(sprite_style=2), alice_user)
check("species are independent", res["species_sprite_styles"] == {SP: 2, SP + 1: 2}, str(res))
res = users_api.set_sprite_style(SP, users_api.SpriteStyleChoice(sprite_style=None), alice_user)
check("back to normal for one species", res["species_sprite_styles"] == {SP + 1: 2}, str(res))
db.execute("DELETE FROM achievement WHERE user_id = ?", (alice,))
check("styles ignored when the achievement is gone",
      users_api._sprite_style(alice)["species_sprite_styles"] == {})

print("\nFAILED: " + ", ".join(failures) if failures else "\nAll ok")
sys.exit(1 if failures else 0)
