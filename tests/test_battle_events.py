"""Tests for the battle event hub (battle_events.py): dispatch per
(event, effect), veto, modify, dry run without a battle, nesting limit and
registration of all implemented catalog abilities.

Run:  python3 tests/test_battle_events.py
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-battle-events-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import abilities, battle, battle_events, db, seed, spawn  # noqa: E402
from app.battle_events import Event, emit, on  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


random.seed(int(os.environ.get("TEST_SEED", "1234")))
db.init_db()
seed.seed_from_fallback()
IVS = {k: 15 for k in spawn.STAT_KEYS}

db.execute(
    """
    INSERT INTO species (id, name, name_de, type1, type2, generation, catch_rate,
        base_exp, growth_rate, base_hp, base_atk, base_def, base_spa, base_spd, base_spe)
    VALUES (90201,'test-normal','test-normal','normal',NULL,1,45,64,'medium_fast',100,80,80,80,80,80)
    ON CONFLICT(id) DO NOTHING
    """
)
normal_sp = db.query_one("SELECT * FROM species WHERE id = 90201")


def mon(ability: str | None = None) -> battle.Combatant:
    return battle.Combatant(species=normal_sp, level=30, ivs=dict(IVS), ability=ability)


# Test-only abilities: injected into the loaded catalog, removed at the end.
cat = abilities.catalog()
cat["test_double"] = {"key": "test_double", "name": "Test Double", "effect": "test_double"}
cat["test_veto"] = {"key": "test_veto", "name": "Test Veto", "effect": "test_veto"}
cat["test_loop"] = {"key": "test_loop", "name": "Test Loop", "effect": "test_loop"}

calls: list[str] = []


@on(Event.DAMAGE_MULT, "test_double")
def _double(ctx):
    calls.append("double")
    ctx.value *= float(ctx.params.get("multiplier", 2.0))


@on(Event.BEFORE_STATUS, "test_veto")
def _veto(ctx):
    ctx.cancelled = True
    ctx.log("battle.log.status_immune_ability", who="x", ability="y")


loop_depth = {"max": 0}


@on(Event.END_OF_TURN, "test_loop")
def _loop(ctx):
    loop_depth["max"] += 1
    emit(Event.END_OF_TURN, ctx.owner, battle=ctx.battle)


try:
    # ------------------------------------------------------------ Dispatch
    ctx = emit(Event.DAMAGE_MULT, mon("test_double"))
    check("modify: handler changes ctx.value", ctx.value == 2.0 and calls == ["double"], str(ctx.value))
    check("modify: handler gets the catalog entry as params", ctx.params.get("key") == "test_double")

    calls.clear()
    ctx = emit(Event.STAB, mon("test_double"), value=1.5)
    check("No handler for this event -> value unchanged", ctx.value == 1.5 and calls == [])

    for ability in (None, "totally_unknown_slug"):
        ctx = emit(Event.DAMAGE_MULT, mon(ability), value=1.25)
        check(f"Ability {ability!r} without catalog entry is a no-op",
              ctx.value == 1.25 and not ctx.cancelled and not ctx.flags)

    # ----------------------------------------------------- Veto + dry run
    target = mon("test_veto")
    check("veto: dry run without battle cancels without crashing",
          emit(Event.BEFORE_STATUS, target, status="sleep").cancelled)
    check("veto: engine helper _ability_blocks_status uses the hub",
          battle._ability_blocks_status(target, "sleep"))

    b = battle.Battle(wild=mon(), team=[target])
    check("veto: inflict_status fails", battle.inflict_status(b, target, "sleep") is False)
    check("veto: status stays empty", target.status is None)
    check("veto: handler writes exactly one log line with a running battle", len(b.log) == 1, str(b.log))

    # ------------------------------------------------------ Nesting limit
    looper = mon("test_loop")
    b = battle.Battle(wild=mon(), team=[looper])
    emit(Event.END_OF_TURN, looper, battle=b)
    check("Nesting limit stops self-triggering handlers",
          loop_depth["max"] == battle_events.MAX_DEPTH, str(loop_depth["max"]))
    check("Nesting depth is back to 0 afterwards", battle_events._depth.get() == 0)

    # ------------------------------------- Registration of the real catalog
    real = {k: v for k, v in cat.items() if not k.startswith("test_")}
    registered = {effect for (_event, effect) in battle_events._HANDLERS}
    missing = sorted({a["effect"] for a in real.values() if a.get("effect")} - registered)
    check("Every effect in abilities.json has at least one handler", not missing, str(missing))

    # Guts via flags: burn penalty is skipped, boost applied.
    burned = mon("guts")
    burned.status = "burn"
    ctx = emit(Event.EFFECTIVE_STAT, burned, stat="atk")
    check("Guts: marks ignore_burn and boosts by 1.5",
          "ignore_burn" in ctx.flags and ctx.value == 1.5, f"{ctx.flags} {ctx.value}")
    ctx = emit(Event.EFFECTIVE_STAT, burned, stat="def")
    check("Guts: no effect on other stats", not ctx.flags and ctx.value == 1.0)

    # Contact status only with a running battle (dry run must not roll dice).
    static_holder = mon("static")
    attacker = mon()
    saved = random.random
    random.random = lambda: (_ for _ in ()).throw(AssertionError("dice rolled in dry run"))
    try:
        emit(Event.DAMAGE_TAKEN, static_holder, attacker,
             move={"category": "physical", "type": "normal"}, dealt=10)
        check("Contact status: dry run neither rolls dice nor inflicts status", attacker.status is None)
    except AssertionError as exc:
        check("Contact status: dry run neither rolls dice nor inflicts status", False, str(exc))
    finally:
        random.random = saved

    b = battle.Battle(wild=static_holder, team=[attacker])
    random.random = lambda: 0.0
    try:
        emit(Event.DAMAGE_TAKEN, static_holder, attacker, battle=b,
             move={"category": "physical", "type": "normal"}, dealt=10)
    finally:
        random.random = saved
    check("Contact status: Static paralyzes the attacker (chance forced)", attacker.status == "paralysis")
finally:
    for key in ("test_double", "test_veto", "test_loop"):
        cat.pop(key, None)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print("  -", label)
    sys.exit(1)
print("All tests passed.")
