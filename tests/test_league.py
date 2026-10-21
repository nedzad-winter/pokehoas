"""Tests fuer Arena/Liga-Kaempfe, Erfolge und die generationsabhaengige
Freischaltung des Spawn-Pools.

Aufruf:  python3 tests/test_league.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-league-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import achievements, battle, db, rarity, rewards, seed, spawn, trainer_battle, trainers  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")
now = datetime.now().astimezone()

pika = db.query_one("SELECT * FROM species WHERE name = 'pikachu'")
karpador = db.query_one("SELECT * FROM species WHERE name = 'magikarp'")
check("Fallback-Pokédex enthaelt Pikachu und Karpador", pika is not None and karpador is not None)


# ---------------------------------------------------- Katalog (echte Daten)
check("Alle 9 Regionen vorhanden", len(trainers.regions()) == 9,
      [r["key"] for r in trainers.regions()])
check("Regionen entsprechen den Generationsgrenzen der Reihe nach",
      [r["key"] for r in trainers.regions()]
      == ["kanto", "johto", "hoenn", "sinnoh", "unova", "kalos", "alola", "galar", "paldea"])

all_t = trainers.all_trainers()
check("Trainer-Katalog nicht leer", len(all_t) > 20, len(all_t))
kanto_ids = [t["id"] for t in all_t if t["region"] == "kanto"]
check("Kanto hat Arenaleiter, Top Vier und Champ", len(kanto_ids) >= 12, kanto_ids)
check("Jeder Kanto-Trainer hat einen Titel",
      all(trainers.title_for(t) for t in all_t if t["region"] == "kanto"))

# Galar: the Champion Cup (semifinals + finals, as in Pokémon Sword) replaces the Elite Four.
galar = [t for t in all_t if t["region"] == "galar"]
galar_ids = [t["id"] for t in galar]
cup = [t for t in galar if t["kind"] == "elite_four"]
check("Galar: Champion Cup zwischen Arenen und Champ, in Kampfreihenfolge",
      [t["id"] for t in cup] == ["galar_marnie", "galar_hop", "galar_bede", "galar_nessa_finals",
                                 "galar_bea_finals", "galar_raihan_finals"]
      and galar_ids.index("galar_raihan") < galar_ids.index("galar_marnie")
      and galar_ids[-1] == "galar_leon", galar_ids)
check("Galar: Halbfinale (Mary, Hop) und Finale (Betys, Kate, Saida, Roy) per stage",
      [t["stage"] for t in cup] == ["semifinal"] * 2 + ["finals"] * 4, [t.get("stage") for t in cup])
check("Galar: Champion-Cup-Trainer haben Top-Vier-IVs und staerkere Teams als die Arenen",
      all(t["iv"] == trainers.KIND_IVS["elite_four"] for t in cup)
      and min(t["max_level"] for t in cup) > max(t["max_level"] for t in galar if t["kind"] == "gym_leader"),
      [(t["id"], t["iv"], t["max_level"]) for t in cup])
check("Galar: Titel Rivalin/Rivale bzw. Arenaleiterin statt Top Vier",
      [trainers.title_for(trainers.get(i), "de") for i in ("galar_marnie", "galar_hop", "galar_nessa_finals")]
      == ["Rivalin", "Rivale", "Arenaleiterin"]
      and trainers.title_for(trainers.get("galar_raihan_finals")) == "Gym Leader")
check("Galar: stage steht in der Trainer-Ansicht (Liga-Tab gruppiert danach)",
      trainer_battle.trainer_info(trainers.get("galar_hop"))["stage"] == "semifinal"
      and trainer_battle.trainer_info(trainers.get("galar_raihan"))["stage"] is None)


# ------------------------------------------- Reihenfolge (gym 1, 2, ... Liga)
# Trainers of a region unlock one after the other, independent of any setting.
def _prereq(tid: str) -> str | None:
    p = trainers.prerequisite(tid)
    return p["id"] if p else None


check("Reihenfolge: erster Arenaleiter hat keine Voraussetzung", _prereq("kanto_brock") is None)
check("Reihenfolge: Arena 2 braucht Arena 1, Arena 8 braucht Arena 7",
      _prereq("kanto_misty") == "kanto_brock" and _prereq("kanto_giovanni") == "kanto_blaine")
check("Reihenfolge: Top Vier folgt auf die letzte Arena, Champ auf den letzten der Top Vier",
      _prereq("kanto_lorelei") == "kanto_giovanni" and _prereq("kanto_blue") == "kanto_lance")
check("Reihenfolge: Galar Champion Cup in Kampfreihenfolge",
      _prereq("galar_marnie") == "galar_raihan" and _prereq("galar_bede") == "galar_hop"
      and _prereq("galar_leon") == "galar_raihan_finals")
check("Reihenfolge: Regionen sind unabhaengig (Johto-Arena 1 ohne Voraussetzung)",
      _prereq(next(t["id"] for t in trainers.all_trainers() if t["region"] == "johto")) is None)
check("Reihenfolge: Training ist nicht betroffen", trainers.prerequisite("training") is None)

try:
    trainer_battle.start("kanto_misty", "bob")
    locked_ok = False
except Exception as exc:  # fastapi.HTTPException
    locked_ok = getattr(exc, "status_code", None) == 409 and "Brock" in str(getattr(exc, "detail", ""))
check("Reihenfolge: Arena 2 laesst sich vor Arena 1 nicht starten (409)", locked_ok)

def _row(user: str, tid: str) -> dict:
    return next(t for r in trainer_battle.overview(user)["regions"] for t in r["trainers"] if t["id"] == tid)

check("Reihenfolge: Uebersicht meldet den Vorgaenger als Sperre",
      _row("bob", "kanto_misty")["locked_by"] is not None and "Brock" in _row("bob", "kanto_misty")["locked_by"]
      and _row("bob", "kanto_brock")["locked_by"] is None)
achievements.unlock("bob", achievements.trainer_key("kanto_brock"))
check("Reihenfolge: nach Arena 1 ist Arena 2 offen, Arena 3 noch gesperrt",
      _row("bob", "kanto_misty")["locked_by"] is None and _row("bob", "kanto_surge")["locked_by"] is not None)
achievements.unlock("bob", achievements.trainer_key("kanto_giovanni"))
check("Reihenfolge: bereits besiegte Trainer bleiben fuer Rematches offen",
      _row("bob", "kanto_giovanni")["locked_by"] is None)
db.execute("DELETE FROM achievement WHERE user_id = ?", ("bob",))


# ------------------------------------------------ Synthetischer Testkampf
# Kein echter Trainer ist mit dem kleinen Offline-Pokédex vollstaendig
# aufloesbar (trainers.resolve_team) - fuer einen echten Ende-zu-Ende-Test
# des Kampf-Flows wird deshalb kurzzeitig ein synthetischer Trainer aus
# Fallback-Arten in den Katalog gespiegelt und danach wieder verworfen.
_saved_raw, _saved_trainers = trainers._raw, trainers._trainers
trainers._raw = {
    "regions": [{"key": "kanto", "label": "Kanto"}],
    "trainers": [{
        "id": "test_weak", "kind": "gym_leader", "region": "kanto",
        "name": "Testo", "team": [["magikarp", 3]],
    }],
}
trainers._trainers = None

owned_id = db.execute(
    "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
    "caught_at, party_slot) VALUES (?,?,?,?,?,?,?,?,?)",
    ("alice", pika["id"], 25, battle.exp_for_level(25), 0,
     json.dumps({k: 31 for k in spawn.STAT_KEYS}), "hardy", now.isoformat(), 1),
)

view = trainer_battle.start("test_weak", "alice")
check("Kampf startet gegen den Testtrainer", view["trainer"]["id"] == "test_weak")
check("Preisgeld wird berechnet", view["trainer"]["prize"] > 0, view["trainer"]["prize"])

balance_before = None
for _ in range(30):
    if view["over"]:
        break
    active_member = next(m for m in view["team"] if m["active"])
    move_key = active_member["moves"][0]["key"]
    view = trainer_battle.act(view["id"], "alice", {"action": "move", "move": move_key})
check("Testkampf endet innerhalb weniger Runden", view["over"], view.get("outcome"))
check("Testkampf wird gewonnen (Lv. 25 vs. Lv. 3 Karpador)", view["outcome"] == "won", view.get("outcome"))
check("EXP wird im Ergebnis gemeldet", bool(view["result"]["exp"]), view["result"])
check("Preisgeld wird gutgeschrieben", view["result"]["reward"]["money_earned"] > 0, view["result"]["reward"])
check("Kontostand nach Sieg > 0", view["result"]["reward"]["money"] > 0)
check(
    "Trainer-Erfolg wird freigeschaltet",
    achievements.trainer_key("test_weak") in achievements.unlocked("alice"),
)
check(
    "Liga-Meister schaltet frei, sobald der (synthetische Ein-Trainer-)Katalog komplett ist",
    achievements.LEAGUE_MASTER in achievements.unlocked("alice"),
)
session_row = db.query_one("SELECT status FROM trainer_battle_session WHERE id = ?", (view["id"],))
check("Kampf-Session steht auf 'won'", session_row["status"] == "won")

# Refight: derselbe Trainer laesst sich nach dem Sieg sofort erneut herausfordern.
rematch = trainer_battle.start("test_weak", "alice")
check("Refight startet einen neuen Kampf gegen denselben Trainer",
      rematch["trainer"]["id"] == "test_weak" and not rematch["over"] and rematch["id"] != view["id"])
trainer_battle.act(rematch["id"], "alice", {"action": "run"})

# Katalog zuruecksetzen, bevor mit den ECHTEN Trainern weitergetestet wird. Der
# synthetische Liga-Meister-Erfolg von eben bleibt fuer Alice bestehen (ist ein
# Artefakt des Testkatalogs), stoert die folgenden Tests aber nicht - die
# pruefen region_cleared/unlocked_generations anhand der echten Trainer-IDs.
trainers._raw, trainers._trainers = _saved_raw, _saved_trainers
check("Echter Katalog wieder geladen", len(trainers.all_trainers()) == len(all_t))


# ---------------------------------------------------- Erfolge & Freischaltung
check("Kanto ist fuer Alice noch nicht komplett", not achievements.region_cleared("alice", "kanto"))
for tid in kanto_ids[:-1]:
    achievements.unlock("alice", achievements.trainer_key(tid))
check("Kanto ist fuer Alice noch nicht komplett (letzter Trainer fehlt)",
      not achievements.region_cleared("alice", "kanto"))
last_kanto_new = achievements.on_trainer_defeated("alice", kanto_ids[-1])
check("Kanto ist fuer Alice jetzt komplett", achievements.region_cleared("alice", "kanto"))
# Kanto HAT einen Regions-Erfolg (Meisterball wie jede Region), schaltet aber
# keine Generation frei - Generation 1 ist ohnehin von Anfang an offen.
kanto_region_def = next(
    (d for d in achievements.definitions() if d["key"] == achievements.region_key("kanto")), None)
check("Regions-Erfolg fuer Kanto existiert", kanto_region_def is not None)
check("Kantos Regions-Erfolg verspricht keine Generations-Freischaltung",
      kanto_region_def is not None and "spawn pool" not in kanto_region_def["description"],
      kanto_region_def)
check("Kantos Regions-Erfolg belohnt einen Meisterball",
      kanto_region_def is not None and kanto_region_def["reward"]["items"]
      == [{"key": rewards.MASTER_BALL, "quantity": 1}],
      kanto_region_def and kanto_region_def["reward"])
check("Alice hat Kantos Regions-Erfolg (beim Test-Katalog-Kampf oder jetzt)",
      achievements.region_key("kanto") in achievements.unlocked("alice"))
check("Johto bleibt fuer Alice offen (nicht besiegt)", not achievements.region_cleared("alice", "johto"))

gens = achievements.unlocked_generations("alice")
check("Generation 1 ist immer freigeschaltet", 1 in gens)
check("Kanto (Gen 1) freizuschalten aendert Gen 2 NICHT (Gen 2 = Johto, nicht Kanto)",
      2 not in gens, gens)

# Die Regions-Erfolg-DEFINITION existiert unabhaengig davon, ob sie schon
# freigeschaltet ist - Text soll die Freischaltungs-Mechanik beschreiben.
region_defs = {d["key"]: d for d in achievements.definitions() if d["category"] == "region"}
johto_def = region_defs.get(achievements.region_key("johto"))
check("Regions-Erfolg fuer Johto existiert", johto_def is not None)
check(
    "Regions-Erfolg-Text nennt die betroffene Generation UND die Region",
    johto_def is not None and "generation 2" in johto_def["description"] and "Johto" in johto_def["description"],
    johto_def,
)
check(
    "Regions-Erfolg-Text beschreibt die Spawn-Pool-Freischaltung",
    johto_def is not None and "spawn pool" in johto_def["description"] and "unlocks" in johto_def["description"],
    johto_def,
)

# ------------------------------------------------- Spawn-Pool-Freischaltung
def picked_generations(n: int, user_id: str | None = None) -> set[int]:
    result = set()
    for _ in range(n):
        species = spawn.pick_species(user_id=user_id)
        if species is not None:
            result.add(species["generation"])
    return result


gen1_species = {row["name"] for row in db.query("SELECT name FROM species WHERE generation = 1")}
gen2_species = {row["name"] for row in db.query("SELECT name FROM species WHERE generation = 2")}
check("Fallback-Pokédex hat sowohl Gen-1- als auch Gen-2-Arten", gen1_species and gen2_species,
      (gen1_species, gen2_species))

picks_before = picked_generations(400, "alice")
check(
    "Solange nur Kanto (Gen 1) besiegt ist, spawnt ausschliesslich Generation 1",
    picks_before == {1}, picks_before,
)

johto_ids = [t["id"] for t in all_t if t["region"] == "johto"]
for tid in johto_ids[:-1]:
    achievements.unlock("bob", achievements.trainer_key(tid))
last_johto_new = achievements.on_trainer_defeated("bob", johto_ids[-1])
check("Bob hat Johto alleine komplett (Fortschritt ist pro Nutzer)",
      achievements.region_cleared("bob", "johto"))
check(
    "Der letzte Johto-Sieg schaltet automatisch den Regions-Erfolg frei (on_trainer_defeated)",
    achievements.region_key("johto") in {a["key"] for a in last_johto_new}, last_johto_new,
)
check(
    "Regions-Erfolg fuer Johto ist fuer Bob jetzt freigeschaltet",
    achievements.region_key("johto") in achievements.unlocked("bob"),
)
gens_after_bob = achievements.unlocked_generations("bob")
check(
    "Generation 2 (Johto) ist fuer Bob freigeschaltet, weil er die Liga selbst besiegt hat",
    2 in gens_after_bob, gens_after_bob,
)
check("Generation 3 (Hoenn) bleibt gesperrt", 3 not in gens_after_bob, gens_after_bob)
# Kern der nutzerweiten Warteschlange: Alice profitiert NICHT von Bobs Liga.
gens_alice = achievements.unlocked_generations("alice")
check("Alice profitiert nicht von Bobs Johto-Liga (Freischaltung ist pro Nutzer)",
      2 not in gens_alice, gens_alice)
check("Nur der haushaltsweite Pool (ohne Nutzer, alte geteilte Encounter) vereinigt beide",
      2 in achievements.unlocked_generations())

# League-Tab-Ansicht (trainer_battle.overview): jede Region meldet ihre
# Generation und ob sie im Spawn-Pool schon offen ist - das ist die Basis fuer
# den Freischaltungs-Hinweis im Frontend.
overview_bob = trainer_battle.overview("bob")
ov_by_key = {r["key"]: r for r in overview_bob["regions"]}
check("Kanto meldet Generation 1 und ist immer 'freigeschaltet'",
      ov_by_key["kanto"]["generation"] == 1 and ov_by_key["kanto"]["spawn_unlocked"] is True)
check("Johto meldet Generation 2 und ist jetzt freigeschaltet",
      ov_by_key["johto"]["generation"] == 2 and ov_by_key["johto"]["spawn_unlocked"] is True)
check("Hoenn meldet Generation 3 und ist NOCH NICHT freigeschaltet",
      ov_by_key["hoenn"]["generation"] == 3 and ov_by_key["hoenn"]["spawn_unlocked"] is False)

hoenn_ids = [t["id"] for t in all_t if t["region"] == "hoenn"]

for tid in hoenn_ids:
    achievements.unlock("bob", achievements.trainer_key(tid))
gens_final = achievements.unlocked_generations("bob")
check("Generation 3 (Hoenn) ist jetzt ebenfalls offen", 3 in gens_final, gens_final)

picks_after = picked_generations(400, "bob")
check(
    "Nach Freischaltung von Gen 2 und 3 tauchen auch deren Arten in Bobs Spawn-Pool auf",
    bool(picks_after & {2, 3}), picks_after,
)
check("Generation 4+ bleibt weiterhin gesperrt", not (picks_after - {1, 2, 3}), picks_after)
check("Alices Pool bleibt bei Generation 1", picked_generations(400, "alice") == {1})


# ------------------------------------------------- Seltenheits-Bonus je Liga
# Stand hier: Alice hat Kanto, Bob hat Johto UND Hoenn besiegt.
check("regions_cleared listet Alices Kanto", achievements.regions_cleared("alice") == ["kanto"],
      achievements.regions_cleared("alice"))
check("regions_cleared listet Bobs zwei Regionen",
      achievements.regions_cleared("bob") == ["johto", "hoenn"],
      achievements.regions_cleared("bob"))

bonus = achievements.rarity_bonus("bob")
check("Bonus zaehlt die eigenen Regionen (Bob mit 2)", bonus["regions"] == 2, bonus)
check("Alice hat ihren eigenen Bonus (nur Kanto)",
      achievements.rarity_bonus("alice")["regions"] == 1, achievements.rarity_bonus("alice"))
check("Bonus kennt die Gesamtzahl der Regionen", bonus["total_regions"] == 9, bonus)
check(
    "Faktor je Stufe entspricht rarity.region_multiplier bei 2 von 9 Regionen",
    all(abs(bonus["factors"][t] - rarity.region_multiplier(t, 2, 9)) < 1e-12
        for t in rarity.TIERS),
    bonus,
)
check(
    "Je seltener die Stufe, desto staerker wirkt der Bonus (mythical am staerksten)",
    bonus["factors"][rarity.MYTHICAL] > bonus["factors"][rarity.LEGENDARY]
    > bonus["factors"][rarity.PSEUDO_LEGENDARY] > bonus["factors"][rarity.PARADOX]
    > bonus["factors"][rarity.RARE] > 1.0,
    bonus["factors"],
)
check("Haeufige Arten werden mit Fortschritt sogar SELTENER (Kehrseite der Umverteilung)",
      bonus["factors"][rarity.COMMON] < 1.0, bonus["factors"])

boosted = spawn._boosted_tier_multipliers("bob")
base = rarity.tier_multipliers()
check(
    "Jede Stufe wird mit ihrem eigenen Bonus-Faktor multipliziert",
    all(abs(boosted[t] - base[t] * bonus["factors"][t]) < 1e-12 for t in rarity.TIERS),
    boosted,
)

check(
    "max_factors ist der Faktor bei KOMPLETT besiegtem Spiel (mythical x10, legendary x4)",
    abs(bonus["max_factors"][rarity.MYTHICAL] - 10.0) < 1e-9
    and abs(bonus["max_factors"][rarity.LEGENDARY] - 4.0) < 1e-9
    and abs(bonus["max_factors"][rarity.COMMON] - 58.5 / 83.0) < 1e-9,
    bonus["max_factors"],
)

# Haushaltsweiter Fallback (ohne Nutzer - alte geteilte Encounter, Admin-Spawns):
# dort zaehlt der beste BERECHTIGTE Nutzer, inaktive also nicht. Ist Bob raus,
# bleibt nur noch Alices Kanto uebrig.
db.execute("UPDATE app_user SET last_seen_at = ? WHERE id = 'bob'",
           ((now - timedelta(days=db.ELIGIBLE_DAYS + 1)).isoformat(),))
check("Inaktive Nutzer zaehlen im Haushalts-Fallback nicht mit",
      achievements.rarity_bonus()["regions"] == 1, achievements.rarity_bonus())
check("Bobs eigener Bonus bleibt davon unberuehrt",
      achievements.rarity_bonus("bob")["regions"] == 2)

# Sind ALLE Nutzer inaktiv (0 besiegte Regionen zaehlen), bleiben die Gewichte
# unveraendert - jede Stufe (auch common) startet bei Faktor 1.0.
db.execute("UPDATE app_user SET last_seen_at = ? WHERE id = 'alice'",
           ((now - timedelta(days=db.ELIGIBLE_DAYS + 1)).isoformat(),))
bonus_none = achievements.rarity_bonus()
check("0 besiegte Regionen im Haushalt -> Bonus 0", bonus_none["regions"] == 0, bonus_none)
check("Bonus 0 laesst jede Stufe unveraendert (Faktor 1.0)",
      all(f == 1.0 for f in bonus_none["factors"].values())
      and spawn._boosted_tier_multipliers() == rarity.tier_multipliers(),
      bonus_none)
db.execute("UPDATE app_user SET last_seen_at = ? WHERE id = 'alice'", (now.isoformat(),))
db.execute("UPDATE app_user SET last_seen_at = ? WHERE id = 'bob'", (now.isoformat(),))

ov_alice = achievements.overview("alice")
check("Erfolgs-Uebersicht meldet den eigenen Bonus, nicht den des Haushalts",
      ov_alice["rarity_bonus"]["regions"] == 1 and ov_alice["rarity_bonus"]["own_regions"] == 1,
      ov_alice["rarity_bonus"])

for lang in ("en", "de"):
    strings = json.loads((ROOT / "pokemon_encounter/app/data/i18n" / f"{lang}.json")
                         .read_text(encoding="utf-8"))
    missing = [key for key in ("achievements.rarity_bonus", "achievements.rarity_bonus_none")
               if key not in strings]
    check(f"i18n {lang} kennt die Bonus-Texte", not missing, missing)


# ------------------------------------------------ Top-Vier-Bonus auf Regeneration
from app import healing  # noqa: E402

db.upsert_user("carol", "Carol")
db.set_setting("heal_regen_percent_per_hour", 20)
elite_ids = [t["id"] for t in trainers.all_trainers() if t["kind"] == "elite_four"]
check("Katalog enthaelt Top-Vier-Trainer", len(elite_ids) >= 2, len(elite_ids))
check("Ohne Top-Vier-Sieg: kein Bonus",
      achievements.elite_four_defeated("carol") == 0
      and healing.heal_status("carol")["regen_percent_per_hour"] == 20.0)
achievements.unlock("carol", achievements.trainer_key(elite_ids[0]))
achievements.unlock("carol", achievements.trainer_key(elite_ids[1]))
gym_ids = [t["id"] for t in trainers.all_trainers() if t["kind"] == "gym_leader"]
achievements.unlock("carol", achievements.trainer_key(gym_ids[0]))
check("Nur Top-Vier-Trainer zaehlen (Arenaleiter nicht)",
      achievements.elite_four_defeated("carol") == 2)
check("Regeneration +1 % pro besiegtem Top-Vier-Mitglied",
      healing.heal_status("carol")["regen_percent_per_hour"] == 22.0)
check("Bonus ist pro Nutzer (Bob unveraendert)",
      achievements.elite_four_defeated("bob") == len(
          [i for i in elite_ids if achievements.trainer_key(i) in achievements.unlocked("bob")])
      and healing.regen_percent("bob") == 20.0 + achievements.elite_four_defeated("bob"))

species_id = db.query_one("SELECT id FROM species ORDER BY id LIMIT 1")["id"]
db.execute(
    "INSERT INTO owned_pokemon (user_id, species_id, level, ivs, current_hp, hp_updated_at, caught_at) "
    "VALUES (?,?,?,?,?,?,?)",
    ("carol", species_id, 50, json.dumps({"hp": 15}), 0,
     (now - timedelta(hours=1)).isoformat(), now.isoformat()),
)
carol_mon = db.query_one("SELECT * FROM owned_pokemon WHERE user_id = 'carol'")
carol_max = healing.max_hp_of(carol_mon)
healing.apply_regen("carol", now)
carol_hp = db.query_one("SELECT current_hp FROM owned_pokemon WHERE id = ?", (carol_mon["id"],))["current_hp"]
check("apply_regen nutzt den erhoehten Satz (22 % nach 1 h)",
      carol_hp == int(carol_max * 22 / 100), f"{carol_hp}/{carol_max}")


# ------------------------------------- Top-Vier-&-Champ-Bonus auf die Heil-Abklingzeit
db.set_setting("heal_team_cooldown_minutes", 60)
champion_ids = [t["id"] for t in trainers.all_trainers() if t["kind"] == "champion"]
check("Katalog enthaelt Champs", len(champion_ids) >= 1, len(champion_ids))
check("Carols Abklingzeit ist bereits um ihre 2 Top-Vier-Siege verkuerzt",
      achievements.elite_four_and_champion_defeated("carol") == 2
      and healing.heal_status("carol")["cooldown_minutes"] == 58.0,
      healing.heal_status("carol")["cooldown_minutes"])
achievements.unlock("carol", achievements.trainer_key(champion_ids[0]))
check("Ein besiegter Champ zaehlt ebenso wie Top Vier fuer die Abklingzeit-Verkuerzung",
      achievements.elite_four_and_champion_defeated("carol") == 3
      and healing.heal_status("carol")["cooldown_minutes"] == 57.0,
      healing.heal_status("carol")["cooldown_minutes"])
check("Arenaleiter zaehlen nicht fuer die Abklingzeit-Verkuerzung (nur Top Vier + Champ)",
      healing.cooldown_minutes("bob")
      == 60.0 - achievements.elite_four_and_champion_defeated("bob"),
      healing.cooldown_minutes("bob"))
db.set_setting("heal_team_cooldown_minutes", 2)
check("Abklingzeit sinkt nie unter 0 Minuten",
      healing.cooldown_minutes("carol") == 0.0, healing.cooldown_minutes("carol"))
db.set_setting("heal_team_cooldown_minutes", 60)

for lang in ("en", "de"):
    strings = json.loads((ROOT / "pokemon_encounter/app/data/i18n" / f"{lang}.json")
                         .read_text(encoding="utf-8"))
    check(f"i18n {lang} kennt den Abklingzeit-Bonus-Text",
          "league.cooldown_bonus_reward" in strings)


# Team preview entries carry everything the gym summary popup shows
# (types, stats, moves), so the player can plan a lineup before fighting.
# (No real trainer resolves with the small offline Pokedex, so use a synthetic one again.)
_saved_raw, _saved_trainers = trainers._raw, trainers._trainers
trainers._raw = {
    "regions": [{"key": "kanto", "label": "Kanto"}],
    "trainers": [{
        "id": "test_preview", "kind": "gym_leader", "region": "kanto",
        "name": "Vorschau", "team": [["magikarp", 12]],
    }, {
        "id": "test_preview_e4", "kind": "elite_four", "region": "kanto",
        "name": "Vorschau Vier", "team": [["magikarp", 12]],
    }, {
        "id": "test_preview_champ", "kind": "champion", "region": "kanto",
        "name": "Vorschau Champ", "team": [["magikarp", 12]],
    }],
}
trainers._trainers = None
preview_trainers = trainer_battle.overview("bob")["regions"][0]["trainers"]
preview_trainer, preview_e4, preview_champ = preview_trainers[0], preview_trainers[1], preview_trainers[2]
raw_trainer = trainers.get("test_preview")
roster_species, roster_level = trainers.resolve_team(raw_trainer)[0]
trainers._raw, trainers._trainers = _saved_raw, _saved_trainers
check("Trainer mit aufgeloestem Team liefert Vorschau-Eintraege", preview_trainer["available"], preview_trainer)
check("Vorschau: Top-Vier-Mitglied zeigt den +1-%-Regenerationsbonus, Arenaleiter nicht",
      preview_e4["regen_bonus"] == healing.ELITE_FOUR_REGEN_BONUS == 1 and preview_trainer["regen_bonus"] == 0,
      (preview_e4["regen_bonus"], preview_trainer["regen_bonus"]))
check("Vorschau: Top Vier und Champ zeigen den Abklingzeit-Bonus, Arenaleiter nicht",
      preview_e4["cooldown_bonus"] == preview_champ["cooldown_bonus"]
      == healing.ELITE_FOUR_CHAMPION_COOLDOWN_REDUCTION_MINUTES == 1
      and preview_trainer["cooldown_bonus"] == 0,
      (preview_e4["cooldown_bonus"], preview_champ["cooldown_bonus"], preview_trainer["cooldown_bonus"]))
first_mon = preview_trainer["team"][0]
check("Vorschau-Eintrag enthaelt Typen, Basiswerte, Werte und Attacken",
      bool(first_mon["types"]) and set(first_mon["base_stats"]) == set(first_mon["stats"])
      == {"hp", "atk", "def", "spa", "spd", "spe"} and 1 <= len(first_mon["moves"]) <= 4, first_mon)
real = trainer_battle._opponent(raw_trainer, roster_species, roster_level, None)
check("Vorschau-Werte und -Attacken stimmen mit dem echten Kampf-Gegner ueberein",
      first_mon["stats"]["hp"] == real.max_hp and first_mon["stats"]["atk"] == real.stat("atk")
      and [m["key"] for m in first_mon["moves"]] == real.moves, first_mon)


# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
