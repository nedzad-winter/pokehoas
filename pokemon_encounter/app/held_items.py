"""Getragene Items (Katalog in `data/held_items.json`).

Jedes Item hat einen `effect`-Typ, den die Kampf-Engine in `battle.py`
auswertet. Neue Items mit einem bereits bekannten Effekt (z. B. weitere
Typ-Verstaerker wie Schwarzgurt) brauchen nur einen neuen JSON-Eintrag.

Im Inventar liegen getragene Items unter ihrem Key (z. B. `leftovers`),
genau wie Baelle. Anlegen verschiebt ein Exemplar vom Inventar auf das
Pokémon (`owned_pokemon.held_item`), Ablegen wieder zurueck.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"

_items: dict[str, dict[str, Any]] | None = None

# Items ohne (oder mit anders benanntem) Sprite in der PokeAPI-Sprites-CDN
# bekommen ein sinnvoll aehnliches Icon bzw. den tatsaechlichen Pfad.
_SPRITE_OVERRIDES: dict[str, str] = {
    "incubator": "mystery-egg",
    # Gen9-DLC-Items (Fairy Feather liegt bei PokeAPI im gen9-Unterordner).
    "fairy_feather": "gen9/fairy-feather",
    # Die folgenden Items haben (Stand jetzt) noch kein eigenes Sprite in der
    # PokeAPI-Sprites-CDN; sie bekommen ein optisch aehnliches Ersatz-Icon.
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


def sprite_url_for(key: str) -> str:
    """Icon-URL fuer einen Item-Key, egal ob getragenes Item oder Shop-Artikel."""
    slug = _SPRITE_OVERRIDES.get(key, key.replace("_", "-"))
    return f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/items/{slug}.png"


def catalog() -> dict[str, dict[str, Any]]:
    global _items
    if _items is None:
        raw = json.loads((DATA_DIR / "held_items.json").read_text(encoding="utf-8"))
        _items = {
            key: {"key": key, "sprite_url": sprite_url_for(key), **value}
            for key, value in raw.items()
        }
    return _items


def get(key: str | None) -> dict[str, Any] | None:
    if not key:
        return None
    return catalog().get(key)


def effect_of(key: str | None) -> str | None:
    item = get(key)
    return item["effect"] if item else None


def any_holds(team: Any, effect: str) -> bool:
    """True if any member of `team` (anything with a `.held_item` attribute,
    e.g. a battle Combatant) holds an item with the given effect - like
    `exp_share`, an Amulet Coin works even without joining the battle."""
    return any(effect_of(member.held_item) == effect for member in team)


def label(key: str | None, lang: str = "en") -> str | None:
    """Anzeigename. Fuer beide Sprachen siehe den vollen Katalog-Eintrag
    (`get()`/`catalog()`), der `name`+`name_de` traegt - `lang` ist fuer
    serverseitig gebauten Text (Kampf-Log-Zeilen), der schon vor der Antwort
    an das Frontend die richtige Sprache braucht."""
    item = get(key)
    if not item:
        return None
    return (item.get("name_de") if lang == "de" else None) or item["name"]
