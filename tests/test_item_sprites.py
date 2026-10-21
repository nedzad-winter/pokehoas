"""Tests fuer Item-Sprite-Aufloesung (app/held_items.sprite_url_for): jedes
Item aus dem Shop- und dem Held-Items-Katalog muss auf eine URL abbilden,
und die Items, fuer die die PokeAPI-Sprites-CDN keinen Datei-Slug unter dem
naiven `key.replace('_', '-')` hat, muessen ueber `_SPRITE_OVERRIDES` auf ein
tatsaechlich vorhandenes (Stand jetzt manuell verifiziertes) Sprite zeigen.

Aufruf:  python3 tests/test_item_sprites.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-item-sprites-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import held_items  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


DATA_DIR = ROOT / "pokemon_encounter" / "app" / "data"
held = json.loads((DATA_DIR / "held_items.json").read_text(encoding="utf-8"))
shop = json.loads((DATA_DIR / "shop.json").read_text(encoding="utf-8"))["items"]
all_keys = sorted(set(held.keys()) | {item["key"] for item in shop})

check("Alle Item-Keys aus held_items.json und shop.json liefern eine sprite_url",
      all(held_items.sprite_url_for(key).startswith(
          "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/items/")
          for key in all_keys))

# Items, die bei PokeAPI (Stand jetzt) keinen Sprite unter dem naiven Slug
# haben (neue Gen8/9-DLC-Items) bzw. dort in einem Unterordner liegen -
# jeweils manuell gegen die tatsaechliche PokeAPI/sprites-CDN verifiziert.
EXPECTED_OVERRIDES = {
    "incubator": "mystery-egg",
    "fairy_feather": "gen9/fairy-feather",
    "black_augurite": "common-stone",
    "chipped_pot": "berry-pots",
    "cracked_pot": "berry-pots",
    "linking_cord": "escape-rope",
    "metal_alloy": "iron-plate",
    "peat_block": "amaze-mulch",
    "sweet_apple": "rare-candy",
    "tart_apple": "rare-candy",
    "syrupy_apple": "rare-candy",
    "auspicious_armor": "armor-fossil",
    "malicious_armor": "armor-fossil",
    "unremarkable_teacup": "tea",
    "masterpiece_teacup": "tea",
}
for key, slug in EXPECTED_OVERRIDES.items():
    expected = f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/items/{slug}.png"
    check(f"sprite_url_for({key!r}) nutzt den verifizierten Override",
          held_items.sprite_url_for(key) == expected, held_items.sprite_url_for(key))

check("Alle bekannten Override-Keys stecken tatsaechlich im Katalog",
      set(EXPECTED_OVERRIDES) <= set(all_keys),
      sorted(set(EXPECTED_OVERRIDES) - set(all_keys)))

# Jeder Key ausserhalb der Override-Liste muss weiterhin ueber den naiven
# Slug aufgeloest werden (kein versehentliches Ueberschreiben).
plain_keys = [k for k in all_keys if k not in EXPECTED_OVERRIDES]
check("Alle uebrigen Items nutzen weiterhin den naiven key->slug-Pfad",
      all(held_items.sprite_url_for(k)
          == f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/items/{k.replace('_', '-')}.png"
          for k in plain_keys))


print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
