"""Tests fuer die Roaming-Legendaries (`app/roaming.py`): sie fliehen statt
gefangen oder besiegt zu werden.

Deckt ab: Engine (Flucht am eigenen Zug, Schlaf/Faenger halten sie fest,
Schaden bleibt beim Fliehen), Wurf aus der Warteschlange (Flucht nach dem
ersten Fehlwurf), Kampf-Session und Auto-Rules, und dass der Schaden beim
naechsten Erscheinen derselben Art wieder da ist.

Aufruf:  python3 tests/test_roaming.py
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

TMP = tempfile.mkdtemp(prefix="pokemon-roaming-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import auto_rules, battle, db, encounter_state, roaming, seed, spawn  # noqa: E402
from app.api import battles as battles_api  # noqa: E402
from app.api import encounters as encounters_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


random.seed(int(os.environ.get("TEST_SEED", "4711")))
db.init_db()
seed.seed_from_fallback()

IVS = {k: 15 for k in spawn.STAT_KEYS}
NOW = datetime.now().astimezone()


def species(name: str) -> dict:
    return db.query_one("SELECT * FROM species WHERE name = ?", (name,))


PIKA, KARP, CHAR = species("pikachu"), species("magikarp"), species("charmander")


def fighter(sp: dict, level: int, moves: list[str], spe: int | None = None,
            wild: bool = False, roams: bool = False) -> battle.Combatant:
    """Combatant with explicit moves and (optionally) a fixed base speed, so the
    turn order is known in advance."""
    data = dict(sp, base_spe=spe) if spe is not None else sp
    return battle.Combatant(species=data, level=level, ivs=dict(IVS), is_wild=wild,
                            roams=roams, moves=moves)


class fixed_rolls:
    """Every `random.random()` returns `value` (0.0 = every accuracy/status roll
    succeeds). Restored on exit."""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __enter__(self) -> None:
        self.original = random.random
        random.random = lambda: self.value

    def __exit__(self, *exc) -> None:
        random.random = self.original


def battle_of(player: battle.Combatant, wild: battle.Combatant) -> battle.Battle:
    return battle.Battle(wild=wild, team=[player], active=0, lang="en")


# ------------------------------------------------------------ Auswahl
check("Raikou ist ein Roaming-Legendary", roaming.is_roaming(243))
check("Bisasam ist keins", not roaming.is_roaming(1))
check("Genau acht Roaming-Legendaries", len(roaming.ROAMING_IDS) == 8, str(len(roaming.ROAMING_IDS)))

# ------------------------------------------------------- Engine: Flucht
# Roamer schneller als das eigene Pokemon: flieht, bevor der eigene Angriff
# dran ist - ohne Schaden.
with fixed_rolls():
    b = battle_of(fighter(PIKA, 5, ["tackle"], spe=5),
                  fighter(KARP, 30, ["tackle"], spe=200, wild=True, roams=True))
    battle.run_turn(b, "tackle")
    check("Schnellerer Roamer flieht vor dem eigenen Angriff", b.outcome == "fled", str(b.outcome))
    check("Ohne Angriff kein Schaden am Roamer", b.wild.current_hp == b.wild.max_hp)
    check("Log nennt die Flucht", any("runs away" in line for line in b.log), str(b.log[-2:]))

# Eigenes Pokemon schneller: der Angriff trifft, danach flieht der Roamer -
# der Schaden bleibt.
with fixed_rolls():
    b = battle_of(fighter(PIKA, 30, ["tackle"], spe=200),
                  fighter(KARP, 30, ["tackle"], spe=5, wild=True, roams=True))
    battle.run_turn(b, "tackle")
    check("Langsamerer Roamer flieht am eigenen Zug", b.outcome == "fled", str(b.outcome))
    check("Angriff vor der Flucht hat Schaden gemacht", b.wild.current_hp < b.wild.max_hp,
          f"{b.wild.current_hp}/{b.wild.max_hp}")

# Ein normales wildes Pokemon flieht nie.
with fixed_rolls():
    b = battle_of(fighter(PIKA, 30, ["tackle"], spe=200),
                  fighter(KARP, 30, ["tackle"], spe=5, wild=True))
    battle.run_turn(b, "tackle")
    check("Kein Roamer - keine Flucht", b.outcome is None, str(b.outcome))

# Wurf im Kampf (player_move None): der Roamer ist am Zug und haut ab.
with fixed_rolls():
    b = battle_of(fighter(PIKA, 30, ["tackle"], spe=200),
                  fighter(KARP, 30, ["tackle"], spe=5, wild=True, roams=True))
    battle.run_turn(b, None)
    check("Fehlwurf im Kampf: Roamer flieht sofort", b.outcome == "fled", str(b.outcome))

# Hypnose: der Roamer schlaeft und flieht solange nicht; nach dem Aufwachen
# handelt er wieder - und haut dann ab.
with fixed_rolls():
    b = battle_of(fighter(PIKA, 30, ["hypnosis", "tackle"], spe=200),
                  fighter(KARP, 30, ["tackle"], spe=5, wild=True, roams=True))
    battle.run_turn(b, "hypnosis")
    check("Hypnose schlaefert den Roamer ein", b.wild.status == "sleep", str(b.wild.status))
    check("Schlafender Roamer flieht nicht", b.outcome is None, str(b.outcome))
    turns_asleep = 0
    while b.wild.status == "sleep" and b.outcome is None and turns_asleep < 10:
        battle.run_turn(b, "tackle")
        turns_asleep += 1
        if b.wild.status == "sleep":
            check("Solange er schlaeft, bleibt er da", b.outcome is None, str(b.outcome))
    check("Nach dem Aufwachen flieht er", b.outcome == "fled", str(b.outcome))

# Mean Look: festgehalten - der Roamer kaempft weiter statt abzuhauen.
with fixed_rolls():
    b = battle_of(fighter(PIKA, 30, ["mean_look", "tackle"], spe=200),
                  fighter(KARP, 30, ["tackle"], spe=5, wild=True, roams=True))
    battle.run_turn(b, "mean_look")
    check("Mean Look haelt den Roamer fest", b.wild.trapped, str(b.wild.trapped))
    check("Festgehaltener Roamer flieht nicht", b.outcome is None, str(b.outcome))
    battle.run_turn(b, "tackle")
    check("Auch in der Folgerunde bleibt er da", b.outcome is None, str(b.outcome))

# Mean Look gegen ein normales Pokemon: wirkungslos, Kampf laeuft normal.
with fixed_rolls():
    b = battle_of(fighter(PIKA, 30, ["mean_look", "tackle"], spe=200),
                  fighter(KARP, 30, ["tackle"], spe=5, wild=True))
    battle.run_turn(b, "mean_look")
    check("Mean Look bei normalem Wildpokemon ohne Wirkung", not b.wild.trapped)

# Die Flucht ist ein Teil des Zustands, der die Session speichert.
check("Trap-Attacken sind im Katalog", battle.moves_mod.get("mean_look").get("trap") is True
      and battle.moves_mod.get("spider_web").get("trap") is True)

# --------------------------------------------------- Datenbank-Teile
# Ab hier ist Pikachu (Warteschlange, Session) / Karpador (Session, Spawn)
# ein Roamer - die Liste ist sonst nur die echten Legendaries.
roaming.ROAMING_IDS = roaming.ROAMING_IDS | {KARP["id"]}

alice = db.upsert_user("alice", "Alice")


def make_owned(user_id: str, sp: dict, level: int) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "current_hp, party_slot, caught_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, sp["id"], level, battle.exp_for_level(level, sp["growth_rate"]), 0,
         json.dumps(IVS), "hardy", None, 1, NOW.isoformat()),
    )


def give(user_id: str, item: str, quantity: int) -> None:
    db.execute(
        "INSERT INTO inventory (item_key, user_id, quantity) VALUES (?,?,?) "
        "ON CONFLICT(item_key, user_id) DO UPDATE SET quantity = excluded.quantity",
        (item, user_id, quantity),
    )


def encounter_of(sp: dict, level: int, user_id: str) -> int:
    encounter_id = spawn.create_encounter(sp, source_entity="test.trigger", user_id=user_id)
    db.execute("UPDATE encounter SET level = ?, ivs = ? WHERE id = ?",
               (level, json.dumps(IVS), encounter_id))
    return encounter_id


def enc(encounter_id: int) -> dict:
    return db.query_one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))


def queue_ids(user: dict) -> list[int]:
    return [e["id"] for e in encounters_api.list_encounters("waiting", 100, user)]


def fixed_miss(*_args, **_kwargs) -> battle.CatchResult:
    return battle.CatchResult(caught=False, critical=False, shakes=0, probability=0.1)


give(alice["id"], "poke_ball", 20)
original_throw = battle.throw_ball
battle.throw_ball = fixed_miss

# Queue: Fehlwurf auf einen Roamer -> er flieht, Versuch zu, aus der Warteschlange.
roamer_queue = encounter_of(KARP, 20, alice["id"])
result = encounters_api.throw(roamer_queue, encounters_api.BallThrow(ball="poke_ball"), alice)
check("Queue: Fehlwurf auf Roamer meldet Flucht", result.get("fled") is True, str(result))
check("Queue: Flucht schliesst den Encounter fuer den Besitzer", result.get("closed") is True)
check("Queue: Encounter-Status ist fled", enc(roamer_queue)["status"] == "fled", enc(roamer_queue)["status"])
check("Queue: Roamer verschwindet aus der Warteschlange", roamer_queue not in queue_ids(alice))

# Queue: Fehlwurf auf ein normales Pokemon bleibt in der Warteschlange.
normal_queue = encounter_of(CHAR, 20, alice["id"])
result = encounters_api.throw(normal_queue, encounters_api.BallThrow(ball="poke_ball"), alice)
check("Queue: normaler Fehlwurf flieht nicht", not result.get("fled"), str(result))
check("Queue: normales Pokemon bleibt in der Warteschlange", normal_queue in queue_ids(alice))
battle.throw_ball = original_throw

# Kampf: Karpador (Roamer) ist langsamer als Pikachu - der Angriff trifft,
# danach flieht es. Der Schaden wird fuer den naechsten Spawn gemerkt.
# Pikachu (Lv. 30) ist hier schneller als Karpador (Lv. 30); der schwaechste
# Schadenszug reicht nicht zum K.o., sodass der Roamer danach noch flieht.
# Wurf 0.5 statt 0.0: sonst wuerde jeder Nebeneffekt-Wurf greifen (Laehmung
# haette den Roamer am Fliehen gehindert - das ist gewollt, aber hier nicht
# Gegenstand des Tests).
make_owned(alice["id"], PIKA, 30)
battle_enc = encounter_of(KARP, 30, alice["id"])
with fixed_rolls(0.5):
    view = battles_api.start_battle(battle_enc, alice)
    weakest = min((m for m in view["team"][0]["moves"] if m.get("power")), key=lambda m: m["power"])
    view = battles_api.battle_action(view["id"], battles_api.BattleAction(
        action="move", move=weakest["key"]), alice)
check("Kampf: Roamer flieht nach dem Angriff", view["outcome"] == "fled", str(view.get("outcome")))
check("Kampf: Ergebnis markiert die Flucht", (view.get("result") or {}).get("escaped") is True,
      str(view.get("result")))
check("Kampf: Session-Status fled", db.query_one(
    "SELECT status FROM battle_session WHERE id = ?", (view["id"],))["status"] == "fled")
check("Kampf: Encounter fuer den Besitzer geschlossen", enc(battle_enc)["status"] == "fled")
check("Kampf: Versuch steht als fled in encounter_user",
      encounter_state.progress(battle_enc, alice["id"])["outcome"] == "fled")

wound = db.query_one("SELECT hp_fraction FROM roamer_wound WHERE user_id = ? AND species_id = ?",
                     (alice["id"], KARP["id"]))
check("Kampf: beschaedigter Roamer ist als Wunde gemerkt",
      wound is not None and 0 < wound["hp_fraction"] < 1, str(wound and dict(wound)))
fraction = wound["hp_fraction"]

# Naechstes Erscheinen derselben Art startet mit dem gemerkten Schaden und
# verbraucht die Notiz.
spawned = spawn.create_encounter(KARP, source_entity="test.trigger", user_id=alice["id"])
row = enc(spawned)
max_hp = battle.combatant_from_encounter(dict(row, current_hp=None)).max_hp
expected = max(1, round(max_hp * fraction))
check("Spawn: Roamer kommt mit gemerktem Schaden", row["current_hp"] == expected,
      f"{row['current_hp']} erwartet {expected} von {max_hp}")
check("Spawn: Notiz ist verbraucht", db.query_one(
    "SELECT 1 FROM roamer_wound WHERE user_id = ? AND species_id = ?",
    (alice["id"], KARP["id"])) is None)
check("Spawn: ohne Notiz startet ein Roamer voll", roaming.take_wound(alice["id"], KARP["id"]) is None)

# Voll geheilt geflohen -> keine Notiz mehr.
roaming.remember(alice["id"], KARP["id"], 10, 10)
check("Volle KP beim Fliehen hinterlassen keine Notiz", db.query_one(
    "SELECT 1 FROM roamer_wound WHERE user_id = ? AND species_id = ?",
    (alice["id"], KARP["id"])) is None)

# Auto-Rules: Wurf-Modus flieht ebenfalls - der Roamer wird nicht weiter beworfen.
battle.throw_ball = fixed_miss
give(alice["id"], "poke_ball", 20)
auto_enc = encounter_of(KARP, 20, alice["id"])
result = auto_rules.execute(alice["id"], enc(auto_enc), auto_rules.Actions(mode="throw"))
check("Auto-Rules: Wurf auf Roamer endet als fled", result["outcome"] == "fled", result["outcome"])
check("Auto-Rules: nur ein Ball verbraucht", sum(result["balls_used"].values()) == 1,
      str(result["balls_used"]))
check("Auto-Rules: Encounter geschlossen", enc(auto_enc)["status"] == "fled")
battle.throw_ball = original_throw

if failures:
    print(f"\n{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("\nAlle Roaming-Tests bestanden.")
