"""Catch / Pokedex-percentage / roamer / daily-streak milestones and their
ball and Rare Candy rewards (achievements.MILESTONES).

Aufruf:  python3 tests/test_milestones.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import achievements, db, i18n, roaming, seed  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")["id"]
# The fallback seed has no roaming legendaries: clone a species row for each.
_cols = [r["name"] for r in db.query("PRAGMA table_info(species)")]
for _rid in sorted(roaming.ROAMING_IDS):
    _sel = ", ".join("?" if c == "id" else "? " if c == "name" else c for c in _cols)
    db.execute(f"INSERT INTO species ({', '.join(_cols)}) SELECT {_sel} FROM species WHERE id = 25",
               tuple(v for c, v in (("id", _rid), ("name", f"roamer{_rid}")) if c in _cols))
total = achievements.dex_progress(alice)["total"]


def qty(item_key: str) -> int:
    row = db.query_one("SELECT quantity FROM inventory WHERE item_key = ? AND user_id = ?",
                       (item_key, alice))
    return int(row["quantity"]) if row else 0


def catch(species_id: int, times: int = 1) -> None:
    db.execute(
        "INSERT INTO dex_entry (species_id, user_id, seen_count, caught_count) VALUES (?,?,1,?) "
        "ON CONFLICT(species_id, user_id) DO UPDATE SET caught_count = caught_count + ?",
        (species_id, alice, times, times))


def keys(new: list[dict]) -> list[str]:
    return [a["key"] for a in new]


# Every milestone has titles/descriptions in both languages and a reward.
for lang in ("en", "de"):
    defs = {d["key"]: d for d in achievements.definitions(lang)}
    check(f"all milestones defined ({lang})", all(k in defs for k in achievements.MILESTONES))
    check(f"no missing i18n key ({lang})",
          all(not defs[k]["title"].startswith("achievements.") for k in achievements.MILESTONES))
check("balls stay within the gym/daily scale",
      all(n <= 30 for m in achievements.MILESTONES.values() for k, n in m["items"].items()
          if k != "master_ball"))
check("Master Ball only for the long hauls",
      [k for k, m in achievements.MILESTONES.items() if "master_ball" in m["items"]]
      == ["catch_1000", "roamer_first", "roamer_all", "daily_streak_100"])

# --- catches
check("nothing at the start", achievements.sync_milestones(alice) == [])
catch(1, 9)
check("9 catches: nothing", achievements.sync_milestones(alice) == [])
catch(4, 1)
new = keys(achievements.sync_milestones(alice))
check("10 catches unlock the first tier", new == ["catch_10"], str(new))
check("10 Poke Balls paid", qty("poke_ball") == 10)
check("no second payout", achievements.sync_milestones(alice) == [] and qty("poke_ball") == 10)
catch(1, 90)
new = keys(achievements.sync_milestones(alice))
check("100 catches unlock the second tier", new == ["catch_100"], str(new))
check("15 Great Balls paid", qty("great_ball") == 15)

# --- Pokedex percentage
roamer_ids = set(roaming.ROAMING_IDS)
ids = [r["id"] for r in db.query("SELECT id FROM species ORDER BY id") if r["id"] not in roamer_ids]
need25 = achievements.milestone_target("dex_caught_25", total)
check("percent target rounds up", need25 * 100 >= total * 25 and (need25 - 1) * 100 < total * 25)
for sid in ids:
    if achievements.dex_progress(alice)["caught"] >= need25 - 1:
        break
    if not db.query_one("SELECT 1 FROM dex_entry WHERE user_id=? AND species_id=? AND caught_count>0",
                        (alice, sid)):
        catch(sid)
check("just below 25%: no dex milestone", "dex_caught_25" not in keys(achievements.sync_milestones(alice)))
for sid in ids:
    if achievements.dex_progress(alice)["caught"] >= need25:
        break
    if not db.query_one("SELECT 1 FROM dex_entry WHERE user_id=? AND species_id=? AND caught_count>0",
                        (alice, sid)):
        catch(sid)
new = keys(achievements.sync_milestones(alice))
check("25% unlocks", "dex_caught_25" in new, str(new))
check("25% pays Ultra Balls and Rare Candy", qty("ultra_ball") >= 5 and qty("rare_candy") == 3)

# --- roamers
check("no roamer yet", "roamer_first" not in achievements.unlocked(alice))
before = qty("master_ball")
roamers = sorted(roaming.ROAMING_IDS)
for r in roamers[:-1]:
    catch(r)
new = keys(achievements.sync_milestones(alice))
check("first roamer unlocks Caught on the Run", "roamer_first" in new, str(new))
check("not all roamers yet", "roamer_all" not in achievements.unlocked(alice))
catch(roamers[-1])
new = keys(achievements.sync_milestones(alice))
check("all roamers unlock Roamer Tracker", "roamer_all" in new, str(new))
check("each roamer milestone pays a Master Ball", qty("master_ball") - before == 2,
      str(qty("master_ball") - before))

# --- daily streak
today = date.today()


def win(day: date) -> None:
    db.execute(
        "INSERT OR REPLACE INTO daily_challenge (user_id, day, slot, opponent_kind, opponent_ref, "
        "opponent_name, snapshot, status) VALUES (?,?,1,'trainer','x','X','[]','won')",
        (alice, day.isoformat()))


check("no streak", achievements.daily_streak(alice) == 0)
for i in range(6):
    win(today - timedelta(days=i))
check("6 days in a row = 6", achievements.daily_streak(alice) == 6)
check("6 days unlock nothing", achievements.sync_milestones(alice, "en", ("streak",)) == [])
win(today - timedelta(days=8))  # gap on day 7
check("a gap breaks the run", achievements.daily_streak(alice) == 6)
win(today - timedelta(days=6))
check("7th consecutive day", achievements.daily_streak(alice) == 7)
new = keys(achievements.sync_milestones(alice, "en", ("streak",)))
check("7 days unlock Weekly Routine", new == ["daily_streak_7"], str(new))
check("streak reward paid", qty("ultra_ball") >= 15 and qty("rare_candy") == 8)
check("limiting metrics ignores the others", achievements.sync_milestones(alice, "en", ("streak",)) == [])

# --- overview carries progress
ov = {a["key"]: a for a in achievements.overview(alice)["achievements"]}
check("overview lists the milestones", all(k in ov for k in achievements.MILESTONES))
check("progress shown for a locked one", ov["catch_500"]["progress"]["total"] == 500
      and ov["catch_500"]["progress"]["current"] >= 100)
check("groups", {ov[k]["group"] for k in achievements.MILESTONES} == {"catching", "pokedex", "daily"})

print("\nFAILED: " + ", ".join(failures) if failures else "\nall ok")
sys.exit(1 if failures else 0)
