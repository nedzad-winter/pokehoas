"""Every supported language must be complete: same keys and placeholders in each
catalog, sorted catalogs, no key used in code but missing, and a description for
every item and ability in every language.

Aufruf:  python3 tests/test_i18n_languages.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "pokemon_encounter" / "app"
sys.path.insert(0, str(ROOT / "pokemon_encounter"))
os.environ["POKEMON_DATA_DIR"] = tempfile.mkdtemp(prefix="pokemon-i18n-lang-test-")
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import i18n, mega, rewards, trainers  # noqa: E402
from app.api import team as team_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    print(f"[{'ok  ' if condition else 'FAIL'}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


LANGS = ("en", "de", "es", "fr", "it", "ja", "ko", "pl")
catalogs = {lang: json.loads((APP / "data" / "i18n" / f"{lang}.json").read_text(encoding="utf-8")) for lang in LANGS}
english = catalogs["en"]
placeholders = lambda text: set(re.findall(r"\{(\w+)\}", text))  # noqa: E731

for lang, catalog in catalogs.items():
    check(f"{lang}: same keys as English", set(catalog) == set(english), sorted(set(catalog) ^ set(english))[:5])
    check(f"{lang}: sorted", list(catalog) == sorted(catalog))
    check(f"{lang}: no empty text", all(str(v).strip() for v in catalog.values()))
    wrong = [k for k in catalog if k in english and placeholders(catalog[k]) != placeholders(english[k])]
    check(f"{lang}: same placeholders", not wrong, wrong[:5])

# Keys named literally in the code must exist.
sources = [p.read_text(encoding="utf-8") for p in APP.rglob("*.py") if p.name != "agent_tasks.py"]
sources.append((ROOT / "pokemon_encounter" / "www" / "index.html").read_text(encoding="utf-8"))
text = "\n".join(sources)
used = set(re.findall(r"""\bi18n\.t(?:_request)?\((?:[^,()"']+(?:\([^()]*\))?,\s*)?["']([\w.]+)["']""", text))
used |= set(re.findall(r"""\btr\(\s*'([\w.]+)'""", text))
used |= set(re.findall(r"""data-i18n(?:-title)?="([\w.]+)\"""", text))
missing = sorted(k for k in used if not k.endswith(".") and k not in english)
check("every literal key in the code exists", not missing, missing[:20])
check("the scan found the keys", len(used) > 800, len(used))

# Descriptions: every catalog entry, every language that isn't kept in the catalogs.
abilities = json.loads((APP / "data" / "abilities.json").read_text(encoding="utf-8"))
shop = json.loads((APP / "data" / "shop.json").read_text(encoding="utf-8"))["items"]
held = json.loads((APP / "data" / "held_items.json").read_text(encoding="utf-8"))
items = {i["key"] for i in shop} | {k for k in held if not k.startswith("_")}
for lang in ("es", "fr", "it", "ja", "ko", "pl"):
    described = team_api.ability_descriptions(lang)
    gaps = sorted(set(abilities) - set(described))
    check(f"{lang}: every ability has a description", not gaps, gaps[:5])
    described = team_api.item_descriptions(lang)
    gaps = sorted(items - set(described))
    check(f"{lang}: every item has a description", not gaps, gaps[:5])

# Trainers: an official name per game language, picked by display_name.
roster = trainers.all_trainers()
gaps = [t["id"] for t in roster if any(not (t.get("names") or {}).get(lang) for lang in ("es", "fr", "it", "ja", "ko"))]
check("every trainer has its official names", not gaps, gaps[:5])
brock = trainers.get("kanto_brock")
check("trainer name: fr", trainers.display_name(brock, "fr") == "Pierre")
check("trainer name: ja", trainers.display_name(brock, "ja") == "タケシ")
check("trainer name: de", trainers.display_name(brock, "de") == "Rocko")
check("trainer name: pl keeps English", trainers.display_name(brock, "pl") == "Brock")

# Language-dependent formatting.
check("thousands: en", i18n.group_digits(200000, "en") == "200,000")
check("thousands: de", i18n.group_digits(200000, "de") == "200.000")
check("thousands: fr", i18n.group_digits(200000, "fr") == "200 000")
check("money label uses them", "200.000" in rewards.money_label(200000, "es"))
check("Accept-Language picks a supported language", i18n.language_from_accept("xx, ko;q=0.8") == "ko")
check("Accept-Language falls back", i18n.language_from_accept(None) == i18n.DEFAULT_LANGUAGE)

# Mega names are built from the translated species name.
form = {"kind": "mega", "name": "Mega Charizard X", "name_de": "Mega-Glurak X", "species_id": 6}
names = mega._form_names(form, {"fr": "Dracaufeu", "ja": "リザードン"})
check("mega: fr", names["fr"] == "Méga-Dracaufeu X", names)
check("mega: ja", names["ja"] == "メガリザードンX", names)
check("mega: catalog keeps de", names["de"] == "Mega-Glurak X", names)

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nAll i18n language checks passed")
