"""Tests fuer die Living-Dex-Sperre: wie viele Pokémon einer Evolutionslinie
noch fuer den Living Dex fehlen (evolution.living_dex_need), die persoenliche
Option `auto_lock_living_dex` beim Fang und die Auto-Regel-Teile (Bedingung
`living_dex_needed`, Aktion `lock`, Vorlage).

Aufruf:  python3 tests/test_living_dex_lock.py
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

TMP = tempfile.mkdtemp(prefix="pokemon-living-dex-lock-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from pydantic import ValidationError  # noqa: E402

from app import auto_rules, battle, db, evolution, seed, spawn  # noqa: E402
from app.api import encounters as encounters_api  # noqa: E402
from app.api import users as users_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


random.seed(int(os.environ.get("TEST_SEED", "1234")))
NOW = datetime.now(timezone.utc).isoformat()
IVS = {k: 10 for k in spawn.STAT_KEYS}

# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()

EEVEE, VAPOREON, JOLTEON, FLAREON, ESPEON, UMBREON = 133, 134, 135, 136, 196, 197
LEAFEON, GLACEON, SYLVEON = 470, 471, 700
RALTS, KIRLIA, GARDEVOIR, GALLADE = 280, 281, 282, 475
NINCADA, NINJASK, SHEDINJA = 290, 291, 292
EEVEELUTIONS = (VAPOREON, JOLTEON, FLAREON, ESPEON, UMBREON, LEAFEON, GLACEON, SYLVEON)


def add_species(sid: int, name: str, type1: str, evolves_from: int | None = None) -> None:
    seed._upsert({
        "id": sid, "name": name, "name_de": name.title(), "name_en": name.title(), "type1": type1,
        "catch_rate": 45, "base_exp": 100, "growth_rate": "medium_fast", "base_hp": 50,
        "base_atk": 50, "base_def": 50, "base_spa": 50, "base_spd": 50, "base_spe": 50,
        "evolves_from": evolves_from, "generation": seed.generation_for(sid),
    })


add_species(EEVEE, "eevee", "normal")
for sid, name in zip(EEVEELUTIONS, ("vaporeon", "jolteon", "flareon", "espeon", "umbreon",
                                    "leafeon", "glaceon", "sylveon")):
    add_species(sid, name, "normal", EEVEE)
add_species(RALTS, "ralts", "psychic")
add_species(KIRLIA, "kirlia", "psychic", RALTS)
add_species(GARDEVOIR, "gardevoir", "psychic", KIRLIA)
add_species(GALLADE, "gallade", "psychic", KIRLIA)
add_species(NINCADA, "nincada", "bug")
add_species(NINJASK, "ninjask", "bug", NINCADA)
add_species(SHEDINJA, "shedinja", "bug", NINCADA)

evolution._write_rows(
    [{"from": EEVEE, "to": sid, "trigger": "use-item", "item_key": f"stone_{sid}"}
     for sid in EEVEELUTIONS]
    + [
        {"from": RALTS, "to": KIRLIA, "trigger": "level-up", "min_level": 20},
        {"from": KIRLIA, "to": GARDEVOIR, "trigger": "level-up", "min_level": 30},
        {"from": KIRLIA, "to": GALLADE, "trigger": "use-item", "item_key": "dawn_stone",
         "detail": {"gender": "male"}},
        {"from": NINCADA, "to": NINJASK, "trigger": "level-up", "min_level": 20},
        {"from": NINCADA, "to": SHEDINJA, "trigger": "level-up", "min_level": 20},
    ],
    "pokeapi", overwrite=True,
)

for uid in ("alice", "bob", "carol"):
    db.upsert_user(uid, uid.title())


def add_owned(user_id: str, species_id: int, gender: str | None = "female") -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "gender, caught_at) VALUES (?,?,?,?,0,?,?,?,?)",
        (user_id, species_id, 10, battle.exp_for_level(10, "medium_fast"), json.dumps(IVS),
         "hardy", gender, NOW),
    )


def need(user_id: str, species_id: int, gender: str | None = "female") -> int:
    return evolution.living_dex_need(user_id, species_id, gender)


# ------------------------------------------------ living_dex_need: Evoli
check("Evoli: ohne Besitz werden 9 Evolis gebraucht (Evoli + 8 Entwicklungen)",
      need("alice", EEVEE) == 9, need("alice", EEVEE))
add_owned("alice", EEVEE)
add_owned("alice", EEVEE)
add_owned("alice", FLAREON)
add_owned("alice", UMBREON)
check("Evoli: 2 Evoli + Flamara + Nachtara -> noch 5 Evolis noetig (Beispiel aus der Anfrage)",
      need("alice", EEVEE) == 5, need("alice", EEVEE))
check("Evoli-Linie: ein weiteres Flamara ist ueberzaehlig", need("alice", FLAREON) == 0)
check("Evoli-Linie: ein fehlendes Aquana wird gebraucht", need("alice", VAPOREON) == 1)
for _ in range(5):
    add_owned("alice", EEVEE)
check("Evoli: mit 7 Evolis ist die Linie gedeckt -> weiteres Evoli ueberzaehlig",
      need("alice", EEVEE) == 0, need("alice", EEVEE))
check("living_dex_need: je Spieler getrennt (bob besitzt nichts)", need("bob", EEVEE) == 9)

# ------------------------------- Ueberzaehlige weiter oben helfen nicht nach unten
add_owned("bob", FLAREON)
add_owned("bob", FLAREON)
check("Zwei Flamara ersetzen kein Evoli (Entwicklung nur vorwaerts)",
      need("bob", EEVEE) == 8, need("bob", EEVEE))

# ---------------------------------------- Geschlecht: Kirlia -> Galagladi nur maennlich
add_owned("carol", RALTS, "female")
add_owned("carol", RALTS, "female")
add_owned("carol", RALTS, "female")
check("Trasla: 3 weibliche decken Trasla, Kirlia, Guardevoir - Galagladi fehlt",
      need("carol", RALTS, "female") == 0 and need("carol", RALTS, "male") == 1,
      (need("carol", RALTS, "female"), need("carol", RALTS, "male")))
check("Galagladi selbst wird gebraucht, egal welches Geschlecht",
      need("carol", GALLADE, "male") == 1)

# ------------------------------------------- Ninjatom kommt mit Ninjask gratis
check("Nincada: 2 reichen fuer Nincada + Ninjask (Ninjatom kommt mit)",
      need("carol", NINCADA, "male") == 2, need("carol", NINCADA, "male"))

# ----------------------------------------------------- Option beim Fang
def is_locked(owned_id: int) -> bool:
    return bool(db.query_one("SELECT locked FROM owned_pokemon WHERE id = ?", (owned_id,))["locked"])


def encounter_row(species_id: int, gender: str | None = "female") -> dict:
    sp = db.query_one("SELECT * FROM species WHERE id = ?", (species_id,))
    encounter_id = spawn.create_encounter(sp, source_entity="test.trigger")
    db.execute("UPDATE encounter SET level = 10, ivs = ?, gender = ?, shiny = 0 WHERE id = ?",
               (json.dumps(IVS), gender, encounter_id))
    return db.query_one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))


def catch(species_id: int, user_id: str, lock: str | None = None, gender: str = "female") -> int:
    row = encounter_row(species_id, gender)
    res = encounters_api.finalize_catch(row, battle.combatant_from_encounter(row), user_id,
                                        "poke_ball", lock=lock)
    return res["owned_id"]


db.set_user_auto_lock_shiny("bob", False)
check("auto_lock_living_dex: Standard ist aus", db.user_auto_lock_living_dex("bob") is False)
check("auto_lock_living_dex: aus -> benoetigtes Evoli bleibt offen", not is_locked(catch(EEVEE, "bob")))
db.set_user_auto_lock_living_dex("bob", True)
check("auto_lock_living_dex: laesst sich einschalten", db.user_auto_lock_living_dex("bob") is True)
check("auto_lock_living_dex: benoetigtes Evoli wird gesperrt", is_locked(catch(EEVEE, "bob")))
check("auto_lock_living_dex: ueberzaehliges Flamara bleibt offen", not is_locked(catch(FLAREON, "bob")))
check("auto_lock_living_dex: ueberzaehliges Evoli (alice hat 7) bleibt offen",
      db.set_user_auto_lock_living_dex("alice", True) and not is_locked(catch(EEVEE, "alice")))

res = users_api.set_auto_lock_living_dex(
    users_api.AutoLockLivingDexChoice(auto_lock_living_dex=False), current_user={"id": "bob"})
check("PUT /users/me/auto-lock-living-dex schaltet um",
      res == {"auto_lock_living_dex": False} and db.user_auto_lock_living_dex("bob") is False)
check("/users/me liefert auto_lock_living_dex",
      users_api.me(current_user=db.get_user("bob"), is_admin=False)["auto_lock_living_dex"] is False)

# -------------------------------------------- lock-Parameter (aus Auto-Regeln)
check("lock='living_dex' sperrt Benoetigtes auch bei ausgeschalteter Option",
      is_locked(catch(VAPOREON, "bob", lock="living_dex")))
check("lock='living_dex' laesst Ueberzaehliges offen",
      not is_locked(catch(VAPOREON, "bob", lock="living_dex")))
check("lock='always' sperrt immer", is_locked(catch(VAPOREON, "bob", lock="always")))

# ----------------------------------------------------------- Auto-Regeln
cat = auto_rules.catalog()
check("Katalog: lock_modes", list(cat["lock_modes"]) == ["settings", "always", "living_dex"])
check("Katalog: Vorlage living_dex", "living_dex" in cat["templates"])
check("Actions.lock: Standard 'settings'", auto_rules.Actions().lock == "settings")
try:
    auto_rules.Actions(lock="sometimes")
    check("Actions.lock: unbekannter Wert wird abgelehnt", False)
except ValidationError:
    check("Actions.lock: unbekannter Wert wird abgelehnt", True)

cond_yes = auto_rules.Conditions(living_dex_needed=True)
cond_no = auto_rules.Conditions(living_dex_needed=False)
needed_row = encounter_row(JOLTEON)    # bob hat kein Blitza
surplus_row = encounter_row(FLAREON)   # bob hat Flamara mehrfach
check("Bedingung living_dex_needed=ja passt auf benoetigtes Blitza",
      auto_rules.matches(cond_yes, auto_rules.Facts(needed_row, "bob")))
check("Bedingung living_dex_needed=ja passt nicht auf ueberzaehliges Flamara",
      not auto_rules.matches(cond_yes, auto_rules.Facts(surplus_row, "bob")))
check("Bedingung living_dex_needed=nein passt auf ueberzaehliges Flamara",
      auto_rules.matches(cond_no, auto_rules.Facts(surplus_row, "bob")))
check("ohne Bedingung passt beides",
      auto_rules.matches(auto_rules.Conditions(), auto_rules.Facts(surplus_row, "bob")))


def rule_catch(row: dict, lock: str) -> int:
    result = {"achievements": []}
    auto_rules._finish_catch(row, battle.combatant_from_encounter(row), "bob", "poke_ball",
                             auto_rules.Actions(mode="catch", lock=lock), result)
    return result["owned_id"]


check("Regel-Fang mit lock='living_dex' sperrt benoetigtes Blitza",
      is_locked(rule_catch(needed_row, "living_dex")))
check("Regel-Fang mit lock='settings' laesst ueberzaehliges Flamara offen (Option aus)",
      not is_locked(rule_catch(surplus_row, "settings")))

tmpl = auto_rules.create_from_template("carol", "living_dex")
check("Vorlage 'Living Dex auffuellen' setzt Bedingung und Sperre",
      tmpl["conditions"].get("living_dex_needed") is True and tmpl["actions"].get("lock") == "living_dex")

# ----------------------------------------------------- Migration / i18n
cols = {r["name"] for r in db.query("PRAGMA table_info(app_user)")}
check("Spalte app_user.auto_lock_living_dex existiert", "auto_lock_living_dex" in cols)
db._migrate_auto_lock_living_dex(db.connect())  # zweiter Lauf: idempotent
check("Migration ist idempotent", "auto_lock_living_dex" in
      {r["name"] for r in db.query("PRAGMA table_info(app_user)")})

for lang in ("en", "de"):
    data = json.loads((ROOT / "pokemon_encounter/app/data/i18n" / f"{lang}.json").read_text())
    keys = ["auto_lock_living_dex.setting", "auto_lock_living_dex.setting_hint",
            "admin.field.auto_lock_living_dex", "auto_rules.act.lock",
            "auto_rules.cond.living_dex_needed", "auto_rules.template.living_dex"]
    keys += [f"auto_rules.lock.{m}" for m in auto_rules.LOCK_MODES]
    missing = [k for k in keys if k not in data]
    check(f"i18n {lang}: alle neuen Schluessel vorhanden", not missing, missing)

html = (ROOT / "pokemon_encounter/www/index.html").read_text()
check("Frontend: Schalter in den Einstellungen",
      'id="autoLockLivingDexPick"' in html and "/api/users/me/auto-lock-living-dex" in html)
check("Frontend: Auto-Regel-Felder",
      "key: 'living_dex_needed'" in html and "key: 'lock'" in html)

# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
