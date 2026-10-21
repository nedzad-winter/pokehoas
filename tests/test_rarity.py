"""Seltenheits-Stufen und ihre Wirkung auf die Spawn-Gewichtung.

Aufruf:  python3 tests/test_rarity.py
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import db, rarity, seed, spawn  # noqa: E402
from app.api import admin as admin_api  # noqa: E402
from app.api import config_routes  # noqa: E402
from app.api import team as team_api  # noqa: E402
from app.api import users as users_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")


def species(name: str) -> dict:
    row = db.query_one("SELECT * FROM species WHERE name = ?", (name,))
    assert row is not None, name
    return row


def add_species(species_id: int, name: str, **overrides) -> None:
    """Der Fallback-Pokédex kennt nur Starter + Pikachu/Karpador - fuer die
    Stufen brauchen wir Entwicklungen, Pseudo-Legendaere und Legendaere."""
    row = {
        "id": species_id, "name": name, "name_en": name.title(), "name_de": name.title(),
        "type1": "normal", "type2": None, "generation": 1, "catch_rate": 45,
        "base_exp": 64, "growth_rate": "medium_fast",
        "base_hp": 60, "base_atk": 60, "base_def": 60,
        "base_spa": 60, "base_spd": 60, "base_spe": 60,
        "is_legendary": 0, "is_mythical": 0, "evolves_from": None, "sprite_url": None,
    }
    row.update(overrides)
    columns = ", ".join(row)
    db.execute(
        f"INSERT OR REPLACE INTO species ({columns}) "
        f"VALUES ({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )


# Echte Werte aus den Hauptspielen, damit die Regel nachvollziehbar bleibt.
add_species(3, "venusaur", evolves_from=1, catch_rate=45, base_hp=80, base_atk=82,
            base_def=83, base_spa=100, base_spd=100, base_spe=80)
add_species(446, "munchlax", catch_rate=45, base_hp=135, base_atk=85, base_def=40,
            base_spa=40, base_spd=85, base_spe=5)
# evolves_from=446 (Munchlax) matches real PokeAPI data: Munchlax (a baby
# Pokemon, only exists for breeding) was retroactively inserted as Snorlax's
# pre-evolution, even though Snorlax predates it by two generations.
add_species(143, "snorlax", evolves_from=446, catch_rate=25, base_hp=160, base_atk=110,
            base_def=65, base_spa=65, base_spd=110, base_spe=30)
add_species(147, "dratini", catch_rate=45, base_hp=41, base_atk=64, base_def=45,
            base_spa=50, base_spd=50, base_spe=50)
add_species(149, "dragonite", evolves_from=148, catch_rate=45, base_hp=91, base_atk=134,
            base_def=95, base_spa=100, base_spd=100, base_spe=80)
add_species(144, "articuno", catch_rate=3, is_legendary=1, base_hp=90, base_atk=85,
            base_def=100, base_spa=95, base_spd=125, base_spe=85)
add_species(151, "mew", catch_rate=45, is_mythical=1, base_hp=100, base_atk=100,
            base_def=100, base_spa=100, base_spd=100, base_spe=100)

# generation wie in den Hauptspielen (9 bzw. 7) - damit sie nicht versehentlich
# im generation-1-only Spawn-Pool auftauchen, den die Verteilungs-Checks unten
# ungefiltert abfragen (unlocked_generations() ist hier noch {1}).
add_species(987, "flutter-mane", generation=9, catch_rate=25, base_hp=55, base_atk=55,
            base_def=55, base_spa=135, base_spd=135, base_spe=135)
add_species(799, "guzzlord", generation=7, catch_rate=15, base_hp=223, base_atk=101,
            base_def=53, base_spa=97, base_spd=53, base_spe=43)


# ------------------------------------------------------------ Einstufung
check("Stufen-Reihenfolge", rarity.TIERS[0] == "common" and rarity.TIERS[-1] == "mythical")

# Basis-Form mit niedriger Basiswert-Summe -> common.
check("Karpador ist common", rarity.tier_for(species("magikarp")) == rarity.COMMON)

# Starter sind trotz Basisform + niedriger BST bewusst rare.
check("Bisasam (Starter) ist rare", rarity.tier_for(species("bulbasaur")) == rarity.RARE)
check("Starter-Liste passt zur Starter-Auswahl",
      rarity.STARTER_IDS == frozenset(users_api.STARTER_DEX_IDS))
check("Alle Starter-Basisformen sind rare",
      all(rarity.tier_for({"id": sid, "evolves_from": None, "base_hp": 40, "base_atk": 40,
                           "base_def": 40, "base_spa": 40, "base_spd": 40, "base_spe": 40})
          == rarity.RARE for sid in users_api.STARTER_DEX_IDS))

# Entwicklung -> rare, egal wie schwach.
check("Bisaflor ist rare", rarity.tier_for(species("venusaur")) == rarity.RARE)
check("Schwache Entwicklung ist trotzdem rare",
      rarity.tier_for({"id": 9999, "evolves_from": 1, "base_hp": 30, "base_atk": 30,
                       "base_def": 30, "base_spa": 30, "base_spd": 30, "base_spe": 30})
      == rarity.RARE)

# Starke Basis-Form (Basiswert-Summe > 450) -> rare, obwohl PokeAPI es als
# Entwicklung von Munchlax (Baby-Pokemon) fuehrt - siehe BABY_IDS.
snorlax = species("snorlax")
check("Relaxo ist rare", rarity.tier_for(snorlax) == rarity.RARE,
      f"BST {rarity.base_stat_total(snorlax)}")
check("Relaxo hat trotzdem Munchlax als evolves_from gespeichert",
      snorlax["evolves_from"] == 446)

# Baby-Vorstufen (Pichu, Munchlax, ...) zaehlen NICHT als "ist eine
# Entwicklung" fuer die Seltenheits-Regel - sonst waeren Arten wie Relaxo
# oder Pummeluff (echte PokeAPI-Daten: evolves_from = Munchlax/Igglybuff)
# niemals wild fangbar, weil nur Basis-Formen spawnen (siehe spawn._spawn_pool).
check("effective_evolves_from ignoriert eine Baby-Vorstufe",
      rarity.effective_evolves_from({"evolves_from": 446}) is None)
check("effective_evolves_from behaelt eine echte Entwicklung",
      rarity.effective_evolves_from({"evolves_from": 1}) == 1)
check("Schwache Art hinter einem Baby ist common, nicht rare",
      rarity.tier_for({"id": 25, "evolves_from": 172, "base_hp": 35, "base_atk": 55,
                       "base_def": 40, "base_spa": 50, "base_spd": 50, "base_spe": 90})
      == rarity.COMMON,
      "Pikachu-artige BST 320 hinter Pichu (172)")
check("Relaxo ist trotz Baby-Vorstufe im wilden Spawn-Pool",
      any(s["id"] == 143 for s in spawn._spawn_pool(None)))
check("Bisaflor (echte Entwicklung, keine Baby-Vorstufe) ist NICHT im Pool",
      all(s["id"] != 3 for s in spawn._spawn_pool(None)))

# Pseudo-Legendaere: alle Stufen der Linie.
check("Dratini ist pseudo_legendary",
      rarity.tier_for(species("dratini")) == rarity.PSEUDO_LEGENDARY)
check("Dragoran ist pseudo_legendary",
      rarity.tier_for(species("dragonite")) == rarity.PSEUDO_LEGENDARY)
check("Alle Pseudo-Linien haben 3 Stufen", len(rarity.PSEUDO_LEGENDARY_IDS) == 30)

# Paradox-Pokemon: feste Dex-Nummern, unabhaengig von Basiswerten.
check("Flutter Mane ist paradox",
      rarity.tier_for(species("flutter-mane")) == rarity.PARADOX)
check("Alle Paradox-Pokemon sind erfasst", len(rarity.PARADOX_IDS) == 20)

# Legendaer/Mysterioes kommen aus den Flags des Seedings, Ultrabestien zaehlen
# laut Vorgabe zusaetzlich als legendary.
check("Arktos ist legendary", rarity.tier_for(species("articuno")) == rarity.LEGENDARY)
check("Guzzlord (Ultrabestie) ist legendary",
      rarity.tier_for(species("guzzlord")) == rarity.LEGENDARY)
check("Alle Ultrabestien sind erfasst", len(rarity.ULTRA_BEAST_IDS) == 11)
check("Mew ist mythical", rarity.tier_for(species("mew")) == rarity.MYTHICAL)
check("mythical schlaegt legendary",
      rarity.tier_for({"id": 1, "is_legendary": 1, "is_mythical": 1}) == rarity.MYTHICAL)

# Deterministisch: gleiche Zeile -> gleiche Stufe.
check("Einstufung ist deterministisch",
      all(rarity.tier_for(species("dratini")) == rarity.PSEUDO_LEGENDARY for _ in range(5)))

# ---------------------------------------------------- Box-Sperre (locked)
# Nur legendaer/mystisch kommen automatisch gesperrt an (siehe api/team.py
# `_release`, db._migrate_locked) - alle anderen Stufen bleiben entsperrt.
check("Arktos (legendary) kommt automatisch gesperrt an",
      rarity.auto_lock(species("articuno")))
check("Mew (mythical) kommt automatisch gesperrt an",
      rarity.auto_lock(species("mew")))
check("Dragoran (pseudo_legendary) kommt NICHT automatisch gesperrt an",
      not rarity.auto_lock(species("dragonite")))
check("Flutter Mane (paradox) kommt NICHT automatisch gesperrt an",
      not rarity.auto_lock(species("flutter-mane")))
check("Karpador (common) kommt NICHT automatisch gesperrt an",
      not rarity.auto_lock(species("magikarp")))


# ------------------------------------------------------------ Gewichtung
multipliers = rarity.tier_multipliers()
check("Default-Multiplikatoren", multipliers["common"] == 83.0
      and multipliers["rare"] == 10.0 and multipliers["pseudo_legendary"] == 3.0
      and multipliers["paradox"] == 2.5 and multipliers["legendary"] == 1.0
      and multipliers["mythical"] == 0.5)

weights = {name: spawn._rarity_weight(species(name), multipliers)
           for name in ("magikarp", "venusaur", "dratini", "flutter-mane", "articuno", "mew")}
check("common > rare", weights["magikarp"] > weights["venusaur"])
check("rare > pseudo_legendary", weights["venusaur"] > weights["dratini"])
check("pseudo_legendary > paradox", weights["dratini"] > weights["flutter-mane"])
check("paradox > legendary", weights["flutter-mane"] > weights["articuno"])
check("legendary > mythical", weights["articuno"] > weights["mew"])

# Fangrate staffelt innerhalb derselben Stufe weiter.
check("Fangrate wirkt innerhalb der Stufe",
      spawn._rarity_weight(species("magikarp"), multipliers)
      > spawn._rarity_weight(species("bulbasaur"), multipliers))

# Settings wirken wirklich: rare auf 0 -> Gewicht faellt auf den Mindestwert.
db.set_setting("rarity_weight_rare", 0.0)
check("Multiplikator 0 druckt das Gewicht auf den Mindestwert",
      spawn._rarity_weight(species("venusaur"), rarity.tier_multipliers()) <= 1e-6)
db.set_setting("rarity_weight_rare", db.DEFAULT_SETTINGS["rarity_weight_rare"])

# Im echten Pool (nur Basis-Formen): haeufige Arten dominieren, Legendaere
# und Mysterioese sind die Ausnahme.
# Sampling is random: fixed seed and a large sample keep this from flaking.
random.seed(1234)
N_PICKS = 3000
picks = [spawn.pick_species() for _ in range(N_PICKS)]
tiers = [rarity.tier_for(p) for p in picks]
common_share = tiers.count(rarity.COMMON) / len(tiers)
special = sum(1 for t in tiers if t in (rarity.LEGENDARY, rarity.MYTHICAL))
check("Haeufige Arten dominieren den Pool", common_share > 0.78, f"{common_share:.0%} common")
# The tier weights are tier shares: with every present tier normalised, legendary
# + mythical make up 1.5 / 97.5 (about 1.5 %) of this small generation-1 pool.
check("Legendaere/Mysterioese fast nie", special <= N_PICKS * 0.025, f"{special} von {N_PICKS}")
check("Pseudo-Legendaere seltener als haeufige",
      tiers.count(rarity.PSEUDO_LEGENDARY) < tiers.count(rarity.COMMON))


# ------------------------------------------------- Tier-Anteile (Normalisierung)
# The tier weights are SHARES of all spawns, independent of how many species a
# tier has. Build a pool with realistic tier sizes (many common, few
# pseudo-legendary) and check 10000 spawns against the configured weights.
SIM_TIER_SIZES = {"common": 300, "rare": 90, "legendary": 70, "mythical": 20}
sim_ids: list[int] = []
next_id = 20000
for tier, count in SIM_TIER_SIZES.items():
    for i in range(count):
        overrides = {"generation": 1, "catch_rate": 3 + (i * 7) % 253}
        if tier == "common":
            overrides.update(base_hp=50, base_atk=50, base_def=50, base_spa=50, base_spd=50, base_spe=50)
        elif tier == "rare":
            overrides.update(base_hp=80, base_atk=80, base_def=80, base_spa=80, base_spd=80, base_spe=80)
        elif tier == "legendary":
            overrides["is_legendary"] = 1
        else:
            overrides["is_mythical"] = 1
        add_species(next_id, f"sim{next_id}", **overrides)
        sim_ids.append(next_id)
        next_id += 1
for dex_id in sorted(rarity.PSEUDO_LEGENDARY_IDS | rarity.PARADOX_IDS):
    if db.query_one("SELECT 1 FROM species WHERE id = ?", (dex_id,)) is None:
        add_species(dex_id, f"sim{dex_id}", generation=1, catch_rate=3 + dex_id % 200)
        sim_ids.append(dex_id)

sim_pool = [s for s in db.query("SELECT * FROM species WHERE evolves_from IS NULL")
            if s["generation"] == 1]
sim_counts = {t: sum(1 for s in sim_pool if rarity.tier_for(s) == t) for t in rarity.TIERS}
check("Simulations-Pool hat alle sechs Stufen in unterschiedlicher Groesse",
      all(sim_counts[t] > 0 for t in rarity.TIERS) and sim_counts["common"] > 5 * sim_counts["pseudo_legendary"],
      str(sim_counts))

# Deterministic: the normalised weights of a tier add up to its multiplier
# (catch-rate factor only redistributes WITHIN the tier).
sim_multipliers = rarity.tier_multipliers()
norms = spawn._tier_norms(sim_pool)
for tier in rarity.TIERS:
    total = sum(spawn._rarity_weight(s, sim_multipliers, 1.0, norms)
                for s in sim_pool if rarity.tier_for(s) == tier)
    check(f"Normalisierte Gewichte summieren sich zum Stufen-Gewicht: {tier}",
          abs(total - sim_multipliers[tier]) < 1e-9, f"{total:.6f} vs {sim_multipliers[tier]}")

N_SIM = 10000
custom_weights = {"common": 1.0, "rare": 0.35, "pseudo_legendary": 0.06,
                  "paradox": 2.5, "legendary": 0.01, "mythical": 0.005}

# custom_weights leaves paradox at its default (2.5) while every other tier
# drops far below - exactly why it ends up with ~64 % of all spawns below.
# rarity.out_of_order_tiers flags this before saving (admin settings preview).
default_weights = {tier: db.DEFAULT_SETTINGS[rarity.setting_key(tier)] for tier in rarity.TIERS}
check("Standard-Gewichte sind nicht ausser der Reihe", rarity.out_of_order_tiers(default_weights) == [])
check("Paradox ausser der Reihe bei den angepassten Gewichten",
      rarity.out_of_order_tiers(custom_weights) == ["paradox"])
check("Mehrere Stufen ausser der Reihe werden alle erkannt",
      rarity.out_of_order_tiers({"common": 1.0, "rare": 0.35, "pseudo_legendary": 0.06,
                                  "paradox": 2.5, "legendary": 3.0, "mythical": 0.005})
      == ["paradox", "legendary"])

for label, weights in (("Standard-Gewichte", {}), ("angepasste Gewichte", custom_weights)):
    for tier in rarity.TIERS:
        db.set_setting(rarity.setting_key(tier),
                       weights.get(tier, db.DEFAULT_SETTINGS[rarity.setting_key(tier)]))
    active = rarity.tier_multipliers()
    random.seed(20240607)
    counts = {t: 0 for t in rarity.TIERS}
    for _ in range(N_SIM):
        counts[rarity.tier_for(spawn.pick_species())] += 1
    total_weight = sum(active.values())
    for tier in rarity.TIERS:
        expected = active[tier] / total_weight
        observed = counts[tier] / N_SIM
        sigma = (expected * (1 - expected) / N_SIM) ** 0.5
        check(f"{N_SIM} Spawns, {label}: {tier} {observed:.2%} (erwartet {expected:.2%})",
              abs(observed - expected) <= 4 * sigma + 1e-9, f"{counts[tier]} Spawns")
for tier in rarity.TIERS:
    db.set_setting(rarity.setting_key(tier), db.DEFAULT_SETTINGS[rarity.setting_key(tier)])
for sim_id in sim_ids:
    db.execute("DELETE FROM species WHERE id = ?", (sim_id,))


# ------------------------------------------- Spawn shares shown in the profile
# spawn.tier_shares (behind profile["spawn_rates"]) uses the same weighting as
# pick_species: shares sum to 100, follow the configured weights and shift with
# the user's league progress.
from app import achievements, profiles  # noqa: E402

def shares_by_tier(user_id):
    return {row["tier"]: row for row in spawn.tier_shares(user_id)["tiers"]}

plain_shares = shares_by_tier(alice["id"])
check("Spawn-Anteile summieren sich auf 100 %",
      abs(sum(r["percent"] for r in plain_shares.values()) - 100) < 0.01)
check("Ohne besiegte Liga: Anteil == Anteil ohne Bonus",
      all(abs(r["percent"] - r["base_percent"]) < 1e-6 for r in plain_shares.values()))
check("Stufe ohne Spezies im Pool ist nicht verfuegbar (Paradox, Gen 9 gesperrt)",
      plain_shares["paradox"]["in_pool"] is False and plain_shares["paradox"]["percent"] == 0)

_real_regions = achievements.regions_cleared
achievements.regions_cleared = lambda user_id: ["kanto", "johto", "hoenn"]
boosted_shares = shares_by_tier(alice["id"])
achievements.regions_cleared = _real_regions
check("Besiegte Ligen erhoehen den Anteil von mysterioes",
      boosted_shares["mythical"]["percent"] > boosted_shares["mythical"]["base_percent"])
check("Besiegte Ligen senken den Anteil von haeufig",
      boosted_shares["common"]["percent"] < boosted_shares["common"]["base_percent"])
check("Basis-Anteile bleiben vom Bonus unberuehrt",
      all(abs(boosted_shares[t]["base_percent"] - plain_shares[t]["percent"]) < 1e-6
          for t in rarity.TIERS))

prof_rates = profiles.build(alice)["spawn_rates"]
check("Profil liefert spawn_rates je Stufe",
      [r["tier"] for r in prof_rates["tiers"]] == list(rarity.TIERS)
      and prof_rates["total_regions"] >= 1)


# ------------------------------------------------------------ API
entry = team_api.get_pokedex_entry(species("dratini")["id"], current_user=alice)
check("Pokédex-Detail liefert rarity_tier",
      entry["species"]["rarity_tier"] == rarity.PSEUDO_LEGENDARY)

dex = team_api.get_pokedex(current_user=alice)
check("Pokédex-Liste liefert rarity_tier",
      all(row.get("rarity_tier") in rarity.TIERS for row in dex["entries"]))


def patch(payload: dict) -> str | None:
    try:
        config_routes.patch_settings(payload)
        return None
    except HTTPException as exc:
        return str(exc.detail)


check("Negatives Gewicht wird abgelehnt", patch({"rarity_weight_common": -1}) is not None)
check("Absurd hohes Gewicht wird abgelehnt", patch({"rarity_weight_rare": 1000}) is not None)
check("Gueltiges Gewicht wird gespeichert",
      patch({"rarity_weight_rare": 0.5}) is None and db.get_setting("rarity_weight_rare") == 0.5)
check(
    "Alle Stufen auf 0 wird abgelehnt",
    patch({key: 0.0 for key in (rarity.setting_key(t) for t in rarity.TIERS)}) is not None,
)

# Der frueher separate Liga-Bonus-Faktor ist durch die stufenspezifische
# Kurve in rarity.REGION_FULL_CLEAR_TARGET ersetzt - kein eigenes Setting mehr.
check("Alter Liga-Bonus-Schluessel wird nicht mehr akzeptiert",
      patch({"rarity_region_bonus_per_league": 0.25}) is not None)

# ------------------------------------------------ Maxed-IV-Spawn-Gewicht
check("maxed_ivs_spawn_weight_factor < 0 wird abgelehnt",
      patch({"maxed_ivs_spawn_weight_factor": -0.1}) is not None)
check("maxed_ivs_spawn_weight_factor > 1 wird abgelehnt",
      patch({"maxed_ivs_spawn_weight_factor": 1.5}) is not None)
check("maxed_ivs_spawn_weight_factor 0 ist gueltig (nie mehr spawnen)",
      patch({"maxed_ivs_spawn_weight_factor": 0.0}) is None
      and db.get_setting("maxed_ivs_spawn_weight_factor") == 0.0)
db.set_setting("maxed_ivs_spawn_weight_factor",
                db.DEFAULT_SETTINGS["maxed_ivs_spawn_weight_factor"])

maxed_species = {**species("magikarp"), "min_ivs": {k: 31 for k in spawn.STAT_KEYS}}
check("Maximierte Art wird im Pool heruntergewichtet",
      spawn._rarity_weight(maxed_species, multipliers)
      < spawn._rarity_weight(species("magikarp"), multipliers))


# ------------------------------------------- Migration alter Gewichte
def rarity_settings() -> dict[str, float]:
    return {tier: db.get_setting(rarity.setting_key(tier)) for tier in rarity.TIERS}


def run_rarity_migration() -> None:
    conn = db.connect()
    with db._lock:
        db._migrate_rarity_weight_scale(conn)


for key, value in db._LEGACY_RARITY_WEIGHTS.items():
    db.set_setting(key, value)
db.set_setting("rarity_weight_paradox", 2.5)
run_rarity_migration()
new_defaults = {tier: db.DEFAULT_SETTINGS[rarity.setting_key(tier)] for tier in rarity.TIERS}
check("Alte Standard-Gewichte werden auf die neue Anteil-Skala gehoben",
      rarity_settings() == new_defaults, str(rarity_settings()))
run_rarity_migration()
check("Migration ist idempotent", rarity_settings() == new_defaults)

for key, value in db._LEGACY_RARITY_WEIGHTS.items():
    db.set_setting(key, value)
db.set_setting("rarity_weight_common", 2.0)
run_rarity_migration()
check("Vom Admin angepasste alte Gewichte bleiben unangetastet",
      db.get_setting("rarity_weight_common") == 2.0
      and db.get_setting("rarity_weight_mythical") == 0.005)
for tier, value in new_defaults.items():
    db.set_setting(rarity.setting_key(tier), value)


# ------------------------------------------------------------ i18n
import json  # noqa: E402

for lang in ("en", "de"):
    data = json.loads((ROOT / "pokemon_encounter/app/data/i18n" / f"{lang}.json")
                      .read_text(encoding="utf-8"))
    missing = [key for tier in rarity.TIERS
               for key in (f"rarity.{tier}", f"settings.{rarity.setting_key(tier)}")
               if key not in data]
    check(f"i18n {lang} vollstaendig", not missing, ", ".join(missing))


# ------------------------------------------------------- Box-Sperre (API)
# Legendaer/mystisch per admin.give_pokemon vergeben -> owned_pokemon.locked
# ist gesetzt, release() lehnt ab, bis der Spieler per set_locked entsperrt.
def owned(owned_id: int) -> dict:
    return db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))


legendary_gift = admin_api.give_pokemon(
    "alice", admin_api.GiveIn(species_id=144, level=50, placement="box"),
)
legendary_id = legendary_gift["owned_id"]
check("Legendaer verschenkt kommt gesperrt in der DB an",
      bool(owned(legendary_id)["locked"]))

common_gift = admin_api.give_pokemon(
    "alice", admin_api.GiveIn(species_id=1, level=5, placement="box"),
)
common_id = common_gift["owned_id"]
check("Nicht-legendaer verschenkt kommt entsperrt an",
      not owned(common_id)["locked"])

try:
    team_api.release(legendary_id, alice)
    check("Gesperrtes Pokémon freilassen -> Exception erwartet", False)
except HTTPException as exc:
    check("Gesperrtes Pokémon freilassen -> 400", exc.status_code == 400, exc.detail)
check("Gesperrtes Pokémon bleibt in der Box", owned(legendary_id) is not None)

unlocked = team_api.set_locked(legendary_id, team_api.LockedChange(locked=False), alice)
check("set_locked(False) hebt die Sperre auf", unlocked["locked"] is False, str(unlocked))
freed = team_api.release(legendary_id, alice)
check("Entsperrtes Pokémon laesst sich freilassen", owned(legendary_id) is None, str(freed))

relocked_gift = admin_api.give_pokemon(
    "alice", admin_api.GiveIn(species_id=151, level=10, placement="box"),
)
relocked_id = relocked_gift["owned_id"]
locked_again = team_api.set_locked(relocked_id, team_api.LockedChange(locked=True), alice)
check("set_locked(True) sperrt ein bereits entsperrtes (hier: automatisch gesperrtes) Pokémon weiter",
      locked_again["locked"] is True, str(locked_again))

try:
    team_api.release_many(team_api.ReleaseIn(ids=[relocked_id, common_id]), alice)
    check("Mehrfach-Freilassen mit gesperrtem Pokémon in der Auswahl -> Exception erwartet", False)
except HTTPException as exc:
    check("Mehrfach-Freilassen mit gesperrtem Pokémon -> 400 (alles oder nichts)",
          exc.status_code == 400, exc.detail)
check("Mehrfach-Freilassen schlaegt fehl -> auch das entsperrte Pokémon bleibt",
      owned(common_id) is not None)

# Bulk lock/unlock from the box selection (POST /api/team/lock-many).
bulk = team_api.set_locked_many(team_api.LockManyIn(ids=[common_id, relocked_id, common_id], locked=True), alice)
check("lock-many sperrt nur die noch entsperrten (Duplikate ignoriert)",
      bulk["changed"] == 1 and bulk["ids"] == [common_id], str(bulk))
check("lock-many: beide gesperrt", owned(common_id)["locked"] and owned(relocked_id)["locked"])
bulk = team_api.set_locked_many(team_api.LockManyIn(ids=[common_id, relocked_id], locked=False), alice)
check("unlock-many entsperrt beide", bulk["changed"] == 2
      and not owned(common_id)["locked"] and not owned(relocked_id)["locked"], str(bulk))
try:
    team_api.set_locked_many(team_api.LockManyIn(ids=[], locked=True), alice)
    check("lock-many ohne Auswahl -> Exception erwartet", False)
except HTTPException as exc:
    check("lock-many ohne Auswahl -> 400", exc.status_code == 400, exc.detail)
bob = db.upsert_user("bob", "Bob")
try:
    team_api.set_locked_many(team_api.LockManyIn(ids=[common_id], locked=True), bob)
    check("lock-many auf fremdes Pokémon -> Exception erwartet", False)
except HTTPException as exc:
    check("lock-many auf fremdes Pokémon -> 403", exc.status_code == 403, exc.detail)
check("fremdes lock-many aendert nichts", not owned(common_id)["locked"])

print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
