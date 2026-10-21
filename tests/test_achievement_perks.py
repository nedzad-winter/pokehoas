"""Non-item bonuses listed in the achievements overview (achievements.perks).

Aufruf:  python3 tests/test_achievement_perks.py
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

from app import achievements, db, healing, seed, trainers  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")["id"]
bob = db.upsert_user("bob", "Bob")["id"]

perks = achievements.perks(alice)
check("no healing perk at the start", perks["regen_bonus"] == 0 and perks["cooldown_reduction"] == 0,
      str(perks))
check("no EXP / training perk at the start", not perks["share_all_exp"] and not perks["training"])
check("only generation 1 in the spawn pool", perks["generations"] == [1], str(perks["generations"]))
check("no sprite styles", perks["sprite_styles"] == [])

# Beat the whole Kanto league: Elite Four + Champion feed the healing perks,
# and the region unlocks the next generation (default lock mode 'region').
kanto = [t for t in trainers.all_trainers() if t["region"] == "kanto"]
for t in kanto:
    achievements.unlock(alice, achievements.trainer_key(t["id"]))
e4 = sum(1 for t in kanto if t["kind"] == "elite_four")
e4_champ = sum(1 for t in kanto if t["kind"] in ("elite_four", "champion"))
perks = achievements.perks(alice)
check("regen bonus per Elite Four member",
      perks["regen_bonus"] == e4 * healing.ELITE_FOUR_REGEN_BONUS, str(perks["regen_bonus"]))
check("cooldown reduction per Elite Four member and Champion",
      perks["cooldown_reduction"] == e4_champ * healing.ELITE_FOUR_CHAMPION_COOLDOWN_REDUCTION_MINUTES,
      str(perks["cooldown_reduction"]))
check("matches healing.cooldown_minutes",
      healing.cooldown_minutes(alice) == max(0.0, float(db.get_setting("heal_team_cooldown_minutes", 60))
                                             - perks["cooldown_reduction"]))
check("generations follow unlocked_generations",
      perks["generations"] == sorted(achievements.unlocked_generations(alice)), str(perks["generations"]))

achievements.unlock(alice, achievements.LEVEL100_50TH)
achievements.unlock(alice, achievements.LEVEL100_101ST)
achievements.unlock(alice, achievements.gen_living_key(3))
perks = achievements.perks(alice)
check("50th level-100 -> equal EXP", perks["share_all_exp"])
check("101st level-100 -> training", perks["training"])
check("living generation -> sprite style", perks["sprite_styles"] == [3], str(perks["sprite_styles"]))

other = achievements.perks(bob)
check("perks are per user", other["regen_bonus"] == 0 and not other["training"]
      and other["sprite_styles"] == [], str(other))

check("overview carries the perks", achievements.overview(alice)["perks"] == perks)

if failures:
    print(f"\n{len(failures)} FAIL")
    sys.exit(1)
print("\nall ok")
