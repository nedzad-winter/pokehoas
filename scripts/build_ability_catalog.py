#!/usr/bin/env python3
"""Fills `pokemon_encounter/app/data/abilities.json` with EVERY main-series
ability from PokeAPI, so the game never has to show a "no known effect"
placeholder.

* Implemented abilities (entries with an `effect`, written by hand together
  with their handler in app/ability_effects.py) are kept as they are - only
  missing `name`/`name_de` are filled in from PokeAPI.
* Every other ability gets `name`, `name_de`, `description` (PokeAPI short
  effect), `description_de` (latest German flavor text, or DE_FALLBACK below
  where PokeAPI has none) and `inactive`: why it does nothing in this battle
  system (`ability.inactive.<reason>` in i18n, shown by the frontend).
  Unless listed in one of the reason sets below, the reason is "todo" -
  implementable with the current engine, just not done yet. When an ability
  gets implemented, give it an `effect` + hand-written description and drop
  it from these sets.

Run from the repo root (network access to pokeapi.co, or a cached dump):

    python3 scripts/build_ability_catalog.py
    python3 scripts/build_ability_catalog.py --cache abilities_raw.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "pokemon_encounter" / "app" / "data" / "abilities.json"
POKEAPI = "https://pokeapi.co/api/v2"

# Only matters next to allies (double battles) - battles here are 1 vs 1.
DOUBLES = {
    "aroma_veil", "battery", "commander", "costar", "curious_medicine", "dancer",
    "flower_veil", "friend_guard", "healer", "hospitality", "minus", "pastel_veil",
    "plus", "power_of_alchemy", "power_spot", "propeller_tail", "receiver", "stalwart",
    "steely_spirit", "symbiosis", "telepathy",
}

# Needs weather or terrain, which this battle system does not have (yet).
WEATHER = {
    "air_lock", "chlorophyll", "cloud_nine", "delta_stream", "desolate_land", "drizzle",
    "drought", "electric_surge", "flower_gift", "forecast", "grass_pelt", "grassy_surge",
    "hadron_engine", "harvest", "hydration", "ice_body", "leaf_guard", "mega_sol",
    "mimicry", "misty_surge", "orichalcum_pulse", "overcoat", "primordial_sea",
    "protosynthesis", "psychic_surge", "quark_drive", "rain_dish", "sand_force",
    "sand_rush", "sand_spit", "sand_stream", "sand_veil", "seed_sower", "slush_rush",
    "snow_cloak", "snow_warning", "solar_power", "surge_surfer", "swift_swim",
    "teraform_zero",
}

# Works outside of battle in the main games.
OVERWORLD = {"ball_fetch", "honey_gather", "illuminate", "pickup", "run_away"}

# Relies on a battle mechanic this engine does not model (move properties
# like punch/sound/bite/contact/wind, confusion, infatuation, PP, form
# changes, weight, accuracy/evasion stages, switching out, item theft, ...).
MECHANIC = {
    "arena_trap", "as_one_glastrier", "as_one_spectrier", "battle_bond", "bulletproof",
    "comatose", "cursed_body", "cute_charm", "damp", "disguise", "embody_aspect",
    "emergency_exit", "gulp_missile", "heavy_metal", "hunger_switch", "ice_face",
    "illusion", "imposter", "infiltrator", "iron_fist", "keen_eye", "libero",
    "light_metal", "lingering_aroma", "liquid_voice", "long_reach", "magician",
    "magnet_pull", "mega_launcher", "minds_eye", "multitype", "mummy",
    "neutralizing_gas", "parental_bond", "perish_body", "pickpocket", "piercing_drill",
    "poison_puppeteer", "power_construct", "pressure", "protean", "punk_rock",
    "rks_system", "schooling", "screen_cleaner", "shadow_tag", "sharpness",
    "shields_down", "skill_link", "soundproof", "stance_change", "sticky_hold",
    "strong_jaw", "suction_cups", "supersweet_syrup", "tangled_feet", "tera_shell",
    "tera_shift", "tough_claws", "trace", "unseen_fist", "wandering_spirit",
    "wimp_out", "wind_power", "wind_rider", "zen_mode", "zero_to_hero",
}

REASONS = {"doubles": DOUBLES, "weather": WEATHER, "overworld": OVERWORLD,
           "mechanic": MECHANIC}

# German descriptions for abilities PokeAPI has no German flavor text for.
DE_FALLBACK = {
    "anger_shell": "Sinken die KP unter die Hälfte, sinken Verteidigung und Spezial-Verteidigung, dafür steigen Angriff, Spezial-Angriff und Initiative.",
    "armor_tail": "Gegner können keine Attacken mit erhöhter Priorität wie Ruckzuckhieb einsetzen.",
    "beads_of_ruin": "Senkt die Spezial-Verteidigung aller anderen Pokémon.",
    "commander": "Schlüpft in das Maul eines verbündeten Heerashai, wenn eines auf dem Feld ist.",
    "costar": "Übernimmt beim Kampfantritt die Statuswert-Veränderungen eines Mitstreiters.",
    "cud_chew": "Isst am Ende der nächsten Runde eine bereits verzehrte Beere noch einmal.",
    "dragonize": "Normal-Attacken werden zu Drachen-Attacken und um 20 % stärker.",
    "earth_eater": "Wird es von einer Boden-Attacke getroffen, füllt es KP auf, statt Schaden zu nehmen.",
    "electromorphosis": "Wird es von einer Attacke getroffen, verdoppelt sich die Stärke seiner nächsten Elektro-Attacke.",
    "embody_aspect": "Erhöht je nach Form des terakristallisierten Ogerpon einen bestimmten Statuswert.",
    "good_as_gold": "Immun gegen Status-Attacken.",
    "guard_dog": "Bedroher erhöht seinen Angriff, und es kann nicht zum Auswechseln gezwungen werden.",
    "hadron_engine": "Erzeugt beim Kampfantritt ein Elektrofeld und erhöht darauf den Spezial-Angriff.",
    "hospitality": "Heilt beim Kampfantritt einen Mitstreiter um ein Viertel seiner KP.",
    "lingering_aroma": "Berührt ein Angreifer das Pokémon, wird seine Fähigkeit zu Duftschwade.",
    "mega_sol": "Setzt Attacken ein, als würde die Sonne grell scheinen.",
    "minds_eye": "Ignoriert Fluchtwert-Änderungen des Gegners, die eigene Genauigkeit kann nicht sinken, und Normal- und Kampf-Attacken treffen Geister-Pokémon.",
    "mycelium_might": "Status-Attacken werden zuletzt ausgeführt, ignorieren dafür aber die Fähigkeit des Ziels.",
    "opportunist": "Kopiert Statuswert-Erhöhungen des Gegners.",
    "orichalcum_pulse": "Lässt beim Kampfantritt die Sonne grell scheinen und erhöht dabei den Angriff.",
    "piercing_drill": "Kontakt-Attacken treffen auch Ziele, die sich schützen.",
    "poison_puppeteer": "Von seinen Attacken vergiftete Pokémon werden zusätzlich verwirrt.",
    "protosynthesis": "Erhöht bei Sonnenschein oder mit Energiekapsel den höchsten Statuswert.",
    "purifying_salt": "Schützt vor Statusproblemen und halbiert den Schaden von Geister-Attacken.",
    "quark_drive": "Erhöht auf Elektrofeld oder mit Energiekapsel den höchsten Statuswert.",
    "rocky_payload": "Erhöht die Stärke von Gestein-Attacken.",
    "seed_sower": "Erzeugt ein Grasfeld, wenn das Pokémon von einer Attacke getroffen wird.",
    "sharpness": "Erhöht die Stärke von Schneide-Attacken.",
    "spicy_spray": "Verbrennt den Angreifer, wenn es durch eine Attacke Schaden nimmt.",
    "supersweet_syrup": "Senkt beim ersten Kampfantritt den Fluchtwert aller angrenzenden Gegner um eine Stufe.",
    "supreme_overlord": "Angriff und Spezial-Angriff steigen für jedes bereits besiegte Pokémon im eigenen Team.",
    "sword_of_ruin": "Senkt die Verteidigung aller anderen Pokémon.",
    "tablets_of_ruin": "Senkt den Angriff aller anderen Pokémon.",
    "tera_shell": "Bei vollen KP sind alle Schadensattacken gegen das Pokémon nicht sehr effektiv.",
    "tera_shift": "Nimmt beim Kampfantritt bis zum Kampfende seine Terakristall-Form an.",
    "teraform_zero": "Hebt Wetter und Felder auf, sobald Terapagos seine Stellarform annimmt.",
    "thermal_exchange": "Wird es von einer Feuer-Attacke getroffen, steigt sein Angriff. Kann nicht verbrannt werden.",
    "toxic_chain": "Kann das Ziel bei einem Treffer schwer vergiften.",
    "toxic_debris": "Streut Giftspitzen vor die Gegner, wenn es durch physische Attacken Schaden nimmt.",
    "vessel_of_ruin": "Senkt den Spezial-Angriff aller anderen Pokémon.",
    "well_baked_body": "Wird es von einer Feuer-Attacke getroffen, steigt seine Verteidigung stark, statt Schaden zu nehmen.",
    "wind_power": "Wird es von einer Wind-Attacke getroffen, verdoppelt sich die Stärke seiner nächsten Elektro-Attacke.",
    "wind_rider": "Immun gegen Wind-Attacken; wird es von einer getroffen, steigt sein Angriff um eine Stufe.",
    "zero_to_hero": "Nimmt beim Auswechseln seine Heldenform an.",
}


def _clean(text: str) -> str:
    text = text.replace("­\n", "").replace("­", "").replace("-\n", "-")
    return re.sub(r"\s+", " ", text).strip()


def _in(entries: list[dict], lang: str) -> list[dict]:
    return [e for e in entries if e["language"]["name"] == lang]


def fetch_all() -> list[dict]:
    import httpx  # app dependency, see pokemon_encounter/requirements.txt

    client = httpx.Client(timeout=30, headers={"User-Agent": "PokeHOAS-catalog-builder"})
    index = client.get(f"{POKEAPI}/ability", params={"limit": 2000}).json()["results"]

    def get(entry: dict) -> dict:
        for attempt in range(4):
            try:
                resp = client.get(entry["url"])
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPError:
                if attempt == 3:
                    raise
        raise AssertionError("unreachable")

    with concurrent.futures.ThreadPoolExecutor(12) as pool:
        return list(pool.map(get, index))


def build(docs: list[dict], catalog: dict[str, dict]) -> tuple[dict[str, dict], list[str]]:
    problems: list[str] = []
    out: dict[str, dict] = {}
    for doc in docs:
        if not doc.get("is_main_series"):
            continue
        key = doc["name"].replace("-", "_")
        name_en = next((n["name"] for n in _in(doc["names"], "en")), None)
        name_de = next((n["name"] for n in _in(doc["names"], "de")), None)
        existing = catalog.get(key) or {}
        if existing.get("effect"):
            entry = {"name": existing.get("name") or name_en, "name_de": existing.get("name_de") or name_de}
            entry.update({k: v for k, v in existing.items() if k not in ("name", "name_de", "inactive")})
            out[key] = entry
            continue
        if not name_en or not name_de:
            continue  # brand-new ability without a German name yet - skip
        short = next((e["short_effect"] for e in _in(doc["effect_entries"], "en")), None)
        if short:
            short = short.replace("$effect_chance", str(doc.get("effect_chance") or ""))
        flavors_de = _in(doc["flavor_text_entries"], "de")
        description_de = _clean(flavors_de[-1]["flavor_text"]) if flavors_de else DE_FALLBACK.get(key)
        if not short or not description_de:
            problems.append(f"{key}: missing {'English' if not short else 'German'} description")
            continue
        reason = next((r for r, keys in REASONS.items() if key in keys), "todo")
        out[key] = {"name": name_en, "name_de": name_de, "description": _clean(short),
                    "description_de": description_de, "inactive": reason}
    # Implemented abilities PokeAPI doesn't know (shouldn't happen) stay.
    for key, entry in catalog.items():
        if entry.get("effect") and key not in out:
            out[key] = entry
    return dict(sorted(out.items())), problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cache", type=Path, help="JSON list of /ability/{id} documents instead of fetching")
    args = parser.parse_args()
    docs = json.loads(args.cache.read_text(encoding="utf-8")) if args.cache else fetch_all()
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    out, problems = build(docs, catalog)
    if problems:
        print("Not written - add a DE_FALLBACK entry (or check PokeAPI) for:", file=sys.stderr)
        for line in problems:
            print("  " + line, file=sys.stderr)
        return 1
    CATALOG.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    implemented = sum(1 for e in out.values() if e.get("effect"))
    print(f"{CATALOG.relative_to(ROOT)}: {len(out)} abilities, {implemented} implemented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
