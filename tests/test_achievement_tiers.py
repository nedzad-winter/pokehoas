"""Every achievement has a bronze / silver / gold tier (the trophy in the UI).

Gym leaders are bronze, Elite Four silver, Champions and regions gold; the
other groups are tiered per entry (achievements.MILESTONES, DEX_TIER,
LEVEL100_TIER). A new achievement without a tier would show no trophy colour,
so this checks the whole definition list and the i18n labels of the tiers.

Aufruf:  python3 tests/test_achievement_tiers.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import achievements, db, trainers  # noqa: E402

failures: list[str] = []
TIERS = {"bronze", "silver", "gold"}


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


db.init_db()
defs = achievements.definitions("en", db.DEFAULT_REGION_LOCK)
by_key = {d["key"]: d for d in defs}

check("every achievement has a tier", all(d.get("tier") in TIERS for d in defs),
      str([d["key"] for d in defs if d.get("tier") not in TIERS]))

kind_tiers = {d["trainer_id"]: d["tier"] for d in defs if "trainer_id" in d}
check("gym leaders are bronze", all(kind_tiers[t["id"]] == "bronze" for t in trainers.all_trainers()
                                    if t["kind"] == "gym_leader"))
check("elite four are silver", all(kind_tiers[t["id"]] == "silver" for t in trainers.all_trainers()
                                   if t["kind"] == "elite_four"))
check("champions are gold", all(kind_tiers[t["id"]] == "gold" for t in trainers.all_trainers()
                                if t["kind"] == "champion"))
check("region clears and League Champion are gold",
      all(d["tier"] == "gold" for d in defs if d["category"] == "region" or d["key"] == achievements.LEAGUE_MASTER))
check("milestones carry their tier", all(by_key[k]["tier"] == m["tier"]
                                         for k, m in achievements.MILESTONES.items()))
check("level-100 tiers", all(by_key[k]["tier"] == achievements.LEVEL100_TIER[k]
                             for k in achievements.LEVEL100_KEYS))
check("pokedex tiers", all(by_key[k]["tier"] == achievements.DEX_TIER[k]
                           for k in achievements.DEX_KEYS))

# --- tier labels exist in both catalogs (the tooltip of the trophy)
for lang in ("en", "de"):
    catalog = json.loads((ROOT / "pokemon_encounter" / "app" / "data" / "i18n" / f"{lang}.json").read_text("utf-8"))
    missing = [f"achievements.tier_{t}" for t in TIERS if f"achievements.tier_{t}" not in catalog]
    check(f"tier labels in {lang}.json", not missing, str(missing))

print("\nFAILED: " + ", ".join(failures) if failures else "\nall ok")
sys.exit(1 if failures else 0)
