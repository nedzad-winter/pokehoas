"""Tests fuer Faehigkeiten: Katalog, Kampf-Effekte, Seeding und Zuordnung.

Aufruf:  python3 tests/test_abilities.py
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-abilities-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import abilities, battle, db, moves as moves_mod, seed, spawn  # noqa: E402
from app.api import encounters as encounters_api  # noqa: E402
from app.api import team as team_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def with_random(**patched):
    """Ersetzt random.<name> zeitweise (siehe tests/test_battle.py)."""
    saved = {name: getattr(random, name) for name in patched}
    for name, fn in patched.items():
        setattr(random, name, fn)

    def restore() -> None:
        for name, fn in saved.items():
            setattr(random, name, fn)

    return restore


random.seed(int(os.environ.get("TEST_SEED", "1234")))
db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
IVS = {k: 15 for k in spawn.STAT_KEYS}
NOW = datetime.now().astimezone()


def make_species(species_id: int, name: str, type1: str, type2: str | None = None) -> dict:
    db.execute(
        """
        INSERT INTO species (id, name, name_de, type1, type2, generation, catch_rate,
            base_exp, growth_rate, base_hp, base_atk, base_def, base_spa, base_spd, base_spe)
        VALUES (?,?,?,?,?,1,45,64,'medium_fast',100,80,80,80,80,80)
        ON CONFLICT(id) DO NOTHING
        """,
        (species_id, name, name, type1, type2),
    )
    return db.query_one("SELECT * FROM species WHERE id = ?", (species_id,))


fire_sp = make_species(90101, "test-fire", "fire")
water_sp = make_species(90102, "test-water", "water")
normal_sp = make_species(90103, "test-normal", "normal")
ground_sp = make_species(90104, "test-ground", "ground")
poison_sp = make_species(90105, "test-poison", "poison")


def mon(sp: dict, level: int = 30, ability: str | None = None, hp_fraction: float = 1.0) -> battle.Combatant:
    c = battle.Combatant(species=sp, level=level, ivs=dict(IVS), ability=ability)
    if hp_fraction < 1.0:
        c.current_hp = max(1, int(c.max_hp * hp_fraction))
    return c


# ============================================================== Katalog
cat = abilities.catalog()
check("Katalog: jede Faehigkeit hat name_de + Beschreibung (de/en)",
      len(cat) > 250 and all(a.get("name_de") and a.get("description") and a.get("description_de")
                             for a in cat.values()),
      f"{len(cat)} Faehigkeiten")
check("Katalog: jede Faehigkeit hat entweder einen Kampfeffekt oder einen inactive-Grund",
      all(bool(a.get("effect")) != bool(a.get("inactive")) for a in cat.values()))
check("Katalog: inactive-Gruende sind die bekannten (i18n ability.inactive.*)",
      {a["inactive"] for a in cat.values() if a.get("inactive")}
      <= {"doubles", "weather", "overworld", "mechanic", "todo"})
check("describe() liefert den inactive-Grund mit",
      abilities.describe("swift_swim")["inactive"] == "weather"
      and abilities.describe("intimidate")["inactive"] is None)
check("get() liefert None fuer unbekannten/leeren Schluessel",
      abilities.get(None) is None and abilities.get("nonsense_ability") is None)
check("effect_of() liefert None fuer unbekannte Faehigkeit (No-Op, kein Crash)",
      abilities.effect_of("nonsense_ability") is None)
check("label() faellt bei unbekannter (aber gesetzter) Faehigkeit auf lesbaren Namen zurueck",
      abilities.label("some_unimplemented_ability") == "Some Unimplemented Ability")
check("label() liefert den englischen Namen fuer implementierte Faehigkeit (Englisch ist Standard)",
      abilities.label("static") == "Static")

# Ein Combatant mit einer voellig unbekannten Faehigkeit darf nirgends crashen.
unknown = mon(normal_sp, ability="totally_unknown_slug")
b = battle.Battle(wild=mon(fire_sp), team=[unknown])
try:
    battle.use_move(b, unknown, b.wild, "tackle")
    battle.effective_stat(unknown, "atk")
    battle.inflict_status(b, unknown, "paralysis")
    check("Unbekannte Faehigkeit ist ueberall ein reines No-Op", True)
except Exception as exc:  # noqa: BLE001
    check("Unbekannte Faehigkeit ist ueberall ein reines No-Op", False, repr(exc))


# ==================================================== Status-Immunitaet
IMMUNITY_CASES = [
    ("immunity", "poison"), ("immunity", "toxic"),
    ("limber", "paralysis"), ("insomnia", "sleep"),
    ("water_veil", "burn"), ("magma_armor", "freeze"),
]
for ability_key, status in IMMUNITY_CASES:
    target = mon(normal_sp, ability=ability_key)
    b = battle.Battle(wild=mon(fire_sp), team=[target])
    ok = battle.inflict_status(b, target, status)
    check(f"{abilities.label(ability_key)}: blockiert {status}", not ok and target.status is None)

# Ohne die Faehigkeit greift derselbe Status normal (Kontrolle).
control = mon(normal_sp)
b = battle.Battle(wild=mon(fire_sp), team=[control])
check("Kontrolle: ohne Immunitaets-Faehigkeit greift Paralyse normal",
      battle.inflict_status(b, control, "paralysis") and control.status == "paralysis")


# ================================================== Kontakt-Status-Chance
CONTACT_CASES = [
    ("static", "paralysis"), ("flame_body", "burn"), ("poison_point", "poison"),
]
for ability_key, status in CONTACT_CASES:
    # Angreifer bewusst normal-typisiert (nicht Feuer/Gift/Elektro/Eis), damit
    # keine der getesteten Status-TYP-Immunitaeten (siehe STATUS_IMMUNE_TYPES)
    # dazwischenfunkt - hier soll ausschliesslich die Faehigkeit greifen.
    attacker = mon(normal_sp)
    defender = mon(normal_sp, ability=ability_key)
    b = battle.Battle(wild=defender, team=[attacker])
    restore = with_random(random=lambda: 0.0)  # trifft immer, Kontakt-Chance greift immer
    try:
        battle.use_move(b, attacker, defender, "tackle")
        check(f"{abilities.label(ability_key)}: steckt den Angreifer bei physischem Treffer an",
              attacker.status == status, f"Angreifer-Status: {attacker.status}")
    finally:
        restore()

# Sporenwolke: einer von drei Status, aber IMMER einer davon (Chance erzwungen).
attacker = mon(normal_sp)
defender = mon(normal_sp, ability="effect_spore")
b = battle.Battle(wild=defender, team=[attacker])
restore = with_random(random=lambda: 0.0)
try:
    battle.use_move(b, attacker, defender, "tackle")
    check("Sporenwolke: steckt mit einem von Gift/Paralyse/Schlaf an",
          attacker.status in ("poison", "paralysis", "sleep"), attacker.status)
finally:
    restore()

# Kontakt-Faehigkeiten wirken nur bei PHYSISCHEN Treffern.
attacker = mon(normal_sp)
defender = mon(normal_sp, ability="static")
b = battle.Battle(wild=defender, team=[attacker])
restore = with_random(random=lambda: 0.0)
try:
    battle.use_move(b, attacker, defender, "water_gun")  # speziell, kein Kontakt
    check("Statik wirkt NICHT bei einer speziellen Attacke", attacker.status is None)
finally:
    restore()

# Chance auf 0 -> loest nie aus (Kontrolle mit hohem Wurf).
attacker = mon(normal_sp)
defender = mon(normal_sp, ability="static")
b = battle.Battle(wild=defender, team=[attacker])
restore = with_random(random=lambda: 0.99)
try:
    battle.use_move(b, attacker, defender, "tackle")
    check("Statik wirkt NICHT, wenn die Chance nicht trifft", attacker.status is None)
finally:
    restore()


# ============================================== Same-Type-Low-HP-Boost
plain_full = battle.expected_damage(mon(fire_sp, hp_fraction=1.0), mon(water_sp), "fire_fang")
blaze_full = battle.expected_damage(mon(fire_sp, ability="blaze", hp_fraction=1.0), mon(water_sp), "fire_fang")
check("Grossbrand wirkt NICHT ueber 1/3 KP", abs(plain_full - blaze_full) < 0.01,
      f"{plain_full:.1f} vs {blaze_full:.1f}")

plain_low = battle.expected_damage(mon(fire_sp, hp_fraction=0.3), mon(water_sp), "fire_fang")
blaze_low = battle.expected_damage(mon(fire_sp, ability="blaze", hp_fraction=0.3), mon(water_sp), "fire_fang")
check("Grossbrand: unter 1/3 KP 50 % mehr Schaden bei Feuer-Attacken",
      abs(blaze_low / plain_low - 1.5) < 0.01, f"{plain_low:.1f} -> {blaze_low:.1f}")

blaze_low_offtype = battle.expected_damage(
    mon(fire_sp, ability="blaze", hp_fraction=0.3), mon(water_sp), "tackle"
)
plain_low_offtype = battle.expected_damage(mon(fire_sp, hp_fraction=0.3), mon(water_sp), "tackle")
check("Grossbrand wirkt NICHT auf eine Attacke eines anderen Typs (Tackle)",
      abs(blaze_low_offtype - plain_low_offtype) < 0.01,
      f"{plain_low_offtype:.1f} vs {blaze_low_offtype:.1f}")


# ========================================================= Adaptability
fire_move = moves_mod.get("fire_fang")
stab_plain = battle._stab_multiplier(mon(fire_sp), fire_move)
stab_adapt = battle._stab_multiplier(mon(fire_sp, ability="adaptability"), fire_move)
check("STAB ist normal x1,5", stab_plain == 1.5)
check("Anpassungsfaehigkeit: STAB steigt auf x2", stab_adapt == 2.0)

dmg_plain = battle.expected_damage(mon(fire_sp, level=50), mon(water_sp, level=50), "fire_fang")
dmg_adapt = battle.expected_damage(mon(fire_sp, level=50, ability="adaptability"), mon(water_sp, level=50), "fire_fang")
check("Anpassungsfaehigkeit erhoeht den erwarteten STAB-Schaden entsprechend",
      abs(dmg_adapt / dmg_plain - (2.0 / 1.5)) < 0.02, f"{dmg_plain:.1f} -> {dmg_adapt:.1f}")


# ============================================================== Levitate
levitator = mon(ground_sp, ability="levitate")
dealt, effectiveness, _ = battle.calc_damage(mon(normal_sp), levitator, moves_mod.get("earthquake"))
check("Schwebe: Boden-Attacke ist komplett wirkungslos", dealt == 0 and effectiveness == 0.0)

grounded = mon(ground_sp)  # dieselbe Spezies, aber ohne die Faehigkeit
dealt2, effectiveness2, _ = battle.calc_damage(mon(normal_sp), grounded, moves_mod.get("earthquake"))
check("Kontrolle: ohne Schwebe trifft Boden ganz normal", dealt2 > 0 and effectiveness2 > 0)

# Schwebe blockiert auch reine Boden-Status-Attacken (falls es welche gaebe) -
# hier ueber denselben Typ-Check wie use_move ihn fuer Statusattacken nutzt.
check("Schwebe wirkt auch im generischen Typ-Effektivitaets-Check fuer Status-Attacken",
      battle._type_effectiveness_for("ground", levitator) == 0.0)


# ========================================================= Wunderwache
guard = mon(water_sp, ability="wonder_guard")

dealt_neutral, eff_neutral, _ = battle.calc_damage(mon(normal_sp), guard, moves_mod.get("tackle"))
check("Wunderwache: neutrale Attacke (1x) richtet keinen Schaden an",
      dealt_neutral == 0 and eff_neutral == 0.0)

dealt_resisted, eff_resisted, _ = battle.calc_damage(mon(water_sp), guard, moves_mod.get("water_gun"))
check("Wunderwache: nicht sehr effektive Attacke (0,5x) richtet keinen Schaden an",
      dealt_resisted == 0 and eff_resisted == 0.0)

dealt_super, eff_super, _ = battle.calc_damage(mon(normal_sp), guard, moves_mod.get("thunder_shock"))
# thunder_shock ist elektrisch, unabhaengig vom Typ des Angreifers - 2x gegen Wasser.
check("Wunderwache: sehr effektive Attacke (2x) wirkt ganz normal",
      dealt_super > 0 and eff_super == 2.0)

unguarded = mon(water_sp)  # dieselbe Spezies, aber ohne die Faehigkeit
dealt_control, eff_control, _ = battle.calc_damage(mon(normal_sp), unguarded, moves_mod.get("tackle"))
check("Kontrolle: ohne Wunderwache trifft eine neutrale Attacke ganz normal",
      dealt_control > 0 and eff_control == 1.0)

attacker = mon(normal_sp)
b = battle.Battle(wild=guard, team=[attacker])
battle.use_move(b, attacker, guard, "growl")
check("Wunderwache blockiert KEINE Status-Attacken, auch wenn ihr Typ nicht sehr effektiv waere",
      guard.stat_stages.get("atk", 0) == -1)


# ==================================================================== Guts
healthy = mon(normal_sp)
burned_plain = mon(normal_sp)
burned_plain.status = "burn"
burned_guts = mon(normal_sp, ability="guts")
burned_guts.status = "burn"
check("Verbrennung halbiert Angriff normalerweise",
      battle.effective_stat(burned_plain, "atk") == battle.effective_stat(healthy, "atk") / 2)
check("Adrenalin/Guts: Verbrennung halbiert den Angriff NICHT mehr und boostet ihn um 50 %",
      battle.effective_stat(burned_guts, "atk") == battle.effective_stat(healthy, "atk") * 1.5)

paralyzed_guts = mon(normal_sp, ability="guts")
paralyzed_guts.status = "paralysis"
check("Adrenalin/Guts boostet den Angriff bei JEDEM Status, nicht nur Verbrennung",
      battle.effective_stat(paralyzed_guts, "atk") == battle.effective_stat(healthy, "atk") * 1.5)

no_status_guts = mon(normal_sp, ability="guts")
check("Adrenalin/Guts ohne Status: kein Bonus",
      battle.effective_stat(no_status_guts, "atk") == battle.effective_stat(healthy, "atk"))


# =============================================================== Shed Skin
shedder = mon(normal_sp, ability="shed_skin")
shedder.status = "poison"
b = battle.Battle(wild=mon(fire_sp), team=[shedder])
restore = with_random(random=lambda: 0.0)
try:
    battle._try_ability_cure_status(b, shedder)
    check("Haeutung/Shed Skin heilt den eigenen Status (Chance erzwungen)", shedder.status is None)
finally:
    restore()

shedder2 = mon(normal_sp, ability="shed_skin")
shedder2.status = "poison"
b2 = battle.Battle(wild=mon(fire_sp), team=[shedder2])
restore = with_random(random=lambda: 0.99)
try:
    battle._try_ability_cure_status(b2, shedder2)
    check("Haeutung/Shed Skin heilt NICHT, wenn die Chance nicht trifft", shedder2.status == "poison")
finally:
    restore()


# ============================================================ Seeding
check("Fallback-Faehigkeiten laden Zeilen (Katalog-Test)",
      abilities.seed_from_fallback() >= 0)  # bereits beim Modul-Import gelaufen; idempotent
rows = db.query("SELECT * FROM species_ability WHERE species_id = 1")
check("Fallback: Bisasam hat mindestens eine Faehigkeit (Ueberwuchs)",
      any(r["ability_key"] == "overgrow" for r in rows), str(rows))
check("Fallback: Bisasam hat eine versteckte Faehigkeit (Slot 2)",
      any(r["slot"] == 2 for r in rows), str(rows))


# ------------------------------------------------- roll_ability: kein Seed
check("roll_ability liefert None, wenn species_ability fuer die Spezies leer ist (Seed laeuft noch)",
      spawn.roll_ability(poison_sp["id"]) is None)

# ------------------------------------------------- roll_ability: Pool vorhanden
db.execute("INSERT INTO species_ability (species_id, ability_key, slot) VALUES (?,?,?)",
           (poison_sp["id"], "immunity", 0))
db.execute("INSERT INTO species_ability (species_id, ability_key, slot) VALUES (?,?,?)",
           (poison_sp["id"], "levitate", 2))
restore = with_random(random=lambda: 0.0)  # < hidden_ability_chance -> versteckte Faehigkeit
try:
    check("roll_ability waehlt bei niedrigem Wurf die versteckte Faehigkeit",
          spawn.roll_ability(poison_sp["id"]) == "levitate")
finally:
    restore()
restore = with_random(random=lambda: 0.9)  # >= hidden_ability_chance -> regulaer
try:
    check("roll_ability waehlt bei hohem Wurf eine regulaere Faehigkeit",
          spawn.roll_ability(poison_sp["id"]) == "immunity")
finally:
    restore()


# ================================================================= Migration
# Bestehende Zeile OHNE explizit gesetzte `ability`-Spalte (wie eine
# Alt-Zeile aus der Zeit vor diesem Feature) - muss klaglos NULL werden.
legacy_owned_id = db.execute(
    "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
    "current_hp, party_slot, caught_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
    ("alice", normal_sp["id"], 10, battle.exp_for_level(10, "medium_fast"), 0,
     json.dumps(IVS), "hardy", None, None, NOW.isoformat()),
)
legacy_row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (legacy_owned_id,))
check("Alt-Zeile ohne Faehigkeit: Spalte ist NULL, kein Fehler", legacy_row["ability"] is None)
legacy_combatant = battle.combatant_from_owned(legacy_row)
check("combatant_from_owned liest ability=None klaglos", legacy_combatant.ability is None)
described = team_api.describe_owned(legacy_row)
check("describe_owned liefert ability=None + ability_info=None fuer Alt-Zeilen",
      described["ability"] is None and described["ability_info"] is None)

db.init_db()
db.init_db()
cols_enc = db._table_columns(db.connect(), "encounter")
cols_owned = db._table_columns(db.connect(), "owned_pokemon")
check("Migration ist idempotent (encounter.ability, owned_pokemon.ability)",
      "ability" in cols_enc and "ability" in cols_owned)
check("Alt-Zeile bleibt nach erneuter Migration unveraendert (NULL)",
      db.query_one("SELECT ability FROM owned_pokemon WHERE id = ?", (legacy_owned_id,))["ability"] is None)
tables = {row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
check("species_ability-Tabelle existiert", "species_ability" in tables)
check("hidden_ability_chance-Setting vorhanden", db.get_setting("hidden_ability_chance") == 0.05)


# ============================================================ finalize_catch
enc_id = spawn.create_encounter(poison_sp, source_entity="test.trigger")
db.execute("UPDATE encounter SET level = 10, ivs = ?, current_hp = 5, ability = 'immunity' WHERE id = ?",
           (json.dumps(IVS), enc_id))
row = db.query_one("SELECT * FROM encounter WHERE id = ?", (enc_id,))
wild = battle.combatant_from_encounter(row)
check("combatant_from_encounter liest die Faehigkeit des Encounters",
      wild.ability == "immunity")
caught = encounters_api.finalize_catch(row, wild, "alice")
owned_row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (caught["owned_id"],))
check("finalize_catch uebernimmt die Faehigkeit des Encounters unveraendert (kein Neu-Wuerfeln)",
      owned_row["ability"] == "immunity")


# ================================================== Paket 1: Switch-in & Co.
def duel(player: battle.Combatant, foe: battle.Combatant, **kw) -> battle.Battle:
    return battle.Battle(wild=foe, team=[player], lang="en", **kw)


def forced(fn, *args):
    """Fuehrt fn mit random.random() == 0 aus (trifft immer, Chancen greifen)."""
    restore = with_random(random=lambda: 0.0)
    try:
        return fn(*args)
    finally:
        restore()


# Intimidate beim Kampfstart (start_log -> battle_start_effects).
b = duel(mon(normal_sp, ability="intimidate"), mon(fire_sp))
battle.start_log(b)
check("Bedroher: senkt beim Kampfstart den Angriff des Gegners",
      b.wild.stat_stages["atk"] == -1, str(b.log))
check("Bedroher: Kampflog nennt die Faehigkeit", any("Intimidate" in line for line in b.log))

b = duel(mon(normal_sp, ability="intimidate"), mon(fire_sp, ability="clear_body"))
battle.start_log(b)
check("Neutraltorso blockt Bedroher", b.wild.stat_stages["atk"] == 0, str(b.log))

b = duel(mon(normal_sp, ability="intimidate"), mon(fire_sp, ability="own_tempo"))
battle.start_log(b)
check("Tempomacher blockt Bedroher", b.wild.stat_stages["atk"] == 0)
forced(battle.use_move, b, b.active_mon, b.wild, "growl")
check("Tempomacher blockt NICHT Heuler (nur Bedroher)", b.wild.stat_stages["atk"] == -1)

b = duel(mon(normal_sp, ability="intimidate"), mon(fire_sp, ability="guard_dog"))
battle.start_log(b)
check("Wachhund: Bedroher erhoeht den Angriff stattdessen", b.wild.stat_stages["atk"] == 1, str(b.log))

b = duel(mon(normal_sp, ability="intimidate"), mon(fire_sp, ability="rattled"))
battle.start_log(b)
check("Hasenfuss: Bedroher senkt Angriff UND erhoeht Initiative",
      b.wild.stat_stages["atk"] == -1 and b.wild.stat_stages["spe"] == 1)

# Wechsel: der neu eingewechselte Bedroher wirkt.
b = battle.Battle(wild=mon(fire_sp), team=[mon(normal_sp), mon(normal_sp, ability="intimidate")], lang="en")
battle.switch_to(b, 1)
check("Bedroher wirkt auch beim Einwechseln", b.wild.stat_stages["atk"] == -1)

# Trainer schickt einen Bedroher als naechstes Pokemon.
foe1, foe2 = mon(fire_sp), mon(fire_sp, ability="intimidate")
foe1.trainer = foe2.trainer = "Testo"
b = duel(mon(normal_sp), foe1, bench=[foe2])
b.wild.current_hp = 0
battle._check_faints(b)
check("Bedroher wirkt, wenn der Trainer das naechste Pokemon schickt",
      b.wild is foe2 and b.active_mon.stat_stages["atk"] == -1)

# Download: der Gegner hat Verteidigung 80 < Sp.-Verteidigung 160.
bulky = mon(normal_sp)
bulky.stat_stages["spd"] = 2
b = duel(mon(normal_sp, ability="download"), bulky)
battle.start_log(b)
check("Download: erhoeht Angriff bei schwacher Verteidigung des Gegners",
      b.active_mon.stat_stages["atk"] == 1 and b.active_mon.stat_stages["spa"] == 0)

# ================================================ Paket 1: Wertsenkungen
b = duel(mon(normal_sp), mon(fire_sp, ability="hyper_cutter"))
forced(battle.use_move, b, b.active_mon, b.wild, "growl")
forced(battle.use_move, b, b.active_mon, b.wild, "leer")
check("Scherenmacht blockt nur Angriffs-Senkungen",
      b.wild.stat_stages["atk"] == 0 and b.wild.stat_stages["def"] == -1)

b = duel(mon(normal_sp), mon(fire_sp, ability="clear_body"))
battle.apply_stat_change(b, b.wild, "atk", -1, source=b.wild)
check("Neutraltorso blockt keine selbst verursachte Senkung", b.wild.stat_stages["atk"] == -1)

b = duel(mon(normal_sp), mon(fire_sp, ability="defiant"))
forced(battle.use_move, b, b.active_mon, b.wild, "leer")
check("Siegeswille: Senkung durch den Gegner -> Angriff +2",
      b.wild.stat_stages["def"] == -1 and b.wild.stat_stages["atk"] == 2, str(b.log))

focused = mon(normal_sp, ability="inner_focus")
b = duel(mon(normal_sp), focused)
check("Konzentrator: kein Zurueckschrecken",
      battle.emit(battle.Event.BEFORE_FLINCH, focused, battle=b).cancelled)
forced(battle.use_move, b, b.active_mon, focused, "headbutt")
check("Konzentrator: Kopfnuss laesst es nicht zurueckschrecken", focused.flinched is False)

# ===================================================== Paket 1: Absorbieren
sponge = mon(normal_sp, ability="water_absorb", hp_fraction=0.5)
before = sponge.current_hp
b = duel(mon(water_sp), sponge)
forced(battle.use_move, b, b.active_mon, sponge, "water_gun")
check("H2O-Absorber: Wasser-Attacke heilt 1/4 der max. KP",
      sponge.current_hp == before + int(sponge.max_hp * 0.25), f"{before} -> {sponge.current_hp}")
check("H2O-Absorber: kein 'keine Wirkung'-Log, sondern Absorbieren",
      any("absorbs" in line for line in b.log) and not any("no effect" in line for line in b.log))
check("H2O-Absorber: KI erwartet 0 Schaden mit Wasser-Attacken",
      battle.expected_damage(mon(water_sp), mon(normal_sp, ability="water_absorb"), "water_gun") == 0)

volt = mon(normal_sp, ability="volt_absorb")
b = duel(mon(normal_sp), volt)
forced(battle.use_move, b, b.active_mon, volt, "thunder_wave")
check("Volt-Absorber: Donnerwelle (Status-Attacke) wird absorbiert, keine Paralyse",
      volt.status is None and any("absorbs" in line for line in b.log))

sipper = mon(normal_sp, ability="sap_sipper")
b = duel(mon(normal_sp), sipper)
forced(battle.use_move, b, b.active_mon, sipper, "vine_whip")
check("Vegetarier: Pflanzen-Attacke erhoeht Angriff statt Schaden",
      sipper.stat_stages["atk"] == 1 and sipper.current_hp == sipper.max_hp)

fire_user = mon(fire_sp, ability="flash_fire")
b = battle.Battle(wild=mon(fire_sp), team=[fire_user, mon(normal_sp)], lang="en")
plain_dmg = battle.expected_damage(fire_user, mon(normal_sp), "ember")
forced(battle.use_move, b, b.wild, fire_user, "ember")
check("Feuerfaenger: Feuer-Attacke absorbiert, Bonus aktiv",
      fire_user.current_hp == fire_user.max_hp and fire_user.volatile.get("flash_fire") is True)
boosted_dmg = battle.expected_damage(fire_user, mon(normal_sp), "ember")
check("Feuerfaenger: eigene Feuer-Attacken danach x1,5",
      abs(boosted_dmg / plain_dmg - 1.5) < 0.02, f"{plain_dmg:.1f} -> {boosted_dmg:.1f}")
battle.switch_to(b, 1)
check("Feuerfaenger: Bonus endet beim Auswechseln", fire_user.volatile == {})

dry_dmg = battle.expected_damage(mon(fire_sp, level=50), mon(normal_sp, level=50, ability="dry_skin"), "ember")
norm_dmg = battle.expected_damage(mon(fire_sp, level=50), mon(normal_sp, level=50), "ember")
check("Trockenheit: Feuer-Attacken richten x1,25 Schaden an",
      abs(dry_dmg / norm_dmg - 1.25) < 0.02, f"{norm_dmg:.1f} -> {dry_dmg:.1f}")

# ============================================================ Paket 1: Robustheit
tank = mon(normal_sp, level=5, ability="sturdy")
b = duel(mon(normal_sp, level=100), tank)
forced(battle.use_move, b, b.active_mon, tank, "headbutt")
check("Robustheit: uebersteht bei vollen KP einen K.O.-Treffer mit 1 KP",
      tank.current_hp == 1, str(b.log[-3:]))
check("Robustheit: Kampflog nennt die Faehigkeit", any("Sturdy" in line for line in b.log))
forced(battle.use_move, b, b.active_mon, tank, "headbutt")
check("Robustheit: ohne volle KP faellt es", tank.fainted)

# ============================================================== Speichern
from app import trainer_battle  # noqa: E402

ff = mon(fire_sp, ability="flash_fire")
ff.volatile["flash_fire"] = True
copy = mon(fire_sp, ability="flash_fire")
trainer_battle.apply_mon_state(copy, json.loads(json.dumps(trainer_battle.mon_state(ff))))
check("Trainer-/PvP-Session speichert den Faehigkeits-Zustand (volatile)",
      copy.volatile == {"flash_fire": True})

from app import battle_session  # noqa: E402

own_ff = mon(fire_sp, ability="flash_fire")
own_ff.owned_id = 4242
own_ff.volatile["flash_fire"] = True
state = battle_session._to_state(battle.Battle(wild=ff, team=[own_ff], lang="en"))
check("Wildkampf-Session speichert den Faehigkeits-Zustand (Team + wildes Pokemon)",
      state["volatiles"] == {"4242": {"flash_fire": True}}
      and state["wild"]["volatile"] == {"flash_fire": True})


# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
