"""Tests fuer den AttackDex-Endpunkt (`GET /api/movedex`) und die
Attacken-Beschreibung in `app/moves.py`, auf der er aufbaut.

Aufruf:  python3 tests/test_movedex.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-movedex-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import db, moves as moves_mod, seed  # noqa: E402
from app.api import movedex as movedex_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")


# --------------------------------------------------------------- moves.py
all_moves = moves_mod.all_moves()
check("all_moves() liefert die volle Attacken-Liste", len(all_moves) > 0)

psychic = moves_mod.describe("psychic")
check("describe() liefert Name/Typ/Stärke/Genauigkeit",
      psychic["name"] == "Psychic" and psychic["type"] == "psychic"
      and psychic["power"] == 90 and psychic["accuracy"] == 100)
check("describe() liefert Statuswert-Aenderungen",
      psychic["stat_changes"] == [{"stat": "spd", "stages": -1, "target": "target"}]
      and psychic["stat_chance"] == 10)

tackle = moves_mod.describe("tackle")
check("describe() liefert None fuer Attacken ohne Statuswert-Aenderung",
      tackle["stat_changes"] is None and tackle["stat_chance"] is None)

growl = moves_mod.describe("growl")
check("Reine Status-Attacken haben power=None statt 0", growl["power"] is None)

# Effekt-Felder fuer die AttackDex-Karten (Drain, Rueckstoss, Crit, Falle, Prioritaet)
absorb = moves_mod.describe("absorb")
check("describe() liefert Drain-Anteil", absorb["drain"] == 0.5, absorb["drain"])
double_edge = moves_mod.describe("double_edge")
check("describe() liefert Rueckstoss-Anteil", double_edge["recoil"] == 0.33, double_edge["recoil"])
razor_leaf = moves_mod.describe("razor_leaf")
check("describe() liefert high_crit als Bool", razor_leaf["high_crit"] is True)
mean_look = moves_mod.describe("mean_look")
check("describe() liefert trap als Bool", mean_look["trap"] is True)
check("Ohne Effekt sind drain/recoil None und high_crit/trap False",
      tackle["drain"] is None and tackle["recoil"] is None
      and tackle["high_crit"] is False and tackle["trap"] is False)
check("Priorität kommt aus dem Katalog (Quick Attack +1)",
      moves_mod.describe("quick_attack")["priority"] == 1)


# ------------------------------------------ Real learnsets (learnsets.json)
# Which species learns which move, and when, follows the games (gen 9 /
# Scarlet-Violet) - not the old "own types + normal" rule.
pika = {"id": 25, "type1": "electric", "type2": None, "base_atk": 55, "base_spa": 50}
rattata = {"id": 19, "type1": "normal", "type2": None, "base_atk": 56, "base_spa": 25}
gastly = {"id": 92, "type1": "ghost", "type2": "poison", "base_atk": 35, "base_spa": 100}
umbreon = {"id": 197, "type1": "dark", "type2": None, "base_atk": 65, "base_spa": 60}
snivy = {"id": 495, "type1": "grass", "type2": None, "base_atk": 45, "base_spa": 45}
karp = {"id": 129, "type1": "water", "type2": None, "base_atk": 10, "base_spa": 15}
check("Lernliste fuer alle 1025 Arten vorhanden", len(moves_mod.all_learnsets()) == 1025)
check("Karmesin/Purpur-Arten nutzen Gen 9", moves_mod.learnset(pika)["gen"] == 9)
check("Pikachu lernt Donnerblitz auf Lv. 36 (wie in Karmesin/Purpur)",
      moves_mod.learn_level(pika, "thunderbolt") == 36)
check("... also nicht auf Lv. 35, aber auf Lv. 36",
      "thunderbolt" not in moves_mod.learned_moves(pika, 35)
      and "thunderbolt" in moves_mod.learned_moves(pika, 36))
check("Typfremde Level-Attacken zaehlen (Pikachu: Eisenschweif)",
      "iron_tail" in moves_mod.learned_moves(pika, 50))
check("Nicht in der Lernliste = nicht lernbar, auch bei passendem Typ (Rattfratz: Hyperschall)",
      "hyper_voice" not in moves_mod.learned_moves(rattata, 100))
check("Pikachu lernt Mean Look nicht", "mean_look" not in moves_mod.learned_moves(pika, 100))
check("Rattata (Normal) lernt Mean Look nicht", "mean_look" not in moves_mod.learned_moves(rattata, 100))
check("Nebulak lernt Mean Look ab Lv. 8", moves_mod.learn_level(gastly, "mean_look") == 8
      and "mean_look" in moves_mod.learned_moves(gastly, 8)
      and "mean_look" not in moves_mod.learned_moves(gastly, 7))
check("Nachtara lernt Mean Look auf Lv. 50", moves_mod.learn_level(umbreon, "mean_look") == 50)
check("Serpifeu bekommt Mean Look nur als Ei-Attacke",
      "mean_look" in moves_mod.egg_moves(snivy) and moves_mod.learn_level(snivy, "mean_look") is None)
check("Ei-Attacken nur bei der Basisstufe (Glurak hat keine)",
      moves_mod.egg_moves({"id": 6}) == [] and moves_mod.egg_moves({"id": 4}))
check("Geerbte Ei-Attacke ist ab Lv. 1 lernbar",
      "mean_look" in moves_mod.learned_moves(snivy, 1, egg=["mean_look"]))
check("Per TM gelernt zaehlt weiter (Levelgrenze der Attacke)",
      "mean_look" in moves_mod.learned_moves(pika, 50, ["mean_look"])
      and "mean_look" not in moves_mod.learned_moves(pika, 19, ["mean_look"]))
check("Ohne Species-ID gilt die alte Typregel",
      "tackle" in moves_mod.learned_moves({"type1": "normal"}, 50))
check("Karpador (Lv. 5) kaempft mit seiner ersten Schadensattacke Tackle (lernt sie erst auf Lv. 15)",
      moves_mod.moveset_for(karp, 5) == ["tackle"] and moves_mod.learn_level(karp, "tackle") == 15)
check("Abra (keine Schadensattacke per Level) bekommt eine Ersatz-Attacke",
      len(moves_mod.moveset_for({"id": 63, "type1": "psychic", "type2": None,
                                 "base_atk": 20, "base_spa": 105}, 10)) == 1)
check("Standard-Moveset nur aus der Lernliste bis zum Level",
      all(moves_mod.learn_level(pika, k) <= 50 for k in moves_mod.moveset_for(pika, 50)))
learners = moves_mod.learners("mean_look")
check("learners(): Level-Lerner mit Level, Ei-Lerner separat",
      learners["level"].get(197) == 50 and 495 in learners["egg"] and 25 not in learners["level"])

# Migration: existing saves drop battle moves the species can't know.
db_pika = db.query_one("SELECT * FROM species WHERE id = 25")
db_snivy = db.query_one("SELECT * FROM species WHERE id = 495")
if db_pika and db_snivy:
    def add(species_id: int, selected: list[str] | None, egg: list[str] | None = None,
            taught: list[str] | None = None, level: int = 50) -> int:
        return db.execute(
            "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
            "caught_at, selected_moves, egg_moves, taught_moves) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("alice", species_id, level, 0, 0, "{}", "hardy", "2026-01-01T00:00:00+00:00",
             json.dumps(selected) if selected is not None else None,
             json.dumps(egg) if egg is not None else None,
             json.dumps(taught) if taught is not None else None),
        )
    p_mixed = add(25, ["mean_look", "thunderbolt"])
    p_only = add(25, ["mean_look"])
    p_type_rule = add(25, ["hyper_voice", "thunderbolt"])
    p_low = add(25, ["thunderbolt", "quick_attack"], level=30)
    p_tm = add(25, ["mean_look", "thunderbolt"], taught=["mean_look"])
    s_egg = add(495, ["mean_look", "leaf_blade"], egg=["mean_look"])
    s_no_egg = add(495, ["mean_look", "leaf_blade"])
    db.init_db()
    sel = lambda oid: db.query_one("SELECT selected_moves FROM owned_pokemon WHERE id = ?", (oid,))["selected_moves"]
    check("Migration entfernt Mean Look bei Pikachu", json.loads(sel(p_mixed)) == ["thunderbolt"], sel(p_mixed))
    check("Migration: leere Auswahl -> Standard-Moveset (NULL)", sel(p_only) is None, sel(p_only))
    check("Migration entfernt Attacken der alten Typregel (Pikachu: Hyperschall)",
          json.loads(sel(p_type_rule)) == ["thunderbolt"], sel(p_type_rule))
    check("Migration entfernt noch nicht erreichte Level-Attacken (Donnerblitz auf Lv. 30)",
          json.loads(sel(p_low)) == ["quick_attack"], sel(p_low))
    check("Migration laesst per TM gelernte Attacken", json.loads(sel(p_tm)) == ["mean_look", "thunderbolt"], sel(p_tm))
    check("Migration laesst geerbte Ei-Attacken (Serpifeu: Mean Look)",
          json.loads(sel(s_egg)) == ["mean_look", "leaf_blade"], sel(s_egg))
    check("Ohne Ei-Herkunft verliert Serpifeu Mean Look",
          json.loads(sel(s_no_egg)) == ["leaf_blade"], sel(s_no_egg))
    db.init_db()
    check("Migration ist idempotent", json.loads(sel(p_mixed)) == ["thunderbolt"], sel(p_mixed))
else:
    check("Fallback-Pokedex enthaelt Pikachu und Serpifeu", False)

# --------------------------------------------------------- GET /api/movedex
result = movedex_api.get_movedex(current_user=alice)
check("Antwort enthaelt so viele Attacken wie all_moves()",
      len(result["moves"]) == len(all_moves), len(result["moves"]))
check("Jede Attacke hat ihren Katalog-Key als 'key'",
      all(entry["key"] in all_moves for entry in result["moves"]))
keys = [entry["key"] for entry in result["moves"]]
check("Keine doppelten Attacken", len(keys) == len(set(keys)))
categories = {entry["category"] for entry in result["moves"]}
check("Physisch/Speziell/Status sind vertreten", categories == {"physical", "special", "status"}, categories)
by_key = {entry["key"]: entry for entry in result["moves"]}
check("AttackDex liefert die Lerner je Attacke (Level/TM/Ei)",
      by_key["thunderbolt"]["learners"]["level"].get(25) == 36 and 25 in by_key["thunderbolt"]["learners"]["tm"])


# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
