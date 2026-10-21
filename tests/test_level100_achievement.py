"""Level-100 achievements (1st and 10th own Pokemon ever at level 100) and
their Exp. Share reward.

Aufruf:  python3 tests/test_level100_achievement.py
"""

from __future__ import annotations

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

from app import achievements, battle, db, seed, training  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")["id"]


def qty(item_key: str) -> int:
    row = db.query_one(
        "SELECT quantity FROM inventory WHERE item_key = ? AND user_id = ?", (item_key, alice)
    )
    return int(row["quantity"]) if row else 0


def exp_at(owned_id: int, level: int = battle.MAX_LEVEL) -> list[dict]:
    return [{"owned_id": owned_id, "level_to": level}]


check("nothing unlocked at the start", achievements.level100_count(alice) == 0)
check("a level up below 100 does nothing", achievements.check_level100(alice, exp_at(1, 99)) == [])
check("count still zero", achievements.level100_count(alice) == 0)

new = [a["key"] for a in achievements.check_level100(alice, exp_at(1))]
check("1st level-100 Pokemon unlocks the first milestone", new == [achievements.LEVEL100_FIRST], str(new))
check("Exp. Share granted", qty("exp_share") == 1)
check("count is now 1", achievements.level100_count(alice) == 1)

check("the same Pokemon reaching 100 again is a no-op",
      achievements.check_level100(alice, exp_at(1)) == [])
check("count unchanged", achievements.level100_count(alice) == 1)
check("only one Exp. Share so far", qty("exp_share") == 1)

for owned_id in range(2, 10):
    new = achievements.check_level100(alice, exp_at(owned_id))
    check(f"pokemon #{owned_id} at level 100 unlocks nothing yet", new == [], str(new))
check("count is 9", achievements.level100_count(alice) == 9)

new = [a["key"] for a in achievements.check_level100(alice, exp_at(10))]
check("10th level-100 Pokemon unlocks the second milestone", new == [achievements.LEVEL100_TENTH], str(new))
check("second Exp. Share granted", qty("exp_share") == 2)

for owned_id in range(11, 51):
    new = [a["key"] for a in achievements.check_level100(alice, exp_at(owned_id))]
    expected = {20: [achievements.LEVEL100_20TH], 35: [achievements.LEVEL100_35TH],
                50: [achievements.LEVEL100_50TH]}.get(owned_id, [])
    check(f"pokemon #{owned_id} unlocks {expected}", new == expected, str(new))
check("20th and 35th pay an Exp. Share each (4 total), the 50th none", qty("exp_share") == 4, str(qty("exp_share")))
check("50th unlocks share_all_exp", achievements.share_all_exp(alice))
check("others do not", not achievements.share_all_exp("nobody") and not achievements.share_all_exp(None))
for owned_id in range(51, 101):
    new = achievements.check_level100(alice, exp_at(owned_id))
    check(f"pokemon #{owned_id} unlocks nothing", new == [], str(new))
new = [a["key"] for a in achievements.check_level100(alice, exp_at(101))]
check("101st level-100 Pokemon unlocks the Training milestone", new == [achievements.LEVEL100_101ST], str(new))
check("the 101st pays no item either", qty("exp_share") == 4, str(qty("exp_share")))
check("Training uses the same threshold",
      training.UNLOCK_COUNT == 101 and training.unlocked(alice))

# Players already past 101 before the milestone existed: caught up on overview.
bob = db.upsert_user("bob", "Bob")["id"]
now_iso = datetime.now().astimezone().isoformat()
for n in range(101):
    db.execute("INSERT INTO pokemon_level100 (user_id, owned_id, reached_at) VALUES (?,?,?)",
               (bob, 5000 + n, now_iso))
check("bob has no level-100 achievement yet", not any(k.startswith("level100") for k in achievements.unlocked(bob)))
achievements.overview(bob)
check("overview catches up every level-100 milestone incl. the 101st",
      set(achievements.LEVEL100_KEYS) <= set(achievements.unlocked(bob)), str(achievements.unlocked(bob)))

db.execute("DELETE FROM owned_pokemon WHERE user_id = ?", (alice,))
check("milestone count survives releasing every Pokemon", achievements.level100_count(alice) == 101)
done = achievements.unlocked(alice)
check("both achievements stay unlocked after release",
      {achievements.LEVEL100_FIRST, achievements.LEVEL100_TENTH} <= set(done), str(done))

ov = achievements.overview(alice)
groups = {a["group"] for a in ov["achievements"]}
check("overview carries every group",
      groups == {"battles", "pokedex", "level100", "catching", "daily", "generations"}, str(groups))
level100_items = {a["key"]: a for a in ov["achievements"] if a["group"] == "level100"}
check("overview carries both level-100 achievements",
      set(level100_items) == set(achievements.LEVEL100_KEYS), str(level100_items))
check("both marked unlocked", all(a["unlocked"] for a in level100_items.values()), str(level100_items))

print("\nFAILED: " + ", ".join(failures) if failures else "\nAll ok")
sys.exit(1 if failures else 0)
