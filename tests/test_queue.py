"""Tests fuer die Warteschlange PRO NUTZER.

Deckt ab: Ball als allererste Aktion, `db.eligible_users`, Fortschritt und
Ausgang je Nutzer (`app/encounter_state.py`), wann ein Encounter die
Warteschlange endgueltig verlaesst - und zwar in beiden Varianten: eigener
Encounter (`encounter.user_id` gesetzt) und alter, haushaltsweit geteilter
Encounter (`user_id` NULL, wie in bestehenden Datenbanken).

Aufruf:  python3 tests/test_queue.py
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-queue-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, db, encounter_state, seed, spawn  # noqa: E402
from app.api import battles as battles_api  # noqa: E402
from app.api import encounters as encounters_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def expect_http(label: str, status: int, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
        check(label, False, "keine Exception")
    except HTTPException as exc:
        check(label, exc.status_code == status, f"{exc.status_code}: {exc.detail}")


random.seed(int(os.environ.get("TEST_SEED", "4711")))
db.init_db()
seed.seed_from_fallback()

IVS = {k: 15 for k in spawn.STAT_KEYS}
NOW = datetime.now().astimezone()

alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")


def species(name: str) -> dict:
    return db.query_one("SELECT * FROM species WHERE name = ?", (name,))


PIKA, KARP, CHAR = species("pikachu"), species("magikarp"), species("charmander")


def make_owned(user_id: str, sp: dict, level: int, slot: int | None = 1) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "current_hp, party_slot, caught_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, sp["id"], level, battle.exp_for_level(level, sp["growth_rate"]), 0,
         json.dumps(IVS), "hardy", None, slot, NOW.isoformat()),
    )


def make_encounter(sp: dict, level: int, user_id: str | None = None) -> int:
    """Ohne `user_id` ein alter, haushaltsweit geteilter Encounter."""
    encounter_id = spawn.create_encounter(sp, source_entity="test.trigger", user_id=user_id)
    db.execute("UPDATE encounter SET level = ?, ivs = ? WHERE id = ?",
               (level, json.dumps(IVS), encounter_id))
    return encounter_id


def enc(encounter_id: int) -> dict:
    return db.query_one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))


def queue(user: dict) -> list[int]:
    return [e["id"] for e in encounters_api.list_encounters("waiting", 100, user)]


def give(user_id: str, item: str, quantity: int) -> None:
    db.execute(
        "INSERT INTO inventory (item_key, user_id, quantity) VALUES (?,?,?) "
        "ON CONFLICT(item_key, user_id) DO UPDATE SET quantity = excluded.quantity",
        (item, user_id, quantity),
    )


def act(battle_id: int, user: dict, **kwargs) -> dict:
    return battles_api.battle_action(battle_id, battles_api.BattleAction(**kwargs), user)


# ---------------------------------------------------- Berechtigte Nutzer
db.upsert_user("tester", "HA Testkonto")
db.upsert_user("t2", "someone", "Test-Zweitgeraet")
old = db.upsert_user("oldie", "Oldie")
db.execute("UPDATE app_user SET last_seen_at = ? WHERE id = 'oldie'",
           ((NOW - timedelta(days=30)).isoformat(),))
eligible = {user["id"] for user in db.eligible_users()}
check("eligible_users: aktive Nutzer zaehlen", {"alice", "bob"} <= eligible)
check("eligible_users: 'test' im Namen zaehlt nicht", "tester" not in eligible)
check("eligible_users: 'Test' im Anzeigenamen zaehlt nicht", "t2" not in eligible)
check("eligible_users: seit ueber 7 Tagen weg zaehlt nicht", "oldie" not in eligible, str(eligible))
check("eligible_users: genau Alice und Bob", eligible == {"alice", "bob"}, str(eligible))


# -------------------------------------------------- Ball als erste Aktion
make_owned("alice", PIKA, 100)
make_owned("bob", CHAR, 100)
give("alice", "master_ball", 5)
give("bob", "master_ball", 5)
give("alice", "poke_ball", 5)

e1 = make_encounter(KARP, 5)  # neue Art fuer Alice - frueher erst kaempfen
view = battles_api.start_battle(e1, alice)
check("Kampf: Ballauswahl ist von Anfang an frei", all(b["count"] >= 0 for b in view["balls"]))
view = act(view["id"], alice, action="ball", ball="master_ball")
check("Ball darf die allererste Aktion sein (Lv.-100-Team)",
      view["outcome"] == "caught" and view["fought"] is False, str(view["outcome"]))
check("Fang ohne Kampf: Encounter fuer Alice erledigt",
      encounter_state.progress(e1, "alice")["outcome"] == "caught")
check("Einstellung 'erst kaempfen' ist weg",
      "require_battle_for_new_species" not in db.all_settings()
      and db.get_setting("require_battle_for_known_species") is None)

e2 = make_encounter(KARP, 5)
res = encounters_api.throw(e2, encounters_api.BallThrow(ball="master_ball"), alice)
check("Auch aus der Warteschlange faengt der erste Ball", res["caught"] is True)


# ------------------------------------------- Jeder Nutzer, eigener Versuch
# (Alt-Encounter ohne Besitzer - die Warteschlange gehoert dem ganzen Haushalt.)
check("Gefangener Encounter: fuer Alice weg, fuer Bob da",
      e1 not in queue(alice) and e1 in queue(bob))
check("Encounter bleibt in der geteilten Warteschlange", enc(e1)["status"] == "waiting")

bob_view = battles_api.start_battle(e1, bob)
check("Bob startet beim Originalzustand (volle KP)",
      bob_view["wild"]["current_hp"] == bob_view["wild"]["max_hp"],
      f"{bob_view['wild']['current_hp']}/{bob_view['wild']['max_hp']}")
bob_view = act(bob_view["id"], bob, action="ball", ball="master_ball")
check("Bob faengt dasselbe Pokémon ebenfalls", bob_view["outcome"] == "caught")
check("Jetzt sind alle durch: Encounter verlaesst die Warteschlange",
      enc(e1)["status"] == "caught" and enc(e1)["resolved_at"] is not None
      and e1 not in queue(bob))
check("Beide haben ihr eigenes Pokémon", db.query_one(
    "SELECT COUNT(*) AS n FROM owned_pokemon WHERE caught_from = ?", (e1,))["n"] == 2)
check("Pokédex je Nutzer nachgezogen", all(
    db.query_one("SELECT caught_count FROM dex_entry WHERE species_id = ? AND user_id = ?",
                 (KARP["id"], uid))["caught_count"] >= 1 for uid in ("alice", "bob")))
expect_http("Abgeschlossener Encounter nimmt keinen Ball mehr", 409, encounters_api.throw, e1,
            encounters_api.BallThrow(ball="master_ball"), alice)


# ----------------------------------------------- Niederlage / Fliehen lassen
e3 = make_encounter(CHAR, 100)
db.execute("UPDATE owned_pokemon SET party_slot = NULL WHERE user_id = 'alice'")
weak = make_owned("alice", KARP, 3, 1)
view = battles_api.start_battle(e3, alice)
for _ in range(30):
    view = act(view["id"], alice, action="move", move=view["team"][0]["moves"][0]["key"])
    if view["over"]:
        break
check("Niederlage: Encounter verlaesst Alices Warteschlange (Wildes flieht)",
      view["outcome"] == "lost" and e3 not in queue(alice) and enc(e3)["status"] == "waiting",
      str(view["outcome"]))
check("Niederlage: eigener Ausgang ist 'fled' - nicht wiederholbar",
      encounter_state.progress(e3, "alice")["outcome"] == "fled")
hp_left = encounter_state.progress(e3, "alice")["current_hp"]
check("Niederlage: eigene KP des Wilden bleiben erhalten", hp_left is not None)
check("Fuer Bob ist dasselbe Pokémon unversehrt",
      encounters_api._decorate(enc(e3), "bob")["current_hp"]
      == encounters_api._decorate(enc(e3), "bob")["max_hp"])
check("Fuer Alice ist es geschwaecht",
      encounters_api._decorate(enc(e3), "alice")["current_hp"] == hp_left)

encounters_api.flee(e3, bob)
check("Nach dem letzten Nutzer ist der Encounter beendet",
      enc(e3)["status"] == "fled" and e3 not in queue(bob))


# --------------------------------------------- Autokampf und Anspruch
db.set_setting("auto_battle_enabled", True)
db.execute("UPDATE owned_pokemon SET party_slot = NULL WHERE user_id = 'alice'")
make_owned("alice", PIKA, 100)
e4 = make_encounter(KARP, 5)
auto = encounters_api.run_auto_battle(e4, alice)
check("Autokampf gewonnen", auto["outcome"] == "won", str(auto["outcome"]))
check("Autokampf: nur Alice ist fertig",
      encounter_state.progress(e4, "alice")["outcome"] == "defeated"
      and e4 not in queue(alice) and e4 in queue(bob))
expect_http("Alice kann den besiegten Encounter nicht erneut angehen", 409,
            encounters_api.run_auto_battle, e4, alice)

bob_view = battles_api.start_battle(e4, bob)
check("Kampfanspruch gilt weiterhin haushaltsweit",
      enc(e4)["status"] == "active" and enc(e4)["claimed_by"] == "bob"
      and e4 not in queue(alice))
expect_http("Waehrend Bobs Kampf wirft niemand sonst", 409, encounters_api.throw, e4,
            encounters_api.BallThrow(ball="master_ball"), db.get_user("tester"))
act(bob_view["id"], bob, action="run")
check("Nach Bobs Flucht ist der Anspruch wieder frei",
      enc(e4)["status"] == "waiting" and enc(e4)["claimed_by"] is None and e4 in queue(bob))


# ---------------------------------------- Nicht berechtigte Nutzer blockieren nicht
e5 = make_encounter(KARP, 5)
tester = db.get_user("tester")
encounters_api.flee(e5, tester)
check("Testkonto blockiert nicht, bleibt aber fuer sich erledigt",
      enc(e5)["status"] == "waiting" and e5 not in queue(tester) and e5 in queue(alice))
encounters_api.flee(e5, alice)
encounters_api.flee(e5, bob)
check("Sind alle Berechtigten durch, schliesst der Encounter",
      enc(e5)["status"] == "fled")


# ------------------------------------------ Eigene Warteschlange je Nutzer
own = make_encounter(KARP, 5, user_id="alice")
check("Eigener Encounter steht nur in Alices Warteschlange",
      own in queue(alice) and own not in queue(bob))
expect_http("Bob sieht einen fremden Encounter gar nicht", 404,
            encounters_api.get_encounter, own, bob)
expect_http("Bob wirft keinen Ball auf einen fremden Encounter", 404, encounters_api.throw,
            own, encounters_api.BallThrow(ball="master_ball"), bob)
expect_http("Bob kaempft nicht gegen einen fremden Encounter", 404,
            battles_api.start_battle, own, bob)
closed = encounters_api.flee(own, alice)["closed"]
check("Mit dem Besitzer ist der eigene Encounter sofort beendet",
      closed is True and enc(own)["status"] == "fled"
      and enc(own)["resolved_at"] is not None and own not in queue(alice))


# ------------------------------------------------------------- Retention
e6 = make_encounter(KARP, 5)
encounters_api.flee(e6, alice)
db.execute("UPDATE encounter SET expires_at = ? WHERE id = ?",
           ((NOW - timedelta(days=1)).isoformat(), e6))
spawn.expire_stale_encounters()
check("Retention beendet den Encounter auch mit offenen Nutzern",
      enc(e6)["status"] == "fled" and e6 not in queue(bob))


# ------------------------------------------------------------- Migration
e7 = make_encounter(KARP, 5)
db.execute("UPDATE encounter SET status = 'caught', claimed_by = 'alice', resolved_at = ? "
           "WHERE id = ?", (NOW.isoformat(), e7))
db.execute("DELETE FROM encounter_user WHERE encounter_id = ?", (e7,))
db.set_setting("require_battle_for_new_species", True)
db.init_db()
db.init_db()
check("Migration traegt alte Ergebnisse als Nutzer-Zeile nach",
      encounter_state.progress(e7, "alice")["outcome"] == "caught")
check("Migration ist idempotent", db.query_one(
    "SELECT COUNT(*) AS n FROM encounter_user WHERE encounter_id = ?", (e7,))["n"] == 1)
check("Abgeloeste Einstellung wird aus bestehenden DBs entfernt",
      db.get_setting("require_battle_for_new_species") is None)

# Alte DB ohne Besitzer-Spalte: die Migration traegt sie nach, bestehende
# Encounter bleiben bewusst geteilt (user_id NULL).
db.execute("DROP INDEX IF EXISTS idx_encounter_owner")
db.execute("ALTER TABLE encounter DROP COLUMN user_id")
db.init_db()
db.init_db()
check("Migration legt encounter.user_id an", "user_id" in {
    row["name"] for row in db.query("PRAGMA table_info(encounter)")})
check("Bestehende Encounter bleiben geteilt (user_id NULL)", db.query_one(
    "SELECT COUNT(*) AS n FROM encounter WHERE user_id IS NOT NULL")["n"] == 0)
new_shared = make_encounter(KARP, 5)
check("Ein Encounter ohne Besitzer steht danach wieder bei allen",
      new_shared in queue(alice) and new_shared in queue(bob))


# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
