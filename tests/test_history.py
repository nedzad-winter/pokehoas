"""Tests fuer Kampf-Historie (battle_history.py) und Nutzerprofil (profiles.py).

Aufruf:  python3 tests/test_history.py
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

TMP = tempfile.mkdtemp(prefix="pokemon-history-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import (achievements, auto_rules, battle, battle_history, db, seed,  # noqa: E402
                 spawn, trainer_battle, trainers)
from app.api import battles as battles_api  # noqa: E402
from app.api import encounters as encounters_api  # noqa: E402
from app.api import shop as shop_api  # noqa: E402
from app.api import users as users_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


random.seed(int(os.environ.get("TEST_SEED", "4321")))
db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")
IVS = {k: 15 for k in spawn.STAT_KEYS}
NOW = datetime.now().astimezone()


def species(name: str) -> dict:
    return db.query_one("SELECT * FROM species WHERE name = ?", (name,))


PIKA, KARP = species("pikachu"), species("magikarp")


def make_owned(user_id: str, sp: dict, level: int, slot: int | None) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "current_hp, party_slot, caught_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, sp["id"], level, battle.exp_for_level(level, sp["growth_rate"]), 0,
         json.dumps(IVS), "hardy", None, slot, NOW.isoformat()),
    )


def make_encounter(sp: dict, level: int) -> int:
    encounter_id = spawn.create_encounter(sp, source_entity="test.trigger")
    db.execute("UPDATE encounter SET level = ?, ivs = ? WHERE id = ?",
               (level, json.dumps(IVS), encounter_id))
    return encounter_id


# --------------------------------------------------------------- Migration
cols = {row["name"] for row in db.query("PRAGMA table_info(battle_log)")}
check("Migration legt die Historie-Spalten an",
      {"kind", "opponent", "money_earned", "caught", "exp_breakdown"} <= cols, sorted(cols))
db.init_db()  # idempotent: zweiter Lauf darf nicht scheitern
check("Migration ist idempotent",
      {"kind", "opponent", "money_earned", "caught", "exp_breakdown"}
      <= {row["name"] for row in db.query("PRAGMA table_info(battle_log)")})


# --------------------------------------------------------- record / recent
battle_history.record(
    "alice", battle_history.KIND_WILD, "caught", opponent="Pikachu",
    money_earned=120, caught=True,
    exp_details=[{"owned_id": 1, "name_en": "Squirtle", "name_de": "Schiggy",
                  "exp": 40, "level_from": 5, "level_to": 6, "level_ups": 1}],
    log=["a", "b"],
)
entries = battle_history.recent("alice")
check("Eintrag wird gelesen", len(entries) == 1, entries)
first = entries[0]
check("Gegner, Geld und Fang-Flag stimmen",
      first["opponent"] == "Pikachu" and first["money_earned"] == 120 and first["caught"] is True,
      first)
check("EXP-Aufschluesselung kommt strukturiert zurueck",
      first["exp"][0]["exp"] == 40 and first["exp"][0]["level_ups"] == 1, first["exp"])
check("Neue Eintraege sind nicht 'legacy'", first["legacy"] is False)
check("Historie ist pro Nutzer", battle_history.recent("bob") == [])

# Alt-Zeile ohne die neuen Spalten (wie vor der Migration geschrieben).
db.execute(
    "INSERT INTO battle_log (encounter_id, user_id, created_at, auto, outcome, detail) "
    "VALUES (NULL, 'alice', ?, 1, 'won', ?)",
    (NOW.isoformat(), json.dumps(["alt"])),
)
old = battle_history.recent("alice")[0]
check("Alte Eintraege bleiben sichtbar", old["outcome"] == "won" and old["legacy"] is True, old)
check("Alte Eintraege leiten die Kampfart aus dem auto-Flag ab",
      old["kind"] == battle_history.KIND_AUTO, old["kind"])
check("Alte Eintraege haben keine EXP-Aufschluesselung", old["exp"] == [], old["exp"])
check("Neueste zuerst", [e["legacy"] for e in battle_history.recent("alice")] == [True, False])
check("Limit wird beachtet", len(battle_history.recent("alice", 1)) == 1)


# ------------------------------------------------------- Historie begrenzen
# Nur die letzten HISTORY_KEEP Kaempfe pro Nutzer werden aufbewahrt; aeltere
# Zeilen werden bei jedem record() geloescht (gleiches Muster wie beim
# auto_rule_log).
carol = db.upsert_user("carol", "Carol")
for i in range(battle_history.HISTORY_KEEP + 10):
    battle_history.record("carol", battle_history.KIND_WILD, "won", opponent=f"Rattata {i}")
check("Historie wird auf HISTORY_KEEP Eintraege pro Nutzer begrenzt",
      battle_history.count("carol") == battle_history.HISTORY_KEEP,
      battle_history.count("carol"))
carol_entries = battle_history.recent("carol", battle_history.HISTORY_KEEP)
check("Nach dem Kuerzen bleiben die neuesten Eintraege erhalten",
      carol_entries[0]["opponent"] == f"Rattata {battle_history.HISTORY_KEEP + 9}"
      and carol_entries[-1]["opponent"] == "Rattata 10",
      (carol_entries[0]["opponent"], carol_entries[-1]["opponent"]))
check("Andere Nutzer bleiben von der Kuerzung unberuehrt",
      battle_history.count("alice") == 2, battle_history.count("alice"))


# ------------------------------------------------- EXP-Aufschluesselung live
squirt_id = make_owned("alice", species("squirtle"), 30, 1)
db.set_setting("auto_battle_enabled", True)
e1 = make_encounter(KARP, 3)
result = encounters_api.run_auto_battle(e1, alice)
check("Autokampf gewonnen", result["outcome"] == "won", result["outcome"])
check("Autokampf liefert Gegner und EXP-Aufschluesselung",
      result["opponent"] and result["exp_details"], result.get("opponent"))

auto_entry = battle_history.recent("alice")[0]
check("Autokampf landet als kind='auto' in der Historie",
      auto_entry["kind"] == battle_history.KIND_AUTO and auto_entry["outcome"] == "won",
      auto_entry["kind"])
check("Autokampf-Eintrag nennt den Gegner", auto_entry["opponent"] == "Magikarp",
      auto_entry["opponent"])
check("Autokampf-Eintrag hat Geld und EXP je Teammitglied",
      auto_entry["money_earned"] and auto_entry["exp"]
      and auto_entry["exp"][0]["owned_id"] == squirt_id, auto_entry)
check("EXP-Eintrag traegt beide Namensfelder (fuer die Anzeigesprache)",
      auto_entry["exp"][0]["name_en"] and "name_de" in auto_entry["exp"][0],
      auto_entry["exp"][0])
check("Autokampf ohne Fang: caught = False", auto_entry["caught"] is False)

# Gefangen per Auto-Regel: das Fang-Flag muss NACH dem Wurf gesetzt werden.
db.execute("INSERT OR REPLACE INTO inventory (item_key, user_id, quantity) VALUES "
           "('master_ball', 'alice', 5)")
catch_rule = auto_rules.create_rule("alice", auto_rules.RuleIn(name="Fang", actions={
    "mode": "catch", "ball": "specific", "ball_key": "master_ball", "allow_master_ball": True,
    "chance_min": 1}))
e2 = make_encounter(KARP, 2)
auto_rules.apply_rules("alice", db.query_one("SELECT * FROM encounter WHERE id = ?", (e2,)))
catch_entry = battle_history.recent("alice")[0]
check("Autokampf mit Fang wird als gefangen protokolliert", catch_entry["caught"] is True,
      catch_entry)
check("Fang-Eintrag nennt die Regel", catch_entry["rule_name"] == "Fang", catch_entry)
auto_rules.delete_rule("alice", catch_rule["id"])


# ------------------------------------------- EXP-Aufschluesselung mit Level-Ups
low = battle.Combatant(species=PIKA, level=5, ivs=dict(IVS),
                       owned_id=make_owned("alice", PIKA, 5, None))
low.max_hp = low.current_hp = battle.stat_hp(PIKA["base_hp"], 15, 5)
details: list[dict] = []
battle.grant_exp([], [low], {0: 20000}, "en", details=details)
check("grant_exp protokolliert Level-Aufstiege je Teammitglied",
      len(details) == 1 and details[0]["level_ups"] > 0
      and details[0]["level_to"] > details[0]["level_from"], details)
check("grant_exp bleibt ohne `details` unveraendert",
      battle.grant_exp([], [low], {0: 10}, "en") == {low.owned_id: 10})


# --------------------------------------------------------- Manueller Kampf
e3 = make_encounter(KARP, 12)
view = battles_api.start_battle(e3, alice)
for _ in range(20):
    view = battles_api.battle_action(
        view["id"],
        battles_api.BattleAction(action="move", move=view["team"][0]["moves"][0]["key"]),
        alice,
    )
    if view["over"] or view.get("needs_switch"):
        break
if not view["over"]:
    view = battles_api.battle_action(view["id"], battles_api.BattleAction(action="run"), alice)
manual = battle_history.recent("alice")[0]
check("Manueller Kampf landet als kind='wild' in der Historie",
      manual["kind"] == battle_history.KIND_WILD, manual["kind"])
check("Manueller Kampf nennt Ausgang und Gegner",
      manual["outcome"] == view["outcome"] and manual["opponent"] == "Magikarp", manual)
if view["outcome"] == "won":
    check("Sieg: Aufschluesselung nennt Level davor/danach",
          manual["exp"] and manual["exp"][0]["level_to"] >= manual["exp"][0]["level_from"],
          manual["exp"])


# --------------------------------------------------------- Arena-/Liga-Kampf
# Kein echter Trainer ist mit dem Fallback-Pokédex vollstaendig aufloesbar -
# daher wie in test_league.py kurz einen synthetischen in den Katalog
# spiegeln und danach wieder verwerfen.
_saved_raw, _saved_trainers = trainers._raw, trainers._trainers
trainers._raw = {
    "regions": [{"key": "kanto", "label": "Kanto"}],
    "trainers": [{"id": "test_weak", "kind": "gym_leader", "region": "kanto",
                  "name": "Testo", "team": [["magikarp", 3]]}],
}
trainers._trainers = None

# Volle KP vor dem Arenakampf - Alice' Team kann durch die vorherigen
# Kaempfe in diesem Testlauf angeschlagen sein, und dieser Abschnitt geht
# von einem klaren Sieg (Lv. 30 gegen Lv. 3) aus, nicht von einem knappen.
db.execute("UPDATE owned_pokemon SET current_hp = NULL WHERE user_id = 'alice'")

tview = trainer_battle.start("test_weak", "alice")
for _ in range(30):
    if tview["over"]:
        break
    if tview.get("needs_switch"):
        target = next(m["owned_id"] for m in tview["team"] if not m["fainted"] and not m["active"])
        tview = trainer_battle.act(tview["id"], "alice", {"action": "switch", "target": target})
        continue
    active = next(m for m in tview["team"] if m["active"])
    tview = trainer_battle.act(tview["id"], "alice",
                               {"action": "move", "move": active["moves"][0]["key"]})
trainer_entry = battle_history.recent("alice")[0]
check("Arena-/Liga-Kampf landet als kind='trainer' in der Historie",
      trainer_entry["kind"] == battle_history.KIND_TRAINER, trainer_entry["kind"])
check("Trainer-Eintrag nennt Titel und Namen des Gegners",
      "Testo" in (trainer_entry["opponent"] or ""), trainer_entry["opponent"])
check("Trainer-Eintrag haelt Ausgang, Preisgeld und EXP fest",
      trainer_entry["outcome"] == tview["outcome"] and trainer_entry["money_earned"]
      and trainer_entry["exp"], trainer_entry)
check("Trainerkampf hat kein Fang-Flag", trainer_entry["caught"] is False)

trainers._raw, trainers._trainers = _saved_raw, _saved_trainers


# -------------------------------------------------------------- Endpunkt
api_out = battles_api.battle_history_list(20, alice)
check("Endpunkt liefert hoechstens `limit` Eintraege",
      len(api_out["battles"]) <= 20 and api_out["limit"] == 20)
check("Endpunkt begrenzt unsinnige Limits", battles_api.battle_history_list(9999, alice)["limit"] == 100)
check("Endpunkt zeigt nur eigene Kaempfe",
      battles_api.battle_history_list(20, bob)["battles"] == [])
check("Endpunkt liefert die Gesamtzahl fuer die Seitennavigation",
      api_out["total"] == len(battle_history.recent("alice", 100)), api_out["total"])

# Pager: Seite 1 (limit=2, offset=0) und Seite 2 (limit=2, offset=2) duerfen
# sich nicht ueberschneiden und muessen zusammen den Anfang von `recent` ergeben.
page1 = battles_api.battle_history_list(2, alice, 0)
page2 = battles_api.battle_history_list(2, alice, 2)
check("Seite 1 und 2 ueberschneiden sich nicht",
      {b["id"] for b in page1["battles"]}.isdisjoint({b["id"] for b in page2["battles"]}))
check("Seiten aneinandergehaengt entsprechen den ersten 4 Eintraegen",
      page1["battles"] + page2["battles"] == battle_history.recent("alice", 4),
      (page1["battles"], page2["battles"]))


# --------------------------------------------------------------- Profil
prof = users_api.profile("me", alice)
check("Profil nennt den Nutzer", prof["user"]["id"] == "alice", prof["user"])
check("Profil enthaelt das Team mit Level und Sprite",
      prof["team"] and prof["team"][0]["level"] and "sprite_url" in prof["team"][0],
      prof["team"])
check("Profil enthaelt Guthaben", isinstance(prof["money"], int), prof["money"])
check("Profil enthaelt Pokédex-Fortschritt",
      prof["pokedex"]["total"] > 0 and prof["pokedex"]["caught"] >= 0, prof["pokedex"])
check("Profil zaehlt gefangene Pokémon", prof["caught_count"] >= 0, prof["caught_count"])

# Generationen-Tabelle: eine Zeile je Generation, aufsteigend sortiert, mit
# Gesamtzahl der Art je Generation, gefangenen Arten (dex_entry) und aktuell
# besessenen Arten (Team oder Box - "lebendiger Dex").
gens = prof["generations"]
check("Generationen-Tabelle ist aufsteigend sortiert",
      [g["generation"] for g in gens] == sorted(g["generation"] for g in gens), gens)
check("Generationen-Tabelle summiert sich zur Dex-Gesamtzahl",
      sum(g["total"] for g in gens) == prof["pokedex"]["total"], gens)
gen1 = next(g for g in gens if g["generation"] == 1)
check("Gen 1: Schiggy, Pikachu und der gefangene Karpador zaehlen als besessen",
      gen1["living"] >= 3, gen1)
check("Gen 1: nur der tatsaechlich gefangene Karpador zaehlt als gefangen",
      gen1["caught"] >= 1, gen1)
check("Gen 1 kann nie mehr gefangene/besessene Arten haben als es Arten gibt",
      gen1["caught"] <= gen1["total"] and gen1["living"] <= gen1["total"], gen1)
check("Gen 1: gesehene Arten zaehlen mindestens die gefangenen",
      gen1["caught"] <= gen1["seen"] <= gen1["total"], gen1)
check("Generationen-Tabelle summiert gesehene Arten zur Dex-Gesamtzahl",
      sum(g["seen"] for g in gens) == prof["pokedex"]["seen"], gens)
check("Profil listet die Orden je Region",
      len(prof["regions"]) == len(trainers.regions())
      and all(r["total"] >= 0 for r in prof["regions"]), len(prof["regions"]))
check("Ohne Siege gibt es keine Orden", all(r["earned"] == 0 for r in prof["regions"]))
check("Ohne Liga-Sieg keine Champion-Titel", prof["champion_titles"] == [])
check("Profil nennt Erfolge",
      prof["achievements"]["total"] > 0
      and prof["achievements"]["unlocked"] == len(prof["achievements"]["items"]),
      prof["achievements"])

for t in trainers.all_trainers():
    if t["region"] == "kanto":
        achievements.unlock("alice", achievements.trainer_key(t["id"]))
prof = users_api.profile("alice", alice)
kanto = next(r for r in prof["regions"] if r["key"] == "kanto")
check("Besiegte Arenaleiter zaehlen als Orden", kanto["earned"] == kanto["total"] > 0, kanto["earned"])
check("Komplette Liga ergibt einen Champion-Titel",
      [t["region"] for t in prof["champion_titles"]] == ["kanto"], prof["champion_titles"])
check("Freigeschaltete Erfolge werden gelistet",
      prof["achievements"]["unlocked"] == len(prof["achievements"]["items"]) > 0,
      prof["achievements"]["unlocked"])

# Fremdes Profil: lesbar, aber eben das des anderen Nutzers.
other = users_api.profile("bob", alice)
check("Fremdes Profil ist lesbar", other["user"]["id"] == "bob", other["user"])
check("Fremdes Profil zeigt fremde Daten", other["achievements"]["unlocked"] == 0)
try:
    users_api.profile("nobody", alice)
    check("Unbekannter Nutzer -> 404", False, "keine Exception")
except HTTPException as exc:
    check("Unbekannter Nutzer -> 404", exc.status_code == 404, exc.status_code)

household = users_api.list_household(alice)
check("Haushaltsliste enthaelt den eigenen Nutzer",
      any(u["id"] == "alice" for u in household["users"]) and household["me"] == "alice",
      household["users"])


# ----------------------------------------------------------- Beutel-Preise
bag = shop_api.bag(alice)
check("Beutel liefert Preise zum Sortieren",
      all("price" in it for it in bag["items"]), bag["items"][:1])
check("Beutel liefert Kategorien fuer die Bereichs-Gruppierung",
      all(it["category"] for it in bag["items"]), bag["items"][:1])
shop = shop_api.shop(alice)
check("Shop kennt genau die erwarteten Bereiche",
      {it["category"] for it in shop["items"]}
      <= {"ball", "medicine", "evolution", "held", "key", "breeding", "vitamin", "tm"},
      sorted({it["category"] for it in shop["items"]}))


# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
