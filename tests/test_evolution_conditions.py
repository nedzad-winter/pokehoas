"""Tests fuer Level-Entwicklungen ohne Level (Lickitung + Walzer, Mantyke +
Remoraid, Sniebel + Scharfklaue, 1000 Schritte) und die dafuer ergaenzten
Attacken (Walzer, Mimikry, Verhoehner, Drachenschrei ...).

Aufruf:  python3 tests/test_evolution_conditions.py
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-evolution-conditions-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, db, evolution, held_items, moves, seed  # noqa: E402
from app.economy import catalog_by_key  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


NOW = datetime.now(timezone.utc).isoformat()
IVS = {k: 10 for k in ("hp", "atk", "def", "spa", "spd", "spe")}
random.seed(7)

db.init_db()
seed.seed_from_fallback()


def species_rec(sid: int, name: str, evolves_from: int | None = None) -> dict:
    return {"id": sid, "name": name, "name_de": name.title(), "name_en": name.title(),
            "type1": "normal", "catch_rate": 45, "base_exp": 100, "growth_rate": "medium_fast",
            "base_hp": 60, "base_atk": 60, "base_def": 60, "base_spa": 60, "base_spd": 60,
            "base_spe": 60, "evolves_from": evolves_from,
            "generation": seed.generation_for(sid)}


for rec in (species_rec(108, "lickitung"), species_rec(463, "lickilicky", 108),
            species_rec(223, "remoraid"), species_rec(458, "mantyke"),
            species_rec(226, "mantine", 458), species_rec(215, "sneasel"),
            species_rec(461, "weavile", 215), species_rec(922, "pawmo"),
            species_rec(923, "pawmot", 922), species_rec(946, "bramblin"),
            species_rec(947, "brambleghast", 946)):
    seed._upsert(rec)

# --------------------------------------------------- PokeAPI-Kette -> Zeilen


def ref(name: str, sid: int) -> dict:
    return {"name": name, "url": f"https://pokeapi.co/api/v2/pokemon-species/{sid}/"}


def chain(base: tuple[str, int], target: tuple[str, int], **details) -> dict:
    det = {"trigger": {"name": "level-up"}, "min_level": None, "is_default": True, **details}
    return {"chain": {"species": ref(*base), "evolves_to": [
        {"species": ref(*target), "evolves_to": [], "evolution_details": [det]}]}}


def only_row(doc: dict) -> dict:
    rows = evolution.rows_from_chain(doc)
    assert len(rows) == 1, rows
    return rows[0]


row = only_row(chain(("lickitung", 108), ("lickilicky", 463),
                     known_move={"name": "rollout"}))
check("Lickitung: Level-up ohne Level, braucht Walzer",
      row["min_level"] is None and row["detail"] == {"known_move": "rollout"}, row)
row = only_row(chain(("mantyke", 458), ("mantine", 226), party_species=ref("remoraid", 223)))
check("Mantyke: Remoraid im Team", row["detail"] == {"party_species": "remoraid"}, row)
row = only_row(chain(("sneasel", 215), ("weavile", 461), held_item={"name": "razor-claw"},
                     time_of_day="night"))
check("Sniebel: Scharfklaue + Nacht",
      row["detail"] == {"held_item": "razor_claw", "time_of_day": "night"}, row)
row = only_row(chain(("bramblin", 946), ("brambleghast", 947), min_steps=1000))
check("1000 Schritte werden zu einem festen Level",
      row["min_level"] == evolution.FRIENDSHIP_LEVEL_BASE and row["detail"] == {"steps": True}, row)
check("Seed-Version angehoben (bestehende DBs werden neu geladen)", evolution.SEED_VERSION != "2")

# ------------------------------------------------------------ Anwendung
evolution._write_rows([
    {"from": 108, "to": 463, "trigger": "level-up", "min_level": None,
     "detail": {"known_move": "rollout"}},
    {"from": 458, "to": 226, "trigger": "level-up", "min_level": None,
     "detail": {"party_species": "remoraid"}},
    {"from": 215, "to": 461, "trigger": "level-up", "min_level": None,
     "detail": {"held_item": "razor_claw", "time_of_day": "night"}},
    {"from": 946, "to": 947, "trigger": "level-up", "min_level": 20, "detail": {"steps": True}},
    {"from": 922, "to": 923, "trigger": "level-up", "min_level": None},  # keine Bedingung
], "pokeapi", overwrite=True)

alice = db.upsert_user("alice", "Alice")


def add_owned(species_id: int, level: int, held_item: str | None = None,
              party_slot: int | None = None) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "gender, held_item, party_slot, caught_at) VALUES (?,?,?,?,0,?,?,?,?,?,?)",
        ("alice", species_id, level, battle.exp_for_level(level, "medium_fast"), json.dumps(IVS),
         "hardy", "male", held_item, party_slot, NOW),
    )


def owned(owned_id: int) -> dict:
    return db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))


def targets(owned_id: int, time_of_day: str | None = None) -> list[int]:
    return [e["to_species_id"] for e in evolution.level_evolutions(owned(owned_id), time_of_day)]


learn_level = moves.learn_level(db.query_one("SELECT * FROM species WHERE id = 108"), "rollout")
check("Lickitung lernt Walzer per Level-up", learn_level is not None, learn_level)

lick = add_owned(108, learn_level - 1)
check("Lickitung ohne Walzer: keine Entwicklung", targets(lick) == [])
db.execute("UPDATE owned_pokemon SET level = ? WHERE id = ?", (learn_level, lick))
check("Lickitung mit Walzer (kein Mindestlevel): kann sich entwickeln", targets(lick) == [463])
res = evolution.evolve_by_level("alice", lick)
check("Lickitung entwickelt sich zu Schlurplek", res["to"]["id"] == 463, res["to"])

mantyke = add_owned(458, 30, party_slot=1)
check("Mantyke ohne Remoraid im Team: nein", targets(mantyke) == [])
remoraid_box = add_owned(223, 30)
check("Remoraid nur in der Box zaehlt nicht", targets(mantyke) == [])
db.execute("UPDATE owned_pokemon SET party_slot = 2 WHERE id = ?", (remoraid_box,))
check("Remoraid im Team: Mantyke kann sich entwickeln", targets(mantyke) == [226])

sneasel = add_owned(215, 30)
check("Sniebel ohne Item: nein", targets(sneasel, "night") == [])
db.execute("UPDATE owned_pokemon SET held_item = 'razor_claw' WHERE id = ?", (sneasel,))
check("Sniebel mit Scharfklaue tagsueber: nein", targets(sneasel, "day") == [])
check("Sniebel mit Scharfklaue nachts: ja", targets(sneasel, "night") == [461])
check("Mehrere Ziele? nein - Sammel-Entwicklung waere moeglich",
      len(evolution.level_evolutions(owned(sneasel), "night")) == 1)
# evolve_by_level nutzt die echte Uhrzeit - Item-Verbrauch ueber _evolve pruefen.
evolution._evolve(owned(sneasel), 461, "en", consume_held=True)
check("Scharfklaue wird bei der Entwicklung verbraucht",
      owned(sneasel)["species_id"] == 461 and owned(sneasel)["held_item"] is None)

bramblin = add_owned(946, 19)
check("Schritte-Entwicklung: unter dem festen Level nein", targets(bramblin) == [])
db.execute("UPDATE owned_pokemon SET level = 20 WHERE id = ?", (bramblin,))
check("Schritte-Entwicklung: am festen Level ja", targets(bramblin) == [947])

pawmo = add_owned(922, 50)
check("Level-up ohne pruefbare Bedingung bleibt unbenutzbar", targets(pawmo) == [])

# -------------------------------------------------- Katalog: Items/Attacken
shop = catalog_by_key()
for key in ("oval_stone", "razor_fang", "razor_claw"):
    check(f"{key}: im Shop und als Halte-Item", key in shop and held_items.get(key) is not None)
check("Scharfklaue erhoeht die Volltrefferquote", held_items.effect_of("razor_claw") == "crit_stage")
for key in ("rollout", "mimic", "double_hit", "twin_beam", "hyper_drill", "taunt", "dragon_cheer"):
    check(f"Attacke {key} im Katalog", key in moves.all_moves())
for sid, key in ((108, "rollout"), (439, "mimic"), (438, "mimic"), (190, "double_hit"),
                 (203, "twin_beam"), (206, "hyper_drill"), (852, "taunt")):
    sp = {"id": sid}
    check(f"Art {sid} lernt {key} per Level-up", moves.learn_level(sp, key) is not None)
tms = json.loads((ROOT / "pokemon_encounter/app/data/tms.json").read_text(encoding="utf-8"))
tm_moves = {t["move"] for t in tms["tms"]}
check("TMs fuer Verhoehner und Drachenschrei (Dipplin lernt Drachenschrei nur per TM)",
      {"taunt", "dragon_cheer"} <= tm_moves
      and "dragon_cheer" in moves.all_learnsets()["1011"]["tm"])
# Jede Bedingung der Level-Entwicklungen ist erfuellbar: der Zielmove ist lernbar.
need = {108: "rollout", 114: "ancient_power", 439: "mimic", 193: "ancient_power", 438: "mimic",
        190: "double_hit", 203: "twin_beam", 206: "hyper_drill", 221: "ancient_power",
        762: "stomp", 803: "dragon_pulse", 1011: "dragon_cheer", 852: "taunt"}
for sid, key in need.items():
    entry = moves.all_learnsets()[str(sid)]
    check(f"Art {sid}: {key} ist lernbar",
          key in entry["level"] or key in entry["tm"] or key in entry["egg"])

# ------------------------------------------------------- Kampf-Effekte
SP = db.query_one("SELECT * FROM species WHERE name = 'pikachu'")
DRAGON = {**SP, "type1": "dragon"}


def mon(sp: dict = SP, level: int = 30) -> battle.Combatant:
    return battle.Combatant(species=sp, level=level, ivs=dict(IVS))


def new_battle(a: battle.Combatant, w: battle.Combatant) -> battle.Battle:
    return battle.Battle(wild=w, team=[a])


a, w = mon(), mon()
b = new_battle(a, w)
battle.use_move(b, a, w, "taunt")
check("Verhoehner setzt den Zustand", w.volatile.get("taunt") == battle.TAUNT_TURNS)
n_log = len(b.log)
battle.use_move(b, w, a, "growl")
check("Verhoehnt: Status-Attacke wird verweigert",
      a.stat_stages.get("atk", 0) == 0 and len(b.log) == n_log + 1, b.log[-1])
hp = a.current_hp
battle.use_move(b, w, a, "tackle")
check("Verhoehnt: Schadensattacke geht", a.current_hp < hp)
for _ in range(battle.TAUNT_TURNS):
    battle.end_of_turn(b)
check("Verhoehner laeuft ab", not w.volatile.get("taunt"))
battle.use_move(b, w, a, "growl")
check("Nach Ablauf wirken Status-Attacken wieder", a.stat_stages.get("atk", 0) == -1)

a, w = mon(), mon()
b = new_battle(a, w)
battle.use_move(b, a, w, "mimic")
check("Mimikry ohne Vorlage schlaegt fehl", b.log[-1] == "But it failed!", b.log[-1])
battle.use_move(b, w, a, "growl")
battle.use_move(b, a, w, "mimic")
check("Mimikry benutzt die letzte Attacke des Gegners", w.stat_stages.get("atk", 0) == -1, b.log[-3:])
check("Mimikry wird nicht selbst als letzte Attacke gemerkt", a.volatile["last_move"] == "growl")

a, w = mon(), mon()
b = new_battle(a, w)
check("Drachenschrei: ohne Boost Stufe 0", battle._crit_stage(a, moves.get("tackle")) == 0)
battle.use_move(b, a, w, "dragon_cheer")
check("Drachenschrei: +1 Volltreffer-Stufe", battle._crit_stage(a, moves.get("tackle")) == 1)
d = mon(DRAGON)
battle.use_move(new_battle(d, w), d, w, "dragon_cheer")
check("Drachenschrei: +2 fuer Drachen-Typen", battle._crit_stage(d, moves.get("tackle")) == 2)

print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
