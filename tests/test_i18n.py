"""Tests fuer die Uebersetzungs-Grundlage: Sprachpraeferenz pro Nutzer,
Fallback-Verhalten von i18n.t()/catalog(), und die zugehoerigen Endpunkte.

Aufruf:  python3 tests/test_i18n.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-i18n-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import abilities, battle, db, i18n, seed  # noqa: E402
from app.api import users as users_api  # noqa: E402

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


# ------------------------------------------------------------- Datenbank
check("Neue Nutzer bekommen 'en' als Standardsprache", alice["language"] == "en", alice["language"])
updated = db.set_user_language("alice", "de")
check("set_user_language aendert die Sprache", updated["language"] == "de")
check("upsert_user (erneuter Besuch) setzt die Sprache NICHT zurueck",
      db.upsert_user("alice", "Alice", "Alice W.")["language"] == "de")
db.set_user_language("alice", "en")


# --------------------------------------------------------------- i18n.py
check("Englisch ist eine verfuegbare Sprache", "en" in i18n.available_languages())
check("Deutsch ist eine verfuegbare Sprache", "de" in i18n.available_languages())
check(
    "t() liefert den echten Text fuer einen vorhandenen Key",
    i18n.t("en", "language.en") == "English",
)
check(
    "t() faellt bei fehlendem Key in einer vorhandenen Sprache auf Englisch zurueck",
    i18n.t("de", "__does_not_exist__", ) == "__does_not_exist__",
)
check(
    "t() faellt bei komplett unbekannter Sprache auf Englisch zurueck",
    i18n.t("xx", "language.en") == "English",
)
check(
    "catalog() mischt Englisch als Fallback fuer fehlende Keys ein",
    i18n.catalog("de").get("language.en") is not None,
)

# Platzhalter-Interpolation, mit einem Key nur fuers Testen.
i18n._catalogs["en"]["test.greeting"] = "Hello {name}!"  # noqa: SLF001
check("t() interpoliert Platzhalter", i18n.t("en", "test.greeting", name="Ash") == "Hello Ash!")
check(
    "t() stuerzt bei fehlendem Platzhalter nicht ab, gibt die Vorlage zurueck",
    i18n.t("en", "test.greeting") == "Hello {name}!",
)


# ------------------------------------------------------------------- API
me = users_api.me(current_user=alice, is_admin=False)
check("/api/users/me meldet die Sprache", me["language"] == "en", me["language"])
check("/api/users/me meldet verfuegbare Sprachen", set(me["available_languages"]) >= {"en", "de"})

result = users_api.set_language(
    users_api.LanguageChoice(language="de"), current_user=db.get_user("alice")
)
check("PUT /users/me/language setzt die Sprache", result["language"] == "de")
check("Persistiert in der DB", db.get_user("alice")["language"] == "de")

try:
    users_api.set_language(
        users_api.LanguageChoice(language="klingon"), current_user=db.get_user("alice")
    )
    check("Unbekannte Sprache wird abgelehnt", False)
except HTTPException as exc:
    check("Unbekannte Sprache wird abgelehnt (400)", exc.status_code == 400)
check("Abgelehnte Sprache aendert nichts", db.get_user("alice")["language"] == "de")


# --------------------------------------------------- Englisch als Standard
# Englisch ist jetzt die Standardsprache: species.name_en (aus dem Fallback-
# Datensatz, da kein Netzwerkzugriff im Test) sowie Faehigkeiten-/Item-Namen
# sollen ohne weiteres Zutun schon Englisch sein.
pika = db.query_one("SELECT * FROM species WHERE name = 'pikachu'")
check("Fallback-Datensatz liefert name_en", pika["name_en"] == "Pikachu", pika["name_en"])

owned_id = db.execute(
    "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, caught_at) "
    "VALUES (?,?,?,?,?,?,?,?)",
    ("alice", pika["id"], 5, 0, 0, json.dumps({k: 15 for k in ("hp", "atk", "def", "spa", "spd", "spe")}),
     "hardy", "2026-01-01T00:00:00+00:00"),
)
owned_row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))
fighter = battle.combatant_from_owned(owned_row)
check("Combatant.name bevorzugt name_en", fighter.name == "Pikachu", fighter.name)

check(
    "abilities.label() liefert Englisch als Standard",
    abilities.label("static") == "Static", abilities.label("static"),
)
ability_desc = abilities.describe("static")
check(
    "abilities.describe() liefert sowohl name (Englisch) als auch name_de",
    ability_desc["name"] == "Static" and ability_desc["name_de"] == "Statik",
    ability_desc,
)


# --------------------------------------------- Frontend-Keys (index.html)
# Das Frontend holt seine Texte ueber tr('key') bzw. data-i18n="key" aus
# denselben json-Dateien. Ein Tippfehler dort faellt sonst erst im Browser
# auf (tr() zeigt dann den rohen Key an) - hier statt dessen sofort.
html = (ROOT / "pokemon_encounter" / "www" / "index.html").read_text(encoding="utf-8")
en_strings = json.loads(
    (ROOT / "pokemon_encounter" / "app" / "data" / "i18n" / "en.json").read_text(encoding="utf-8")
)
de_strings = json.loads(
    (ROOT / "pokemon_encounter" / "app" / "data" / "i18n" / "de.json").read_text(encoding="utf-8")
)
used = set(re.findall(r"""tr\(\s*['"]([a-z0-9_.]+)['"]""", html))
used |= set(re.findall(r'data-i18n="([a-z0-9_.]+)"', html))
# Praefixe wie tr('gen.' + g) sind zur Laufzeit zusammengesetzt - die kann
# dieser Test nicht pruefen.
missing_en = sorted(k for k in used if not k.endswith(".") and k not in en_strings)
check("Alle im Frontend fest genutzten Keys stehen in en.json", not missing_en, missing_en)
check("en.json und de.json haben dieselben Keys",
      set(en_strings) == set(de_strings),
      sorted(set(en_strings) ^ set(de_strings)))
# Alle weiteren Sprachen (es, fr, it, pl, ja, ko, ...) muessen ebenfalls vollstaendig
# sein - sonst zeigt die Oberflaeche dort englische Texte an.
for path in sorted((ROOT / "pokemon_encounter" / "app" / "data" / "i18n").glob("*.json")):
    lang_strings = json.loads(path.read_text(encoding="utf-8"))
    check(f"{path.stem}.json hat dieselben Keys wie en.json",
          set(lang_strings) == set(en_strings),
          sorted(set(lang_strings) ^ set(en_strings)))
    check(f"{path.stem}.json ist alphabetisch sortiert", list(lang_strings) == sorted(lang_strings))


# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
