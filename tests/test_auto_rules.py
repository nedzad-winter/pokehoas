"""Tests for the auto rules (app/auto_rules.py): schema, conditions, first-match
order, the defeat/catch/throw/flee/skip actions, guards, auto-buy, heal, team
choice, spawn hook, migration from `auto_battle_also_catches` and the API.

Aufruf:  python3 tests/test_auto_rules.py
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

TMP = tempfile.mkdtemp(prefix="pokemon-autorules-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import (auto_rules, battle, battle_history, db, economy, encounter_state,  # noqa: E402
                 seed, spawn)
from app.api import auto_rules as auto_rules_api  # noqa: E402
from app.api import encounters as encounters_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


random.seed(int(os.environ.get("TEST_SEED", "1234")))
db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")
USER = {"id": "alice", "language": "en", "name": "Alice"}
IVS = {k: 15 for k in spawn.STAT_KEYS}
NOW = datetime.now().astimezone()


def species(name: str) -> dict:
    return db.query_one("SELECT * FROM species WHERE name = ?", (name,))


PIKA, KARP, SQUIRT, BULBA = species("pikachu"), species("magikarp"), species("squirtle"), \
    species("bulbasaur")


def make_owned(user_id: str, sp: dict, level: int, slot: int | None, hp: int | None = None) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "current_hp, party_slot, caught_at, hp_updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, sp["id"], level, battle.exp_for_level(level, sp["growth_rate"]), 0,
         json.dumps(IVS), "hardy", hp, slot, NOW.isoformat(), NOW.isoformat()),
    )


def make_encounter(sp: dict, level: int, user_id: str = "alice", **fields) -> dict:
    encounter_id = spawn.create_encounter(sp, source_entity="binary_sensor.kitchen",
                                          user_id=user_id)
    sets = {"level": level, "ivs": json.dumps(fields.pop("ivs", IVS)), "shiny": 0,
            "ability": None, **fields}
    db.execute(f"UPDATE encounter SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?",
               (*sets.values(), encounter_id))
    return db.query_one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))


def set_stock(user_id: str, **items: int) -> None:
    db.execute("DELETE FROM inventory WHERE user_id = ?", (user_id,))
    for key, qty in items.items():
        db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES (?,?,?)",
                   (key, user_id, qty))


def stock(user_id: str, key: str) -> int:
    row = db.query_one("SELECT quantity FROM inventory WHERE user_id = ? AND item_key = ?",
                       (user_id, key))
    return row["quantity"] if row else 0


def rule(name: str = "r", conditions: dict | None = None, actions: dict | None = None,
         enabled: bool = True) -> auto_rules.RuleIn:
    return auto_rules.RuleIn(name=name, enabled=enabled, conditions=conditions or {},
                             actions=actions or {})


def clear_rules(user_id: str = "alice") -> None:
    db.execute("DELETE FROM auto_rule WHERE user_id = ?", (user_id,))


def heal_all(user_id: str = "alice") -> None:
    db.execute("UPDATE owned_pokemon SET current_hp = NULL WHERE user_id = ?", (user_id,))


def outcome_of(encounter_id: int, user_id: str = "alice") -> str | None:
    own = encounter_state.progress(encounter_id, user_id)
    return own["outcome"] if own else None


def matches(cond: dict, enc: dict, user_id: str = "alice") -> bool:
    row = encounter_state.for_user(enc, user_id)
    return auto_rules.matches(auto_rules.Conditions.model_validate(cond),
                              auto_rules.Facts(row, user_id))


class AlwaysThrow:
    """Context manager: every ball catches (True) or breaks free (False)."""

    def __init__(self, caught: bool) -> None:
        self.caught = caught

    def __enter__(self):
        self.orig = battle.throw_ball
        caught = self.caught
        battle.throw_ball = lambda wild, user_id, ball_key="poke_ball", status=None, context=None: \
            battle.CatchResult(caught=caught, critical=False, shakes=4 if caught else 1,
                               probability=0.5)
        return self

    def __exit__(self, *exc):
        battle.throw_ball = self.orig


# A strong lead so battles end quickly.
squirt_id = make_owned("alice", SQUIRT, 60, 1)
bulba_id = make_owned("alice", BULBA, 8, 2)
db.execute("UPDATE app_user SET money = 5000 WHERE id = 'alice'")


# ------------------------------------------------------------ Migration
cols = {row["name"] for row in db.query("PRAGMA table_info(app_user)")}
check("Migration adds the player switches",
      {"auto_rules_enabled", "auto_rules_daily_cap"} <= cols)
check("Migration adds battle_log.rule_name",
      "rule_name" in {row["name"] for row in db.query("PRAGMA table_info(battle_log)")})
check("Retired settings are not in DEFAULT_SETTINGS",
      "auto_battle_also_catches" not in db.DEFAULT_SETTINGS
      and "auto_battle_only_known_species" not in db.DEFAULT_SETTINGS)

db.execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('auto_battle_also_catches', 'true')")
db.execute("INSERT OR REPLACE INTO setting (key, value) VALUES "
           "('auto_battle_only_known_species', 'true')")
db.init_db()
seeded = auto_rules.list_rules("alice")
check("Old auto-catch switch becomes one rule per player",
      len(seeded) == 1 and len(auto_rules.list_rules("bob")) == 1, seeded)
check("Seeded rule catches with a Poké Ball, only caught species",
      seeded and seeded[0]["actions"]["mode"] == "catch"
      and seeded[0]["actions"]["ball"] == "specific"
      and seeded[0]["conditions"]["caught_before"] is True, seeded)
check("Old settings are deleted afterwards",
      db.query_one("SELECT 1 FROM setting WHERE key = 'auto_battle_also_catches'") is None)
db.init_db()
check("Migration seeds only once", len(auto_rules.list_rules("alice")) == 1)
clear_rules("alice")
clear_rules("bob")


# --------------------------------------------------------------- Schema
for label, payload in (
    ("unknown type", {"conditions": {"types": ["laser"]}}),
    ("unknown rarity", {"conditions": {"rarity": ["ultra"]}}),
    ("unknown ball", {"actions": {"ball_key": "moon_ball"}}),
    ("unbuyable auto-buy ball", {"actions": {"buy_ball": "master_ball"}}),
    ("unknown mode", {"actions": {"mode": "dance"}}),
    ("generation 0", {"conditions": {"generations": [0]}}),
    ("empty name", {"name": ""}),
):
    try:
        auto_rules.RuleIn.model_validate({"name": "x", **payload})
        check(f"Schema rejects {label}", False)
    except ValueError:
        check(f"Schema rejects {label}", True)
check("Stored rules ignore unknown keys (forward compatible)",
      auto_rules.Conditions.model_validate({"future_key": 1}).level_min is None)
check("Duplicate list values are collapsed",
      auto_rules.Conditions(types=["water", "water"]).types == ["water"])


# ----------------------------------------------------------- Conditions
karp = make_encounter(KARP, 10)
check("Empty conditions match everything", matches({}, karp))
check("Level range", matches({"level_min": 5, "level_max": 10}, karp)
      and not matches({"level_min": 11}, karp) and not matches({"level_max": 9}, karp))
db.execute("UPDATE species SET generation = 1 WHERE id = ?", (KARP["id"],))
check("Generation", matches({"generations": [1, 2]}, karp) and not matches({"generations": [3]}, karp))
check("Rarity tier", matches({"rarity": ["common"]}, karp) and not matches({"rarity": ["legendary"]}, karp))
check("Type any-of", matches({"types": ["water", "fire"]}, karp)
      and not matches({"types": ["fire"]}, karp))
check("Type all-of", not matches({"types": ["water", "flying"], "types_mode": "all"}, karp)
      and matches({"types": ["water"], "types_mode": "all"}, karp))
check("Species include/exclude", matches({"species_include": [KARP["id"]]}, karp)
      and not matches({"species_exclude": [KARP["id"]]}, karp)
      and not matches({"species_include": [PIKA["id"]]}, karp))
check("Shiny", matches({"shiny": False}, karp) and not matches({"shiny": True}, karp))
check("Nature", matches({"natures": [karp["nature"]]}, karp)
      and not matches({"natures": [n for n in spawn.NATURES if n != karp["nature"]][:1]}, karp))
check("Source trigger", matches({"sources": ["binary_sensor.kitchen"]}, karp)
      and not matches({"sources": ["light.hall"]}, karp))
check("Biome", not matches({"biomes": ["beach"]}, karp))
check("Caught before (Pokédex)", matches({"caught_before": False}, karp)
      and not matches({"caught_before": True}, karp))
db.execute("INSERT OR REPLACE INTO dex_entry (species_id, user_id, seen_count, caught_count) "
           "VALUES (?, 'alice', 3, 2)", (KARP["id"],))
check("Caught before after a catch", matches({"caught_before": True}, karp))
check("Caught count max", matches({"caught_count_max": 2}, karp)
      and not matches({"caught_count_max": 1}, karp))
check("Full IVs unlocked", matches({"ivs_maxed": False}, karp)
      and not matches({"ivs_maxed": True}, karp))
check("IV percent (15/31 = 48 %)", matches({"iv_percent_min": 48}, karp)
      and not matches({"iv_percent_min": 49}, karp))
check("Better IVs than best owned (none owned)", matches({"iv_better_than_best": True}, karp))
karp_owned = make_owned("alice", KARP, 5, None)
db.execute("UPDATE owned_pokemon SET ivs = ? WHERE id = ?",
           (json.dumps({k: 20 for k in spawn.STAT_KEYS}), karp_owned))
check("Better IVs than best owned (worse than owned)",
      matches({"iv_better_than_best": False}, karp)
      and not matches({"iv_better_than_best": True}, karp))
db.execute("DELETE FROM owned_pokemon WHERE id = ?", (karp_owned,))
db.execute("UPDATE species SET evolves_from = NULL WHERE id = ?", (KARP["id"],))
db.execute("UPDATE species SET evolves_from = ? WHERE id = ?", (KARP["id"], PIKA["id"]))
check("Evolution stage base", matches({"evo_stage": ["base"]}, karp)
      and not matches({"evo_stage": ["final"]}, karp))
check("Evolution stage final/middle",
      matches({"evo_stage": ["final"]}, make_encounter(PIKA, 10))
      and not matches({"evo_stage": ["middle"]}, karp))
db.execute("UPDATE species SET evolves_from = NULL WHERE id = ?", (PIKA["id"],))
check("Hidden ability (none)", matches({"hidden_ability": False}, karp))
check("Money", matches({"money_min": 5000}, karp) and not matches({"money_min": 5001}, karp))
set_stock("alice", poke_ball=3, great_ball=2)
check("Balls in bag", matches({"ball_stock_min": 5}, karp) and not matches({"ball_stock_min": 6}, karp))
check("Free team slot", matches({"free_team_slot": True}, karp))
check("Team HP", matches({"team_hp_min": 100}, karp))
db.execute("UPDATE owned_pokemon SET current_hp = 1, hp_updated_at = ? WHERE id = ?",
           (NOW.isoformat(), squirt_id))
check("Team HP below threshold", not matches({"team_hp_min": 90}, karp))
heal_all()
check("Level advantage (60 vs 10)", matches({"level_advantage_min": 50}, karp)
      and not matches({"level_advantage_min": 51}, karp))
db.execute("UPDATE owned_pokemon SET current_hp = 0, hp_updated_at = ? WHERE id = ?",
           (NOW.isoformat(), bulba_id))
check("Type advantage (only water Squirtle fit vs water Magikarp: none)",
      matches({"type_advantage": False}, karp))
heal_all()
check("Type advantage (water vs fire)",
      matches({"type_advantage": True}, make_encounter(species("charmander"), 10)))
check("Catch chance", matches({"catch_chance_min": 1}, karp)
      and not matches({"catch_chance_min": 100, "catch_chance_ball": "poke_ball"}, karp))


# ------------------------------------------------------------ Rule order
first = auto_rules.create_rule("alice", rule("first", {"level_min": 50}, {"mode": "skip"}))
second = auto_rules.create_rule("alice", rule("second", {}, {"mode": "skip"}))
disabled = auto_rules.create_rule("alice", rule("off", {}, {"mode": "flee"}, enabled=False))
check("Positions are appended", [r["name"] for r in auto_rules.list_rules("alice")]
      == ["first", "second", "off"])
check("First matching rule wins", auto_rules.match_rule("alice", karp)["name"] == "second")
auto_rules.reorder("alice", [disabled["id"], first["id"]])
check("Reorder keeps unlisted rules at the end",
      [r["name"] for r in auto_rules.list_rules("alice")] == ["off", "first", "second"])
check("Disabled rules are skipped", auto_rules.match_rule("alice", karp)["name"] == "second")
check("Rules are per player", auto_rules.match_rule("bob", karp) is None)
check("Stored JSON keeps only non-defaults",
      json.loads(db.query_one("SELECT actions FROM auto_rule WHERE id = ?",
                              (second["id"],))["actions"]) == {"mode": "skip"})
res = auto_rules.apply_rules("alice", karp)
check("Skip leaves the encounter untouched",
      res["outcome"] == "skipped" and outcome_of(karp["id"]) is None
      and encounter_state.progress(karp["id"], "alice") is None, res)
clear_rules()


# ---------------------------------------------------------------- Defeat
db.set_setting("auto_battle_enabled", True)
money_before = economy.balance("alice")
r_def = auto_rules.create_rule("alice", rule("train", {}, {"mode": "defeat"}))
res = auto_rules.apply_rules("alice", karp)
check("Defeat: Magikarp is beaten", res["outcome"] == "defeated", res["outcome"])
check("Defeat: encounter finished as defeated", outcome_of(karp["id"]) == "defeated")
check("Defeat: money earned", economy.balance("alice") > money_before)
entry = battle_history.recent("alice")[0]
check("Defeat: history names the rule", entry["rule_name"] == "train" and entry["outcome"] == "won"
      and entry["kind"] == battle_history.KIND_AUTO, entry)
log_row = auto_rules.recent_log("alice")[0]
check("Defeat: rule log row", log_row["rule_name"] == "train" and log_row["outcome"] == "defeated",
      log_row)
clear_rules()


# ----------------------------------------------------------------- Catch
heal_all()
set_stock("alice", poke_ball=10)
enc = make_encounter(PIKA, 5)
auto_rules.create_rule("alice", rule("catch", {}, {
    "mode": "catch", "ball": "specific", "ball_key": "poke_ball", "chance_min": 1,
    "max_balls": 3, "to_box": True, "notify": True}))
auto_rules.drain_notifications()
money_before = economy.balance("alice")
with AlwaysThrow(True):
    res = auto_rules.apply_rules("alice", enc)
check("Catch: caught", res["outcome"] == "caught" and res["owned_id"], res["outcome"])
owned = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (res["owned_id"],))
check("Catch: 'to box' keeps it out of the team", owned["party_slot"] is None, owned)
check("Catch: one ball used", stock("alice", "poke_ball") == 9 and res["balls_used"] == {"poke_ball": 1})
check("Catch: catch money only (no win money)",
      res.get("money_earned") and economy.balance("alice") - money_before == res["money_earned"], res)
entry = battle_history.recent("alice")[0]
check("Catch: history caught=True", entry["caught"] is True and entry["outcome"] == "caught", entry)
notes = auto_rules.drain_notifications()
check("Catch: notification queued", len(notes) == 1 and "Pikachu" in notes[0]["message"], notes)
clear_rules()

# Failed catch: stays in the queue, no rewards, logged as NOT caught (old bug).
heal_all()
enc = make_encounter(PIKA, 5)
auto_rules.create_rule("alice", rule("catch", {}, {
    "mode": "catch", "ball": "specific", "ball_key": "poke_ball", "chance_min": 1,
    "max_balls": 2, "on_fail": "leave"}))
money_before = economy.balance("alice")
with AlwaysThrow(False):
    res = auto_rules.apply_rules("alice", enc)
check("Failed catch: left in the queue", res["outcome"] == "left" and outcome_of(enc["id"]) is None,
      res["outcome"])
check("Failed catch: max_balls respected", res["balls_used"] == {"poke_ball": 2}, res["balls_used"])
check("Failed catch: no money", economy.balance("alice") == money_before)
entry = battle_history.recent("alice")[0]
check("Failed catch: history caught=False", entry["caught"] is False and entry["outcome"] == "ran",
      entry)
check("Failed catch: encounter is waiting again",
      db.query_one("SELECT status FROM encounter WHERE id = ?", (enc["id"],))["status"] == "waiting")
clear_rules()

# Weakening: real throws, realistic chance target - only invariants.
heal_all()
set_stock("alice", poke_ball=20)
enc = make_encounter(PIKA, 20)
auto_rules.create_rule("alice", rule("weaken", {}, {
    "mode": "catch", "ball": "specific", "ball_key": "poke_ball", "chance_min": 50,
    "max_balls": 5}))
res = auto_rules.apply_rules("alice", enc)
check("Weaken & catch ends in a defined outcome",
      res["outcome"] in ("caught", "left", "defeated", "lost"), res["outcome"])
check("Weaken & catch used at most max_balls", sum(res["balls_used"].values()) <= 5)
if res["outcome"] == "left":
    hp = encounter_state.progress(enc["id"], "alice")["current_hp"]
    check("Weakened Pokémon keeps its lowered HP", hp is not None and hp > 0, hp)
clear_rules()

# on_fail defeat in battle.
heal_all()
enc = make_encounter(PIKA, 5)
auto_rules.create_rule("alice", rule("c", {}, {"mode": "catch", "ball": "specific",
                                               "chance_min": 1, "max_balls": 1,
                                               "on_fail": "defeat"}))
with AlwaysThrow(False):
    res = auto_rules.apply_rules("alice", enc)
check("on_fail defeat: battled down after the miss", res["outcome"] == "defeated", res["outcome"])
clear_rules()


# ---------------------------------------------------------------- Guards
heal_all()
shiny = make_encounter(PIKA, 5, shiny=1)
auto_rules.create_rule("alice", rule("flee all", {}, {"mode": "flee"}))
res = auto_rules.apply_rules("alice", shiny)
check("Guard: a shiny is never let go by default",
      res["outcome"] == "left" and outcome_of(shiny["id"]) is None
      and encounter_state.progress(shiny["id"], "alice") is None, res["outcome"])
clear_rules()
auto_rules.create_rule("alice", rule("flee all", {}, {"mode": "flee", "allow_flee_special": True}))
res = auto_rules.apply_rules("alice", shiny)
check("Guard can be lifted per rule", res["outcome"] == "fled" and outcome_of(shiny["id"]) == "fled")
clear_rules()
plain = make_encounter(PIKA, 5)
auto_rules.create_rule("alice", rule("flee", {}, {"mode": "flee"}))
check("Flee lets a normal Pokémon go", auto_rules.apply_rules("alice", plain)["outcome"] == "fled")
clear_rules()

set_stock("alice", master_ball=1)
enc = make_encounter(PIKA, 5)
auto_rules.create_rule("alice", rule("best", {}, {"mode": "catch", "ball": "best"}))
res = auto_rules.apply_rules("alice", enc)
check("Guard: Master Ball not used without permission",
      res["outcome"] == "no_ball" and stock("alice", "master_ball") == 1
      and encounter_state.progress(enc["id"], "alice") is None, res["outcome"])
clear_rules()
auto_rules.create_rule("alice", rule("best", {}, {"mode": "throw", "ball": "best",
                                                  "allow_master_ball": True}))
res = auto_rules.apply_rules("alice", enc)
check("Master Ball with permission catches", res["outcome"] == "caught"
      and stock("alice", "master_ball") == 0, res["outcome"])
clear_rules()


# ----------------------------------------------------------- Ball choice
wild = battle.combatant_from_encounter(make_encounter(PIKA, 5))
ctx = battle.CatchContext()
inv = {"poke_ball": 5, "great_ball": 5, "ultra_ball": 5}
A = auto_rules.Actions
check("Ball 'best' picks the highest chance",
      auto_rules.choose_ball(A(ball="best"), wild, ctx, inv)[0] == "ultra_ball")
check("Ball 'cheapest_chance' picks the cheapest that reaches the target",
      auto_rules.choose_ball(A(ball="cheapest_chance", chance_min=1), wild, ctx, inv)[0] == "poke_ball")
check("Ball 'cheapest_chance' falls back to the best when none reaches it",
      auto_rules.choose_ball(A(ball="cheapest_chance", chance_min=100), wild, ctx, inv)[0]
      == "ultra_ball")
check("Ball 'priority' takes the first in stock",
      auto_rules.choose_ball(A(ball="priority", ball_priority=["net_ball", "great_ball"]),
                             wild, ctx, inv)[0] == "great_ball")
check("Excluded balls are never used",
      auto_rules.choose_ball(A(ball="best", ball_exclude=["ultra_ball"]), wild, ctx, inv)[0]
      == "great_ball")
check("Specific ball out of stock -> none",
      auto_rules.choose_ball(A(ball="specific", ball_key="net_ball"), wild, ctx, inv) is None)


# ------------------------------------------------------- Throw & auto-buy
set_stock("alice")
db.execute("UPDATE app_user SET money = 1000, auto_rules_daily_cap = 0 WHERE id = 'alice'")
enc = make_encounter(PIKA, 5)
auto_rules.create_rule("alice", rule("buy", {}, {
    "mode": "throw", "ball": "specific", "ball_key": "poke_ball", "max_balls": 1,
    "buy_ball": "poke_ball", "buy_below": 3, "buy_up_to": 10, "money_reserve": 600}))
price = economy.unit_price(economy.catalog_by_key()["poke_ball"])
with AlwaysThrow(True):
    res = auto_rules.apply_rules("alice", enc)
expected = min(10, 400 // price)
check("Auto-buy respects the money reserve",
      res["bought"] == {"poke_ball": expected} and economy.balance("alice") >= 600, res["bought"])
check("Throw mode catches without a battle",
      res["outcome"] == "caught" and res["exp_details"] == [], res["outcome"])
check("Spent money counts for today", auto_rules.spent_today("alice") == expected * price)
clear_rules()

set_stock("alice")
db.execute("UPDATE app_user SET money = 100000, auto_rules_daily_cap = ? WHERE id = 'alice'",
           (auto_rules.spent_today("alice") + 2 * price,))
enc = make_encounter(PIKA, 5)
auto_rules.create_rule("alice", rule("buy", {}, {
    "mode": "throw", "ball": "specific", "max_balls": 1, "buy_ball": "poke_ball",
    "buy_below": 5, "buy_up_to": 50}))
with AlwaysThrow(False):
    res = auto_rules.apply_rules("alice", enc)
check("Auto-buy respects the daily cap", res["bought"] == {"poke_ball": 2}, res["bought"])
check("Throw miss -> left in queue", res["outcome"] == "left" and outcome_of(enc["id"]) is None)
db.execute("UPDATE app_user SET auto_rules_daily_cap = 0 WHERE id = 'alice'")
clear_rules()

set_stock("alice", poke_ball=5)
enc = make_encounter(PIKA, 5)
auto_rules.create_rule("alice", rule("t", {}, {"mode": "throw", "ball": "specific",
                                               "max_balls": 1, "on_fail": "defeat"}))
with AlwaysThrow(False):
    res = auto_rules.apply_rules("alice", enc)
check("Throw miss with on_fail defeat battles it", res["outcome"] == "defeated", res["outcome"])
clear_rules()


# --------------------------------------------------------- Heal & team
set_stock("alice", poke_ball=5, potion=5, super_potion=5, hyper_potion=5)
db.execute("UPDATE owned_pokemon SET current_hp = 5, hp_updated_at = ? WHERE id = ?",
           (NOW.isoformat(), squirt_id))
enc = make_encounter(KARP, 5)
auto_rules.create_rule("alice", rule("heal", {}, {"mode": "defeat", "heal_below": 90}))
res = auto_rules.apply_rules("alice", enc)
check("Heal first when the team is low", res["healed"], res["healed"])
check("...without the Auto heal switch", not db.user_auto_heal("alice")["auto_heal"])
clear_rules()

heal_all()
box_id = make_owned("alice", PIKA, 30, None)
enc = make_encounter(KARP, 5)
auto_rules.create_rule("alice", rule("lineup", {}, {"mode": "defeat", "team": "lineup",
                                                    "lineup": [box_id]}))
res = auto_rules.apply_rules("alice", enc)
check("Lineup can field a box Pokémon",
      res["outcome"] == "defeated" and [d["owned_id"] for d in res["exp_details"]] == [box_id],
      res["exp_details"])
clear_rules()
rows = auto_rules._team_rows("alice", auto_rules.Actions(team="lowest_level"),
                             battle.combatant_from_encounter(enc))
order = [r["id"] for r in rows if r["id"] in (bulba_id, squirt_id)]
check("Lowest level first", order == [bulba_id, squirt_id] and rows[-1]["id"] == squirt_id, order)

db.execute("UPDATE owned_pokemon SET current_hp = 0, hp_updated_at = ? WHERE user_id = 'alice'",
           (NOW.isoformat(),))
db.set_setting("heal_regen_percent_per_hour", 0)
enc = make_encounter(KARP, 5)
auto_rules.create_rule("alice", rule("d", {}, {"mode": "defeat"}))
res = auto_rules.apply_rules("alice", enc)
check("Fainted team: nothing happens", res["outcome"] == "no_party"
      and encounter_state.progress(enc["id"], "alice") is None, res["outcome"])
heal_all()
clear_rules()


# ------------------------------------------------------------ Entry points
db.set_setting("auto_battle_enabled", False)
try:
    auto_rules.run_for_user("alice")
    check("Run needs the household switch", False)
except auto_rules.AutoRuleError as exc:
    check("Run needs the household switch", exc.status_code == 409)
db.set_setting("auto_battle_enabled", True)

db.execute("UPDATE encounter_user SET outcome = 'fled' WHERE user_id = 'alice' AND outcome IS NULL")
db.execute("UPDATE encounter SET status = 'fled' WHERE user_id = 'alice'")
e1, e2 = make_encounter(KARP, 5), make_encounter(PIKA, 5)
auto_rules.create_rule("alice", rule("karp only", {"species_include": [KARP["id"]]},
                                     {"mode": "defeat"}))
prev = auto_rules.preview("alice")
check("Preview names the matching rule per encounter",
      {p["encounter_id"]: p["rule_name"] for p in prev} == {e1["id"]: "karp only", e2["id"]: None},
      prev)
summary = auto_rules.run_for_user("alice")
check("Run handles matches, leaves the rest", summary["handled"] == 1 and summary["unmatched"] == 1,
      summary)
check("Run: Pikachu still waiting", outcome_of(e2["id"]) is None and outcome_of(e1["id"]) == "defeated")

# Spawn hook.
db.execute("INSERT OR REPLACE INTO trigger_entity (entity_id, friendly_name, enabled, trigger_states, "
           "spawn_chance, type_weights) VALUES ('binary_sensor.hall', 'Hall', 1, '[\"on\"]', 1, '{}')")
trigger = db.query_one("SELECT * FROM trigger_entity WHERE entity_id = 'binary_sensor.hall'")
clear_rules()
auto_rules.create_rule("alice", rule("all", {}, {"mode": "defeat"}))
db.set_setting("global_cooldown_minutes", 0)
db.set_setting("max_spawns_per_day", 1000)


def spawn_once() -> list[dict]:
    before = {r["id"] for r in db.query("SELECT id FROM encounter")}
    spawn.process_events([{"entity_id": "binary_sensor.hall", "state": "on"}], [dict(trigger)],
                         force=True)
    return [r for r in db.query("SELECT * FROM encounter WHERE user_id = 'alice'")
            if r["id"] not in before]


new = spawn_once()
check("Spawn: player switch off -> rules don't run",
      new and all(outcome_of(r["id"]) is None for r in new))
auto_rules.set_user_settings("alice", enabled=True)
new = spawn_once()
check("Spawn: switch on -> rule handles the new encounter",
      new and all(outcome_of(r["id"]) == "defeated" for r in new),
      [outcome_of(r["id"]) for r in new])
db.set_setting("auto_battle_enabled", False)
new = spawn_once()
check("Spawn: household switch off -> rules don't run",
      new and all(outcome_of(r["id"]) is None for r in new))
db.set_setting("auto_battle_enabled", True)


# ----------------------------------------------------- Queue button & API
clear_rules()
enc = make_encounter(KARP, 5)
res = encounters_api.run_auto_battle(enc["id"], USER)
check("Auto-battle button reports 'won'", res["outcome"] == "won", res["outcome"])
entry = battle_history.recent("alice")[0]
check("Auto-battle button: history without rule", entry["rule_name"] is None
      and entry["kind"] == battle_history.KIND_AUTO)
check("Auto-battle button writes no rule log",
      auto_rules.recent_log("alice")[0]["encounter_id"] != enc["id"])

try:
    auto_rules_api.create_rule({"name": "x", "conditions": {"types": ["laser"]}}, USER)
    check("API: invalid rule -> 400", False)
except HTTPException as exc:
    check("API: invalid rule -> 400 with translated text",
          exc.status_code == 400 and "conditions.types" in exc.detail, exc.detail)
created = auto_rules_api.create_from_template("catch_new", USER)
check("API: template creates a named rule", created["name"] == "Catch new species", created["name"])
top = auto_rules_api.create_from_template("shiny_hunter", USER)
check("API: shiny template goes to the top", auto_rules.list_rules("alice")[0]["id"] == top["id"])
try:
    auto_rules_api.create_from_template("nope", USER)
    check("API: unknown template -> 404", False)
except HTTPException as exc:
    check("API: unknown template -> 404", exc.status_code == 404)
updated = auto_rules_api.update_rule(created["id"], {"name": "renamed", "enabled": False}, USER)
check("API: update", updated["name"] == "renamed" and updated["enabled"] is False)
try:
    auto_rules_api.update_rule(top["id"], {"name": "x"}, {"id": "bob", "language": "en"})
    check("API: other player's rule -> 404", False)
except HTTPException as exc:
    check("API: other player's rule -> 404", exc.status_code == 404)
auto_rules_api.delete_rule(created["id"], USER)
check("API: delete", [r["id"] for r in auto_rules.list_rules("alice")] == [top["id"]])
got = auto_rules_api.get_rules(USER)
check("API: overview has settings, rules and catalog",
      got["settings"]["enabled"] is True and got["rules"] and "balls" in got["catalog"])
check("Catalog lists all 18 types", len(auto_rules.ALL_TYPES) == 18, auto_rules.ALL_TYPES)


print()
if failures:
    print(f"{len(failures)} FAIL")
    sys.exit(1)
print("all ok")
