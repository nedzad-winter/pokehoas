"""Rauchtest der Kernlogik ohne laufendes Home Assistant.

Aufruf:  python3 tests/test_core.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, db, seed, spawn  # noqa: E402
from app.api import encounters as encounters_api  # noqa: E402
from app.api import users as users_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


# ---------------------------------------------------------------- Setup
db.init_db()
loaded = seed.seed_from_fallback()
check("Fallback-Pokédex geladen", loaded > 0, f"{loaded} Arten")
check("Default-Settings gesetzt", db.get_setting("retention_days") == 7)

# -------------------------------------------------------------- Multi-User
# Zwei Haushaltsmitglieder, wie sie beim Oeffnen der Web-App ueber die
# Ingress-Header automatisch angelegt wuerden (siehe app/auth.py).
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")
check("Nutzer werden angelegt", alice["id"] == "alice" and bob["id"] == "bob")
check("Erneutes Upsert aendert die ID nicht", db.upsert_user("alice", "Alice W.")["id"] == "alice")

# ------------------------------------------------------------ Spawn-Engine
pika = db.query_one("SELECT * FROM species WHERE name = 'pikachu'")
karpador = db.query_one("SELECT * FROM species WHERE name = 'magikarp'")
check("Spezies-Lookup", pika is not None and karpador is not None)

# Seltenheit: Karpador (catch_rate 255) muss haeufiger sein als Pikachu (190).
check(
    "Seltenheit aus catch_rate",
    spawn._rarity_weight(karpador) > spawn._rarity_weight(pika),
)

# Elektro-Bonus eines Lichtschalters muss Elektro-Typen bevorzugen.
picks = [spawn.pick_species(type_weights={"electric": 50.0})["type1"] for _ in range(300)]
electric_share = picks.count("electric") / len(picks)
check("Elektro-Bonus wirkt", electric_share > 0.25, f"{electric_share:.0%} Elektro")

# Ohne Bonus darf Elektro NICHT dominieren.
plain = [spawn.pick_species()["type1"] for _ in range(300)]
check(
    "Ohne Bonus kein Elektro-Uebergewicht",
    plain.count("electric") / len(plain) < electric_share,
)

# Wasser-Biom muss Wasser-Typen stark bevorzugen.
water = [spawn.pick_species(biome="lake")["type1"] for _ in range(300)]
check("Wasser-Biom wirkt", water.count("water") / len(water) > 0.5,
      f"{water.count('water') / len(water):.0%} Wasser")

# Vorschlagswerte fuer neue Entities.
hints = spawn.default_type_weights_for("light.wohnzimmer")
check("Lichtschalter schlaegt Elektro-Bonus vor", hints.get("electric", 0) > 1)

# ------------------------------------------------------- Event-Verarbeitung
db.execute(
    "INSERT INTO trigger_entity (entity_id, friendly_name, enabled, trigger_states, "
    "spawn_chance, type_weights, cooldown_minutes) VALUES (?,?,?,?,?,?,?)",
    ("binary_sensor.flur_bewegung", "Flur Bewegung", 1, '["on"]', 1.0, "{}", 0),
)
db.set_setting("global_cooldown_minutes", 0)

now = datetime.now(timezone.utc)
events = [
    {"entity_id": "binary_sensor.flur_bewegung", "state": "on", "last_changed": now},
    {"entity_id": "binary_sensor.flur_bewegung", "state": "off", "last_changed": now},
    {"entity_id": "binary_sensor.unbekannt", "state": "on", "last_changed": now},
]
triggers = db.query("SELECT * FROM trigger_entity WHERE enabled = 1")
created = spawn.process_events(events, triggers, now=now)
# Die Warteschlange ist pro Nutzer: EIN passendes Event legt je Nutzer eine
# eigene Zeile mit eigenem Spezies-Wurf an (hier Alice und Bob).
check("Nur passender Zustand loest aus", created == 2, f"{created} Encounter")
owners = [row["user_id"] for row in db.query("SELECT user_id FROM encounter ORDER BY id")]
check("Ein Event spawnt je Nutzer einen eigenen Encounter",
      owners == ["alice", "bob"], str(owners))

# "Gesehen" zaehlt nur beim Besitzer der Warteschlange: die Summe seiner
# seen_count entspricht genau der Zahl seiner eigenen Encounter.
for uid in ("alice", "bob"):
    seen = db.query_one(
        "SELECT COALESCE(SUM(seen_count), 0) AS n FROM dex_entry WHERE user_id = ?", (uid,)
    )["n"]
    mine = db.query_one(
        "SELECT COUNT(*) AS n FROM encounter WHERE user_id = ?", (uid,)
    )["n"]
    check(f"Neuer Spawn zaehlt als 'gesehen' nur beim Besitzer ({uid})", seen == mine,
          f"{seen} gesehen / {mine} Encounter")

# Anzeigename des Ausloesers statt der Entity-ID (siehe app/spawn.py).
check("Quelle wird mit friendly_name angezeigt",
      spawn.source_label("binary_sensor.flur_bewegung") == "Flur Bewegung")
check("Unbekannte Quelle faellt auf die Entity-ID zurueck",
      spawn.source_label("binary_sensor.unbekannt") == "binary_sensor.unbekannt")
check("Ohne Quelle kein Anzeigename", spawn.source_label(None) is None)

# Cooldown blockiert den naechsten Spawn (zaehlt pro Nutzer).
db.set_setting("global_cooldown_minutes", 30)
created2 = spawn.process_events(events, db.query("SELECT * FROM trigger_entity"), now=now)
check("Globaler Cooldown blockiert", created2 == 0)

# Tageslimit, ebenfalls pro Nutzer: jeder hat schon einen Encounter von heute.
db.set_setting("global_cooldown_minutes", 0)
db.set_setting("max_spawns_per_day", 1)
created3 = spawn.process_events(events, db.query("SELECT * FROM trigger_entity"), now=now)
check("Tageslimit blockiert", created3 == 0)
db.set_setting("max_spawns_per_day", 15)

# ------------------------------------------------------------- Retention
db.execute(
    "UPDATE encounter SET expires_at = ? WHERE id = 1",
    ((now - timedelta(days=1)).isoformat(),),
)
fled = spawn.expire_stale_encounters(now)
check("Retention laesst abgelaufene Encounter fliehen", fled == 1)
status = db.query_one("SELECT status FROM encounter WHERE id = 1")["status"]
check("Status ist 'fled'", status == "fled")

# ----------------------------------------------------------------- Kampf
wild = battle.Combatant(species=karpador, level=10, ivs={k: 15 for k in spawn.STAT_KEYS})
mine = battle.Combatant(species=pika, level=12, ivs={k: 15 for k in spawn.STAT_KEYS})
check("KP-Berechnung plausibel", 20 < wild.max_hp < 60, f"{wild.max_hp} KP")

dealt, eff, _crit = battle.damage(mine, wild, move_power=60, move_type="electric")
check("Elektro gegen Wasser ist sehr effektiv", eff == 2.0)
check("Schaden > 0", dealt > 0, f"{dealt}")

eff_ground = battle.type_effectiveness("electric", ["ground"])
check("Elektro gegen Boden wirkungslos", eff_ground == 0.0)

# ----------------------------------------------------------------- Fangen
full = battle.catch_probability(wild, "poke_ball")
wild.current_hp = 1
weak = battle.catch_probability(wild, "poke_ball")
check("Fangchance steigt mit sinkenden KP", weak > full, f"{full:.2f} -> {weak:.2f}")

better = battle.catch_probability(wild, "ultra_ball")
check("Besserer Ball = hoehere Chance", better >= weak)
master = battle.catch_probability(wild, "master_ball")
check("Meisterball faengt immer", master == 1.0)

# Wer im Kampf gelevelt hat, ist nicht mehr fangbar (User-Bezug nur fuer den
# kritischen Fang relevant, siehe battle.critical_catch_chance).
wild.leveled_in_battle = True
result = battle.throw_ball(wild, "alice", "poke_ball")
check("Gelevelt im Kampf -> nicht fangbar", result.caught is False and result.reason)

# Ballverbrauch ist jetzt pro Nutzer (siehe app/api/encounters.py).
db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES ('poke_ball', 'alice', 1)")
check("Ball wird verbraucht", encounters_api.consume_ball("poke_ball", "alice") is True)
check("Leeres Inventar verweigert", encounters_api.consume_ball("poke_ball", "alice") is False)
check(
    "Bobs Inventar ist von Alice' Baellen unberuehrt",
    encounters_api.consume_ball("poke_ball", "bob") is False,
)

# ------------------------------------------------------------------- EXP
owned_id = db.execute(
    "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, caught_at) "
    "VALUES (?,?,?,?,?,?,?,?)",
    ("alice", pika["id"], 5, battle.exp_for_level(5), 0,
     json.dumps({k: 15 for k in spawn.STAT_KEYS}), "hardy", now.isoformat()),
)
owned = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))
fighter = battle.combatant_from_owned(owned)
levels = battle.apply_exp(fighter, 5000)
check("EXP fuehrt zu Level-Ups", len(levels) > 0, f"Level {levels[-1] if levels else '-'}")
check("Level wird persistiert",
      db.query_one("SELECT level FROM owned_pokemon WHERE id = ?", (owned_id,))["level"] > 5)

# --------------------------------------------------------------- Autokampf
db.execute("UPDATE owned_pokemon SET party_slot = 1 WHERE id = ?", (owned_id,))
encounter_id = spawn.create_encounter(karpador, source_entity="binary_sensor.flur_bewegung")
encounter = db.query_one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))
auto = battle.auto_battle(encounter, "alice")
check("Autokampf liefert Ergebnis", auto["outcome"] in ("won", "lost", "timeout"),
      auto["outcome"])
check("Autokampf protokolliert Runden", len(auto.get("log", [])) > 0)

check("Bobs Team ist leer (eigenes Team pro Nutzer)", battle.party("bob") == [])
check("Alice' Team enthaelt das eingesetzte Pokémon", len(battle.party("alice")) == 1)

# --------------------------------------------------------- Pokédex pro Nutzer
db.execute(
    """
    INSERT INTO dex_entry (species_id, user_id, seen_count, caught_count, first_seen_at, first_caught_at)
    VALUES (?, 'alice', 1, 1, ?, ?)
    ON CONFLICT(species_id, user_id) DO UPDATE SET caught_count = 1, first_caught_at = excluded.first_caught_at
    """,
    (karpador["id"], now.isoformat(), now.isoformat()),
)
check("Alice kennt die Art, die sie gefangen hat", battle.species_is_known(karpador["id"], "alice"))
check("Bob kennt dieselbe Art noch nicht", not battle.species_is_known(karpador["id"], "bob"))

# ------------------------------------------------------------ Starter-Wahl
check("27 Starter ueber 9 Generationen", len(users_api.STARTER_DEX_IDS) == 27)

starters = users_api.list_starters()
check(
    "Alle Starter-Generationen sind im (Fallback-)Pokédex vertreten",
    len(starters["generations"]) == 9 and not starters["seeding"],
)
check(
    "Jede Generation liefert 3 Starter",
    all(len(gen["starters"]) == 3 for gen in starters["generations"]),
)

carol = db.upsert_user("carol", "Carol")
try:
    users_api.choose_starter(users_api.StarterChoice(species_id=9999), carol)
    check("Ungueltiger Starter wird abgelehnt", False)
except HTTPException as exc:
    check("Ungueltiger Starter wird abgelehnt", exc.status_code == 400)

starter_result = users_api.choose_starter(users_api.StarterChoice(species_id=1), carol)
check("Starter-Wahl liefert das neue Pokémon", starter_result["ok"] is True)
check(
    "Starter landet auf Team-Platz 1",
    db.query_one(
        "SELECT party_slot FROM owned_pokemon WHERE user_id = 'carol'"
    )["party_slot"] == 1,
)
check(
    "Starter-Inventar wird vergeben",
    db.query_one(
        "SELECT quantity FROM inventory WHERE item_key = 'poke_ball' AND user_id = 'carol'"
    )["quantity"] == 20,
)
check(
    "Starter-Inventar enthaelt 5 Traenke",
    db.query_one(
        "SELECT quantity FROM inventory WHERE item_key = 'potion' AND user_id = 'carol'"
    )["quantity"] == 5,
)
check(
    "app_user.starter_species_id wird gesetzt",
    db.get_user("carol")["starter_species_id"] == 1,
)

try:
    # In der echten App liest die Ingress-Dependency den Nutzer bei jedem
    # Request neu aus der DB - hier also frisch nachladen statt die
    # veraltete lokale `carol`-Variable von vor der Starter-Wahl zu nutzen.
    users_api.choose_starter(users_api.StarterChoice(species_id=4), db.get_user("carol"))
    check("Zweite Starter-Wahl wird abgelehnt", False)
except HTTPException as exc:
    check("Zweite Starter-Wahl wird abgelehnt", exc.status_code == 409)

# Regression: das Einstellungs-Formular hat starter_inventory frueher als
# Zahlenfeld gespeichert (-> 0), danach brach jede Starter-Wahl mit 500 ab.
from app.api import config_routes  # noqa: E402

try:
    config_routes.patch_settings({"starter_inventory": 0})
    check("PATCH starter_inventory=0 wird abgelehnt", False)
except HTTPException as exc:
    check("PATCH starter_inventory=0 wird abgelehnt", exc.status_code == 400)
try:
    config_routes.patch_settings({"auto_battle_enabled": 1})
    check("PATCH Zahl statt Schalter wird abgelehnt", False)
except HTTPException as exc:
    check("PATCH Zahl statt Schalter wird abgelehnt", exc.status_code == 400)
config_routes.patch_settings({"retention_days": 5, "auto_battle_enabled": True})
check("Gueltiger PATCH wird gespeichert", db.get_setting("retention_days") == 5)

db.set_setting("starter_inventory", 0)
dave = db.upsert_user("dave", "dave")
users_api.choose_starter(users_api.StarterChoice(species_id=1), dave)
check(
    "Starter-Wahl klappt trotz kaputtem starter_inventory",
    db.query_one(
        "SELECT quantity FROM inventory WHERE item_key = 'poke_ball' AND user_id = 'dave'"
    )["quantity"] == 20,
)

# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
