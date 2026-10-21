"""Tests fuer den Kampfstil (Set / Shift) bei Trainerkaempfen: pro Nutzer
waehlbar. Shift bietet nach jedem besiegten Gegner-Pokémon einen kostenlosen
Wechsel an, Set laesst den Kampf sofort weitergehen (bisheriges Verhalten).

Aufruf:  python3 tests/test_battle_style.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-battle-style-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, db, i18n, seed, spawn, trainer_battle, trainers  # noqa: E402
from app.api import users as users_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


# ---------------------------------------------------------------- Migration
# Eine Datenbank aus der Zeit VOR der Option: app_user ohne Spalte battle_style.
legacy = Path(TMP) / "legacy.sqlite"
conn = sqlite3.connect(legacy)
conn.row_factory = sqlite3.Row
conn.executescript(
    "CREATE TABLE app_user (id TEXT PRIMARY KEY, name TEXT NOT NULL, display_name TEXT,"
    " starter_species_id INTEGER, language TEXT NOT NULL DEFAULT 'en',"
    " created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);"
    "INSERT INTO app_user (id, name, created_at, last_seen_at) VALUES ('old','Old','x','x');"
)
db._migrate_battle_style(conn)
check("Migration legt die Spalte battle_style an", "battle_style" in db._table_columns(conn, "app_user"))
check("Bestehende Nutzer bleiben nach der Migration bei 'set' (bisheriges Verhalten)",
      conn.execute("SELECT battle_style FROM app_user WHERE id='old'").fetchone()[0] == "set")
conn.execute("UPDATE app_user SET battle_style = 'shift' WHERE id='old'")
db._migrate_battle_style(conn)
check("Migration ist idempotent und ueberschreibt keine Wahl",
      conn.execute("SELECT battle_style FROM app_user WHERE id='old'").fetchone()[0] == "shift")
conn.close()

# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")
now = datetime.now().astimezone()
pika = db.query_one("SELECT * FROM species WHERE name = 'pikachu'")

check("Neue Nutzer starten mit 'set'", db.user_battle_style("alice") == "set")
check("Unbekannter Nutzer faellt auf den Standard zurueck", db.user_battle_style("nobody") == "set")
try:
    db.set_user_battle_style("alice", "bogus")
    check("Ungueltiger Stil wird abgelehnt", False)
except ValueError:
    check("Ungueltiger Stil wird abgelehnt", True)

check("/users/me meldet Stil und Auswahl",
      users_api.me(current_user=db.get_user("alice"), is_admin=False)["battle_styles"] == ["set", "shift"])
res = users_api.set_battle_style(users_api.BattleStyleChoice(battle_style="shift"),
                                 current_user=db.get_user("bob"))
check("PUT battle-style speichert den Stil pro Nutzer",
      res == {"battle_style": "shift"} and db.user_battle_style("bob") == "shift"
      and db.user_battle_style("alice") == "set")
try:
    users_api.set_battle_style(users_api.BattleStyleChoice(battle_style="bogus"),
                               current_user=db.get_user("bob"))
    check("PUT mit ungueltigem Stil -> 400", False)
except HTTPException as exc:
    check("PUT mit ungueltigem Stil -> 400", exc.status_code == 400)
check("i18n-Schluessel existieren in en und de",
      all(i18n.t(lang, key) != key for lang in ("en", "de")
          for key in ("battle_style.title", "battle_style.set", "battle_style.shift",
                      "battle_style.desc.set", "battle_style.desc.shift", "battle_style.hint",
                      "battle.log.shift_offer", "battle.shift_offer", "battle.switch_free_shift",
                      "error.invalid_battle_style")))

# Synthetischer Trainer (siehe test_league.py): zwei Karpfen, die nie schaden.
_saved_raw, _saved_trainers = trainers._raw, trainers._trainers
trainers._raw = {
    "regions": [{"key": "kanto", "label": "Kanto"}],
    "trainers": [{
        "id": "test_two", "kind": "gym_leader", "region": "kanto",
        "name": "Testo", "team": [["magikarp", 3], ["magikarp", 3]],
    }],
}
trainers._trainers = None


def give_team(user_id: str, size: int) -> list[int]:
    ids = []
    for slot in range(1, size + 1):
        ids.append(db.execute(
            "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
            "caught_at, party_slot) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, pika["id"], 25, battle.exp_for_level(25), 0,
             json.dumps({k: 31 for k in spawn.STAT_KEYS}), "hardy", now.isoformat(), slot),
        ))
    return ids


def first_move(view: dict) -> str:
    return next(m for m in view["team"] if m["active"])["moves"][0]["key"]


def fight_until_first_ko(user_id: str, view: dict) -> dict:
    for _ in range(15):
        if view["over"] or view["opponents"][0]["fainted"]:
            break
        view = trainer_battle.act(view["id"], user_id, {"action": "move", "move": first_move(view)})
    return view


def other_member(view: dict) -> int:
    return next(m["owned_id"] for m in view["team"] if not m["active"] and not m["fainted"])


give_team("alice", 2)
give_team("bob", 2)
db.upsert_user("carol", "Carol")
give_team("carol", 1)

# ------------------------------------------------------------ Set (Standard)
view = trainer_battle.start("test_two", "alice")
view = fight_until_first_ko("alice", view)
check("set: nach dem ersten KO ist der naechste Gegner draussen", view["opponents"][0]["fainted"]
      and not view["over"], view["opponents"])
check("set: kein Wechsel-Angebot", view["shift_offer"] is False)
turn_before = view["turn"]
view = trainer_battle.act(view["id"], "alice", {"action": "switch", "target": other_member(view)})
check("set: Auswechseln kostet die Runde", view["turn"] == turn_before + 1, (turn_before, view["turn"]))
trainer_battle.act(view["id"], "alice", {"action": "run"})

# ---------------------------------------------------------------- Shift
view = trainer_battle.start("test_two", "bob")
check("shift: vor dem KO gibt es kein Angebot", view["shift_offer"] is False)
view = fight_until_first_ko("bob", view)
check("shift: nach dem KO wird ein Wechsel angeboten", view["shift_offer"] is True
      and not view["needs_switch"] and not view["over"])
check("shift: das Angebot steht im Kampflog", i18n.t("en", "battle.log.shift_offer") in view["log"])
db.set_user_battle_style("bob", "set")  # Stilwechsel mitten im Kampf: Session behaelt 'shift'
view = trainer_battle.get_view(view["id"], "bob")
check("shift: das Angebot ueberlebt das Neuladen der Session", view["shift_offer"] is True)

turn_before = view["turn"]
target = other_member(view)
view = trainer_battle.act(view["id"], "bob", {"action": "switch", "target": target})
check("shift: der Wechsel ist kostenlos (keine Runde vergangen)", view["turn"] == turn_before,
      (turn_before, view["turn"]))
check("shift: das eingewechselte Pokémon ist aktiv und unversehrt",
      view["active"] == target and next(m for m in view["team"] if m["active"])["current_hp"]
      == next(m for m in view["team"] if m["active"])["max_hp"])
check("shift: das Angebot ist danach verbraucht", view["shift_offer"] is False)
try:
    trainer_battle.act(view["id"], "bob", {"action": "switch", "target": other_member(view)})
    switched_again = True
except HTTPException:
    switched_again = False
check("shift: ein zweiter Wechsel ist wieder ein normaler Wechsel (kostet die Runde)",
      switched_again and trainer_battle.get_view(view["id"], "bob")["turn"] == turn_before + 1)

view = trainer_battle.get_view(view["id"], "bob")
for _ in range(15):
    if view["over"]:
        break
    view = trainer_battle.act(view["id"], "bob", {"action": "move", "move": first_move(view)})
check("shift: beim letzten Gegner gibt es kein Angebot, Kampf gewonnen",
      view["over"] and view["outcome"] == "won" and view["shift_offer"] is False, view.get("outcome"))

# Angebot ablehnen: einfach eine Attacke waehlen beendet es.
db.set_user_battle_style("bob", "shift")
view = trainer_battle.start("test_two", "bob")
view = fight_until_first_ko("bob", view)
check("shift: zweiter Kampf bietet wieder einen Wechsel an", view["shift_offer"] is True)
view = trainer_battle.act(view["id"], "bob", {"action": "move", "move": first_move(view)})
check("shift: eine Attacke lehnt das Angebot ab", view["shift_offer"] is False)
if not view["over"]:  # the move may already have beaten the last opponent
    trainer_battle.act(view["id"], "bob", {"action": "run"})

# Kein Angebot, wenn es niemanden zum Wechseln gibt.
db.set_user_battle_style("carol", "shift")
view = trainer_battle.start("test_two", "carol")
view = fight_until_first_ko("carol", view)
check("shift: ohne weiteres einsatzfaehiges Teammitglied kein Angebot",
      view["shift_offer"] is False and not view["over"])
trainer_battle.act(view["id"], "carol", {"action": "run"})

# Engine: ein Wechsel-Angebot ohne shift_mode entsteht nie (Wildkaempfe, Set).
species = db.query_one("SELECT * FROM species WHERE name = 'magikarp'")
strong = battle.Combatant(species=pika, level=50, ivs={k: 31 for k in spawn.STAT_KEYS}, owned_id=1)
backup = battle.Combatant(species=pika, level=50, ivs={k: 31 for k in spawn.STAT_KEYS}, owned_id=2)
foes = [battle.Combatant(species=species, level=1, ivs={k: 0 for k in spawn.STAT_KEYS}, trainer="T")
        for _ in range(2)]
for shift_mode in (False, True):
    b = battle.Battle(wild=foes[0], team=[strong, backup], bench=[foes[1]], shift_mode=shift_mode)
    foes[0].current_hp = 0
    battle._check_faints(b)
    check(f"Engine: shift_mode={shift_mode} -> shift_offer={shift_mode}", b.shift_offer is shift_mode)
    foes[0].current_hp, foes[1].current_hp = foes[0].max_hp, foes[1].max_hp

trainers._raw, trainers._trainers = _saved_raw, _saved_trainers

print()
if failures:
    print(f"{len(failures)} FAIL")
    sys.exit(1)
print("all ok")
