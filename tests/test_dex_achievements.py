"""Pokedex achievements (seen / caught / living dex) and their shiny perks.

Aufruf:  python3 tests/test_dex_achievements.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import achievements, db, seed, spawn  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")["id"]
species_ids = [r["id"] for r in db.query("SELECT id FROM species ORDER BY id")]
now = "2026-01-01T00:00:00+00:00"


def dex(sid: int, seen: int, caught: int) -> None:
    db.execute(
        "INSERT INTO dex_entry (species_id, user_id, seen_count, caught_count, first_seen_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(species_id, user_id) DO UPDATE SET "
        "seen_count = excluded.seen_count, caught_count = excluded.caught_count",
        (sid, alice, seen, caught, now),
    )


def own(sid: int) -> None:
    db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, gender, "
        "current_hp, caught_at, hp_updated_at) VALUES (?,?,5,0,0,'{}','hardy','male',10,?,?)",
        (alice, sid, now, now),
    )


def give_charm() -> None:
    db.execute("INSERT OR REPLACE INTO inventory (item_key, user_id, quantity) VALUES ('shiny_charm',?,1)",
               (alice,))


def rate(n: int = 3000) -> float:
    return sum(spawn.roll_shiny(alice) for _ in range(n)) / n


check("nothing unlocked at the start", achievements.sync_pokedex(alice) == [])
check("boost neutral",
      achievements.shiny_boost(alice) == {"charm_factor": 1.0, "living_rate": None, "gen_factors": {}})

for sid in species_ids[:-1]:
    dex(sid, 1, 0)
check("all but one seen: locked", achievements.sync_pokedex(alice) == [])
dex(species_ids[-1], 1, 0)
new = [a["key"] for a in achievements.sync_pokedex(alice)]
check("all seen unlocks dex_seen_all only", new == [achievements.DEX_SEEN_ALL], str(new))
check("seen perk: charm x1.1", achievements.shiny_boost(alice)["charm_factor"] == 1.1)

for sid in species_ids:
    dex(sid, 1, 1)
new = [a["key"] for a in achievements.sync_pokedex(alice)]
# Every species of every generation is caught too, so every gen_caught_<n>
# (achievements.GEN_CAUGHT_PREFIX) unlocks alongside dex_caught_all.
expected = [achievements.gen_caught_key(g) for g in achievements.generations()] + [achievements.DEX_CAUGHT_ALL]
check("all caught unlocks dex_caught_all and every gen_caught", new == expected, str(new))
check("caught perk replaces seen: charm x2", achievements.shiny_boost(alice)["charm_factor"] == 2.0)
check("no living rate yet", achievements.shiny_boost(alice)["living_rate"] is None)

db.set_setting("shiny_rate", 0.1)
db.set_setting("shiny_charm_multiplier", 2)
without = rate()
give_charm()
with_charm = rate()
check("charm without perk would be x2; with caught perk ~x4",
      0.36 < with_charm < 0.44 and without < 0.13, f"{without:.3f} / {with_charm:.3f}")

for sid in species_ids[:-1]:
    own(sid)
# species_ids[-1] is the only species of the highest generation still
# missing, so every earlier generation's gen_living_<n> unlocks already.
new = [a["key"] for a in achievements.sync_pokedex(alice)]
expected = [achievements.gen_living_key(g) for g in achievements.generations()[:-1]]
check("missing one: every earlier gen_living unlocks, no living dex yet", new == expected, str(new))
own(species_ids[-1])
new = [a["key"] for a in achievements.sync_pokedex(alice)]
expected = [achievements.gen_living_key(achievements.generations()[-1]), achievements.DEX_LIVING]
check("owning all unlocks the last gen_living and dex_living", new == expected, str(new))
db.set_setting("shiny_rate", 1 / 4096)
db.execute("DELETE FROM inventory WHERE user_id = ?", (alice,))
r = rate(12000)
check("living dex: ~1/100 without charm", 0.006 < r < 0.015, f"{r:.4f}")
db.execute("DELETE FROM owned_pokemon WHERE user_id = ?", (alice,))
check("perk is permanent after release", achievements.shiny_boost(alice)["living_rate"] == 0.01)

ov = achievements.overview(alice)
groups = {a["group"] for a in ov["achievements"]}
check("overview has all groups",
      groups == {"battles", "pokedex", "level100", "catching", "daily", "generations"}, str(groups))
check("dex achievements carry progress",
      all("progress" in a for a in ov["achievements"] if a["group"] == "pokedex"))

print("\nFAILED: " + ", ".join(failures) if failures else "\nAll ok")
sys.exit(1 if failures else 0)
