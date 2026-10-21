"""Mega-Entwicklung und Proto-Formen (Katalog in `data/mega_evolutions.json`).

Wie in den Hauptspielen: Ein Pokémon traegt den zu seiner Art passenden
Mega-Stein - ein gewoehnliches getragenes Item (`held_items.json`,
`effect: "mega_stone"`, im Shop die Kategorie `mega`) - und kann damit IM KAMPF
megaentwickeln. Regeln, bewusst wie im Spiel:

* Nur EIN Pokémon pro Seite und Kampf (`Battle.mega_used` / `foe_mega_used`).
* Freiwillig: der Spieler entscheidet per Aktion `mega` (siehe battle_session/
  trainer_battle/pvp), die Mega-Entwicklung selbst kostet die Runde NICHT.
* Sie haelt bis zum Kampfende, auch ueber ein Auswechseln hinweg.

Die Form lebt ausschliesslich im Kampf: `battle.apply_mega()` haengt dem
Combatant eine abgeleitete species-Kopie mit den Mega-Werten an (`species_for`:
Basiswerte, Typen, Faehigkeit, Name, Sprite), `owned_pokemon.species_id` bleibt unberuehrt - nach
dem Kampf ist das Pokémon von sich aus wieder normal. Dex-Nummer, Basis-EXP,
Fangrate und Wachstumsrate bleiben die der Grundform (die Mega-Form ist kein
eigener Pokédex-Eintrag).

Proto-Formen (Kyogre/Groudon mit Rotem/Blauem Edelstein) laufen ueber genau
denselben Mechanismus, nur mit eigener Log-Zeile - `kind` unterscheidet sie.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import db, held_items

DATA_DIR = Path(__file__).parent / "data"

EFFECT = "mega_stone"
CATEGORY = "mega"

_forms: dict[str, dict[str, Any]] | None = None


def catalog() -> dict[str, dict[str, Any]]:
    """Alle Formen, Schluessel = Item-Key des Steins."""
    global _forms
    if _forms is None:
        raw = json.loads((DATA_DIR / "mega_evolutions.json").read_text(encoding="utf-8"))
        _forms = {key: {"stone": key, **value} for key, value in raw["forms"].items()}
    return _forms


def get(stone_key: str | None) -> dict[str, Any] | None:
    if not stone_key:
        return None
    return catalog().get(stone_key)


def is_stone(item_key: str | None) -> bool:
    return get(item_key) is not None


def form_for(species_id: int | None, stone_key: str | None) -> dict[str, Any] | None:
    """Form, in die genau diese Art mit genau diesem Stein wechselt.
    Ein Stein auf der falschen Art bleibt wirkungslos (wie im Spiel)."""
    form = get(stone_key)
    if form is None or species_id is None or form["species_id"] != species_id:
        return None
    return form


def forms_for_species(species_id: int) -> list[dict[str, Any]]:
    """Alle Formen dieser Art (Glurak/Mewtu haben je zwei Steine)."""
    return [form for form in catalog().values() if form["species_id"] == species_id]


def name(form: dict[str, Any], lang: str = "en") -> str:
    return (form.get("name_de") if lang == "de" else None) or form["name"]


def stone_label(form: dict[str, Any], lang: str = "en") -> str | None:
    return held_items.label(form["stone"], lang)


def describe(form: dict[str, Any] | None) -> dict[str, Any] | None:
    """Anzeige-Infos fuers Frontend (waehlt die Sprache selbst, siehe `pick`)."""
    if form is None:
        return None
    return {
        "stone": form["stone"],
        "kind": form["kind"],
        "name": form["name"],
        "name_de": form.get("name_de"),
        "sprite_url": form.get("sprite_url"),
        "item_name": held_items.label(form["stone"], "en"),
        "item_name_de": (held_items.get(form["stone"]) or {}).get("name_de"),
    }


def species_for(base: dict[str, Any], form: dict[str, Any]) -> dict[str, Any]:
    """Abgeleitete species-Zeile fuer die Kampfdauer: Basiswerte, Typen, Name
    und Sprite der Mega-Form, alles andere (id, base_exp, catch_rate,
    growth_rate, ...) bleibt die Grundform."""
    return {
        **base,
        "name": form["name"],
        "name_en": form["name"],
        "name_de": form.get("name_de"),
        "type1": form["type1"],
        "type2": form.get("type2"),
        "base_hp": form["base_hp"],
        "base_atk": form["base_atk"],
        "base_def": form["base_def"],
        "base_spa": form["base_spa"],
        "base_spd": form["base_spd"],
        "base_spe": form["base_spe"],
        "sprite_url": form.get("sprite_url") or base.get("sprite_url"),
    }


def available_stones() -> set[str]:
    """Steine, deren Art im Pokédex dieser Installation ueberhaupt vorkommt -
    der Shop bietet nur diese an (bei nur teilweise geseedeten Generationen
    waere der Rest unbenutzbar)."""
    forms = catalog()
    ids = sorted({form["species_id"] for form in forms.values()})
    if not ids:
        return set()
    known = {
        row["id"] for row in db.query(
            "SELECT id FROM species WHERE id IN (%s)" % ",".join("?" * len(ids)), tuple(ids)
        )
    }
    return {key for key, form in forms.items() if form["species_id"] in known}
