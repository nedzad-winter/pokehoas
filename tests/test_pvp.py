"""Tests fuer Spieler-gegen-Spieler: Team-Codes (Round-Trip + Validierung),
Schnappschuss-Isolation (fremde Pokémon bleiben unangetastet), Belohnungen,
Bilanz und Historie.

Aufruf:  python3 tests/test_pvp.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-pvp-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import (  # noqa: E402
    abilities, battle, battle_history, db, economy, profiles, pvp, seed, spawn, team_code,
)

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


def expect_error(label: str, fn, *args) -> None:
    """Erwartet einen abgelehnten Aufruf (HTTPException oder CodeError)."""
    try:
        fn(*args)
    except (HTTPException, team_code.CodeError) as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        check(label, True, detail)
    else:
        check(label, False, "kein Fehler")


# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()
abilities.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")
now = datetime.now().astimezone()

pika = db.query_one("SELECT * FROM species WHERE name = 'pikachu'")
karp = db.query_one("SELECT * FROM species WHERE name = 'magikarp'")
check("Fallback-Pokédex enthaelt Pikachu und Karpador", pika is not None and karp is not None)


def give(user_id: str, species: dict, level: int, slot: int | None = 1, **extra) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "ability, held_item, current_hp, caught_at, party_slot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, species["id"], level, battle.exp_for_level(level), 0,
         json.dumps({key: 31 for key in spawn.STAT_KEYS}), "hardy",
         extra.get("ability"), extra.get("held_item"),
         battle.stat_hp(species["base_hp"], 31, level), now.isoformat(), slot),
    )


alice_pika = give("alice", pika, 30)
bob_karp = give("bob", karp, 4, ability="swift_swim", held_item="leftovers")


# --------------------------------------------------------- Schnappschuss
snapshot = pvp.snapshot_for_user("bob")
check("Schnappschuss enthaelt genau das Team des Gegners", len(snapshot) == 1, snapshot)
mon = snapshot[0]
check("Schnappschuss uebernimmt Art, Level, Faehigkeit und Item",
      mon["species_id"] == karp["id"] and mon["level"] == 4
      and mon["ability"] == "swift_swim" and mon["held_item"] == "leftovers", mon)
check("Schnappschuss uebernimmt die DVs", set(mon["ivs"]) == set(team_code.IV_KEYS)
      and all(value == 31 for value in mon["ivs"].values()), mon["ivs"])
check("Schnappschuss uebernimmt die Attacken", bool(mon["moves"]), mon["moves"])
# EVs (app/evs.py) gehoeren zum Abbild, wandern aber bewusst NICHT in den
# Team-Code - ein importiertes Team kaempft deshalb ohne Fleisspunkte.
check("Schnappschuss uebernimmt die EVs", set(mon["evs"]) == set(team_code.IV_KEYS), mon["evs"])


# ------------------------------------------------------------- Team-Code
export = pvp.team_code_for_user("bob")
code = export["code"]
check("Team-Code ist kompakt und kopierbar (base64url ohne Polster)",
      len(code) < 400 and "=" not in code and "/" not in code and "+" not in code, len(code))
decoded, owner = team_code.decode(code)
check("Round-Trip: Team-Code liefert denselben Schnappschuss (ohne EVs)",
      decoded == [{k: v for k, v in mon.items() if k != "evs"} for mon in snapshot], decoded)
check("Team-Code traegt keine EVs", all("evs" not in mon for mon in decoded), decoded)
check("Round-Trip: Besitzername steckt im Code", owner == "Bob", owner)
check("Code-Vorschau nennt Art und Level",
      export["team"][0]["species_id"] == karp["id"] and export["team"][0]["level"] == 4,
      export["team"])

full_team = [
    {"species_id": pika["id"], "level": 100, "ivs": {key: 31 for key in team_code.IV_KEYS},
     "ability": "static", "held_item": None, "moves": ["thunderbolt"]},
] * 6
check("Round-Trip mit vollem Team (6 Pokémon)",
      team_code.decode(team_code.encode(full_team))[0] == full_team)


def tamper(source: str, payload_change) -> str:
    raw = base64.urlsafe_b64decode(source + "=" * (-len(source) % 4))
    data = json.loads(raw)
    payload_change(data)
    return base64.urlsafe_b64encode(
        json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")


def bad(entry_change=None, payload_change=None) -> str:
    """Code mit genau einer manipulierten Stelle - Pruefsumme bleibt gueltig."""
    team = [[pika["id"], 30, [31] * 6, "static", None, ["thunderbolt"]]]
    if entry_change:
        entry_change(team[0])
    payload = {"v": team_code.CODE_VERSION, "c": team_code._checksum(team), "t": team}
    if payload_change:
        payload_change(payload)
        payload["c"] = team_code._checksum(payload["t"])
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


check("Der Referenz-Code der Validierungstests ist fuer sich gueltig",
      len(team_code.decode(bad())[0]) == 1)

expect_error("Leerer Code wird abgelehnt", team_code.decode, "   ")
expect_error("Kein base64 -> abgelehnt", team_code.decode, "nicht-ein-code!!!")
expect_error("Base64, aber kein JSON -> abgelehnt", team_code.decode,
             base64.urlsafe_b64encode(b"hallo welt").decode().rstrip("="))
expect_error("Ueberlanger Code wird abgelehnt", team_code.decode,
             "A" * (team_code.MAX_CODE_LENGTH + 1))
expect_error("Falsche Formatversion wird abgelehnt", team_code.decode,
             tamper(code, lambda d: d.__setitem__("v", team_code.CODE_VERSION + 1)))
expect_error("Veraenderte Nutzlast faellt bei der Pruefsumme auf", team_code.decode,
             tamper(code, lambda d: d["t"][0].__setitem__(1, 100)))
expect_error("Leeres Team wird abgelehnt", team_code.decode,
             bad(payload_change=lambda p: p.__setitem__("t", [])))
expect_error("Mehr als 6 Pokémon werden abgelehnt", team_code.decode,
             bad(payload_change=lambda p: p.__setitem__("t", p["t"] * 7)))
expect_error("Unbekannte Art wird abgelehnt", team_code.decode,
             bad(lambda m: m.__setitem__(0, 99999)))
expect_error("Level 0 wird abgelehnt", team_code.decode, bad(lambda m: m.__setitem__(1, 0)))
expect_error("Level 101 wird abgelehnt", team_code.decode, bad(lambda m: m.__setitem__(1, 101)))
expect_error("DV 32 wird abgelehnt", team_code.decode,
             bad(lambda m: m.__setitem__(2, [32] + [31] * 5)))
expect_error("Negativer DV wird abgelehnt", team_code.decode,
             bad(lambda m: m.__setitem__(2, [-1] + [31] * 5)))
expect_error("Zu wenige DV-Werte werden abgelehnt", team_code.decode,
             bad(lambda m: m.__setitem__(2, [31, 31])))
expect_error("Faehigkeit, die die Art nicht haben kann, wird abgelehnt", team_code.decode,
             bad(lambda m: m.__setitem__(3, "swift_swim")))
expect_error("Unbekanntes getragenes Item wird abgelehnt", team_code.decode,
             bad(lambda m: m.__setitem__(4, "master_item")))
expect_error("Unbekannte Attacke wird abgelehnt", team_code.decode,
             bad(lambda m: m.__setitem__(5, ["hyper_laser"])))
# Typfremde Attacken sind erlaubt: genau die bringt eine TM bei (app/tms.py),
# ein Team MIT TM-Attacken muss also als Code austauschbar bleiben.
check("Typfremde Attacke auf passendem Level wird akzeptiert (TM-Attacke)",
      team_code.decode(bad(lambda m: m.__setitem__(5, ["surf"])))[0][0]["moves"] == ["surf"])
expect_error("Attacke ueber dem Level des Pokémon wird abgelehnt", team_code.decode,
             bad(lambda m: m.__setitem__(5, ["blizzard"])))
expect_error("Mehr als vier Attacken werden abgelehnt", team_code.decode,
             bad(lambda m: m.__setitem__(5, ["thunderbolt", "thunder_shock", "quick_attack",
                                             "tackle", "growl"])))
expect_error("Doppelte Attacken werden abgelehnt", team_code.decode,
             bad(lambda m: m.__setitem__(5, ["thunderbolt", "thunderbolt"])))
expect_error("Falsch geformter Eintrag wird abgelehnt", team_code.decode,
             bad(payload_change=lambda p: p.__setitem__("t", [[1, 2, 3]])))
try:
    team_code.decode("###", "de")
    german = ""
except team_code.CodeError as exc:
    german = str(exc)
check("Fehlermeldungen kommen in der Sprache des Nutzers zurueck",
      german == "Das ist kein gueltiger Team-Code.", german)


# ------------------------------------------------- Kampf gegen ein Konto
def before_of(owned_id: int) -> dict:
    row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))
    return {key: row[key] for key in ("level", "exp", "current_hp", "held_item", "ivs", "ability")}


def fight(view: dict, user_id: str, limit: int = 40) -> dict:
    for _ in range(limit):
        if view["over"]:
            break
        member = next(m for m in view["team"] if m["active"])
        view = pvp.act(view["id"], user_id, {"action": "move", "move": member["moves"][0]["key"]})
    return view


bob_before = before_of(bob_karp)
bob_money_before = economy.balance("bob")
alice_money_before = economy.balance("alice")

view = pvp.start_vs_user("alice", "bob")
check("Kampf gegen ein Haushaltsmitglied startet", view["kind"] == "pvp" and not view["over"])
check("Gegner ist das Konto von Bob",
      view["opponent"]["name"] == "Bob" and view["opponent"]["user_id"] == "bob"
      and not view["opponent"]["from_code"], view["opponent"])
check("Gegnerisches Pokémon stammt aus dem Schnappschuss",
      view["wild"]["species_id"] == karp["id"] and view["wild"]["level"] == 4, view["wild"])
check("Gegen ein fremdes Pokémon kann kein Ball geworfen werden", view["balls"] == [])

# Mitten im Kampf das Team des Gegners aendern - der Kampf laeuft auf dem
# Schnappschuss weiter.
db.execute("UPDATE owned_pokemon SET level = 60, current_hp = 1 WHERE id = ?", (bob_karp,))
view = pvp.act(view["id"], "alice",
               {"action": "move",
                "move": next(m for m in view["team"] if m["active"])["moves"][0]["key"]})
check("Team-Aenderungen des Gegners waehrend des Kampfes wirken nicht",
      view["wild"]["level"] == 4, view["wild"]["level"])
db.execute("UPDATE owned_pokemon SET level = ?, current_hp = ? WHERE id = ?",
           (bob_before["level"], bob_before["current_hp"], bob_karp))

view = fight(view, "alice")
check("Kampf endet und wird gewonnen (Lv. 30 gegen Lv. 4)",
      view["over"] and view["outcome"] == "won", view.get("outcome"))
check("Sitzung steht danach auf 'won'",
      db.query_one("SELECT status FROM pvp_battle_session WHERE id = ?",
                   (view["id"],))["status"] == "won")

# ---- Isolation: am Gegner darf sich NICHTS geaendert haben
check("Gegnerisches Pokémon ist voellig unveraendert", before_of(bob_karp) == bob_before,
      before_of(bob_karp))
check("Guthaben des Gegners unveraendert", economy.balance("bob") == bob_money_before)
check("Der Gegner bekommt keinen Historien-Eintrag", battle_history.recent("bob") == [])
check("Der Gegner bekommt keine Kampf-Sitzung",
      db.query_one("SELECT COUNT(*) AS n FROM pvp_battle_session WHERE user_id = 'bob'")["n"] == 0)

# ---- Belohnungen fuer den Herausforderer
check("EXP wird gutgeschrieben", bool(view["result"]["exp"]), view["result"]["exp"])
earned = view["result"]["reward"]["money_earned"]
check("Preisgeld entspricht der Einstellung",
      earned == pvp._prize_money(snapshot) and earned > 0, earned)
check("Preisgeld ist niedriger als ein Arenakampf gleicher Staerke",
      float(db.get_setting("pvp_money_per_level"))
      < float(db.get_setting("trainer_money_per_level")))
check("Guthaben des Herausforderers steigt um das Preisgeld",
      economy.balance("alice") == alice_money_before + earned)
own = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (alice_pika,))
check("Das eigene Pokémon hat EXP bekommen", own["exp"] > battle.exp_for_level(30), own["exp"])
check("Das eigene Team ist nach dem gewonnenen Spielerkampf voll geheilt",
      own["current_hp"] == battle.stat_hp(pika["base_hp"], 31, own["level"]), own["current_hp"])

entry = battle_history.recent("alice")[0]
check("Der Kampf steht in der eigenen Historie",
      entry["kind"] == battle_history.KIND_PVP and entry["opponent"] == "Bob"
      and entry["outcome"] == "won", entry)

record = pvp.record_of("alice", "bob")
check("Bilanz zaehlt den Sieg", record["wins"] == 1 and record["losses"] == 0, record)
check("Die Bilanz des Gegners bleibt leer (er hat nie gekaempft)",
      pvp.record_of("bob", "alice") == {"wins": 0, "losses": 0, "last_at": None})

bob_profile = profiles.build(db.get_user("bob"), "en", viewer_id="alice")
check("Fremdes Profil zeigt die Bilanz des Betrachters",
      bob_profile["pvp"]["can_challenge"] and bob_profile["pvp"]["record"]["wins"] == 1,
      bob_profile["pvp"])
check("Das eigene Profil bietet keine Herausforderung an",
      not profiles.build(db.get_user("alice"), "en", viewer_id="alice")["pvp"]["can_challenge"])

overview = pvp.opponents_overview("alice")
check("Gegneruebersicht listet Bob (und nicht Alice selbst)",
      [o["user_id"] for o in overview["opponents"]] == ["bob"], overview["opponents"])
check("Gegneruebersicht nennt Teamgroesse und Bilanz",
      overview["opponents"][0]["team_size"] == 1
      and overview["opponents"][0]["record"]["wins"] == 1, overview["opponents"][0])


# ----------------------------------------------------- Kampf gegen Code
db.execute("UPDATE owned_pokemon SET current_hp = ? WHERE id = ?",
           (battle.stat_hp(pika["base_hp"], 31, 30), alice_pika))

expect_error("Gegen das eigene Team kann man nicht kaempfen", pvp.start_vs_user, "alice", "alice")
expect_error("Unbekannter Gegner wird abgelehnt", pvp.start_vs_user, "alice", "carol")
expect_error("Ein ungueltiger Code startet keinen Kampf", pvp.start_vs_code, "alice", "kaputt!")

view = pvp.start_vs_code("alice", code)
check("Kampf gegen einen Team-Code startet",
      view["kind"] == "pvp" and view["opponent"]["from_code"]
      and view["opponent"]["name"] == "Bob", view["opponent"])
expect_error("Waehrend eines laufenden Kampfes keine neue Herausforderung",
             pvp.start_vs_user, "alice", "bob")
check("Derselbe laufende Kampf wird fortgesetzt statt neu gestartet",
      pvp.start_vs_code("alice", code)["id"] == view["id"])

view = pvp.act(view["id"], "alice", {"action": "run"})
check("Aufgeben beendet den Kampf als Niederlage",
      view["over"] and view["outcome"] == "lost", view.get("outcome"))
check("Code-Kaempfe zaehlen nicht in die Bilanz gegen ein Konto",
      pvp.record_of("alice", "bob") == record | {"last_at": record["last_at"]},
      pvp.record_of("alice", "bob"))
check("Auch der Code-Kampf laesst das Original unveraendert",
      before_of(bob_karp) == bob_before)
check("Code-Kampf steht als Spielerkampf in der Historie",
      battle_history.recent("alice")[0]["kind"] == battle_history.KIND_PVP
      and battle_history.recent("alice")[0]["outcome"] == "lost")


# --------------------------------------------- Niederlage: auch dann voll geheilt
carol = db.upsert_user("carol", "Carol")
carol_karp = give("carol", karp, 5)

view = pvp.start_vs_user("carol", "alice")
view = fight(view, "carol")
check("Deutlich schwaecheres Team verliert den Spielerkampf",
      view["over"] and view["outcome"] == "lost", view.get("outcome"))
carol_row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (carol_karp,))
check("Auch nach einer Niederlage im Spielerkampf ist das eigene Team voll geheilt",
      carol_row["current_hp"] == battle.stat_hp(karp["base_hp"], 31, carol_row["level"])
      and carol_row["current_hp"] > 0, carol_row["current_hp"])


# --------------------------------------------------------- Ohne Gegner-Team
db.execute("UPDATE owned_pokemon SET party_slot = NULL WHERE id = ?", (bob_karp,))
check("Ein Konto ohne Team hat einen leeren Schnappschuss", pvp.snapshot_for_user("bob") == [])
expect_error("Gegen ein Konto ohne Team kann man nicht kaempfen",
             pvp.start_vs_user, "alice", "bob")
expect_error("Ohne eigenes Team gibt es keinen Team-Code", pvp.team_code_for_user, "bob")


# ------------------------------------------------------------- Endpunkte
from app import main  # noqa: E402  (importiert die komplette App)

paths = {route.path for route in main.app.routes}
for path in ("/api/pvp/opponents", "/api/pvp/challenge/{user_id}", "/api/pvp/team-code",
             "/api/pvp/preview", "/api/pvp/code-battle", "/api/pvp-battles/{battle_id}",
             "/api/pvp-battles/{battle_id}/action"):
    check(f"Endpunkt {path} ist registriert", path in paths)


print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("Alle Tests bestanden.")
