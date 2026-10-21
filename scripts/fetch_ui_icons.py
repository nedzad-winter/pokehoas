#!/usr/bin/env python3
"""Re-download the UI icon assets under pokemon_encounter/www/img/.

Two sets, both shipped with the add-on so the UI needs no external requests:

* types/<type>.png   - the in-game type plates (English for every UI language,
  like the type labels themselves). Source: PokeAPI/sprites, Scarlet/Violet.
* badges/<trainer_id>.png - the gym badge of every gym leader in
  app/data/trainers.json. Source: the Pokémon Wiki (Fandom); its CDN blocks
  plain scripted requests, so the images are pulled through the wsrv.nl image
  proxy, then scaled to 64 px and reduced to a 128-colour palette.

Usage:  python3 scripts/fetch_ui_icons.py      (needs Pillow + network)
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
IMG_DIR = ROOT / "pokemon_encounter" / "www" / "img"
TRAINERS = ROOT / "pokemon_encounter" / "app" / "data" / "trainers.json"

# PokeAPI type ids - the file names in the sprite repository.
TYPE_IDS = {
    "normal": 1, "fighting": 2, "flying": 3, "poison": 4, "ground": 5,
    "rock": 6, "bug": 7, "ghost": 8, "steel": 9, "fire": 10, "water": 11,
    "grass": 12, "electric": 13, "psychic": 14, "ice": 15, "dragon": 16,
    "dark": 17, "fairy": 18,
}
TYPE_URL = ("https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/"
            "types/generation-ix/scarlet-violet/{id}.png")

# Gym leader -> file name of its badge on the Pokémon Wiki. Kalos and Paldea
# reuse type names, so those files carry a suffix ("Fairy Badge" is Galar's,
# "Fairy Badge XY" Kalos'); Paldea's badges are unnamed in-game and filed
# under "<Type> Badge SV".
BADGE_FILES = {
    "kanto_brock": "Boulder Badge", "kanto_misty": "Cascade Badge",
    "kanto_surge": "Thunder Badge", "kanto_erika": "Rainbow Badge",
    "kanto_koga": "Soul Badge", "kanto_sabrina": "Marsh Badge",
    "kanto_blaine": "Volcano Badge", "kanto_giovanni": "Earth Badge",
    "johto_falkner": "Zephyr Badge", "johto_bugsy": "Hive Badge",
    "johto_whitney": "Plain Badge", "johto_morty": "Fog Badge",
    "johto_chuck": "Storm Badge", "johto_jasmine": "Mineral Badge",
    "johto_pryce": "Glacier Badge", "johto_clair": "Rising Badge",
    "hoenn_roxanne": "Stone Badge", "hoenn_brawly": "Knuckle Badge",
    "hoenn_wattson": "Dynamo Badge", "hoenn_flannery": "Heat Badge",
    "hoenn_norman": "Balance Badge", "hoenn_winona": "Feather Badge",
    "hoenn_tate_liza": "Mind Badge", "hoenn_juan": "Rain Badge",
    "sinnoh_roark": "Coal Badge", "sinnoh_gardenia": "Forest Badge",
    "sinnoh_fantina": "Relic Badge", "sinnoh_maylene": "Cobble Badge",
    "sinnoh_wake": "Fen Badge", "sinnoh_byron": "Mine Badge",
    "sinnoh_candice": "Icicle Badge", "sinnoh_volkner": "Beacon Badge",
    "unova_cilan": "Trio Badge", "unova_lenora": "Basic Badge",
    "unova_burgh": "Insect Badge", "unova_elesa": "Bolt Badge",
    "unova_clay": "Quake Badge", "unova_skyla": "Jet Badge",
    "unova_brycen": "Freeze Badge", "unova_drayden": "Legend Badge",
    "kalos_viola": "Bug Badge Viola", "kalos_grant": "Cliff Badge",
    "kalos_korrina": "Rumble Badge", "kalos_ramos": "Plant Badge",
    "kalos_clemont": "Voltage Badge", "kalos_valerie": "Fairy Badge XY",
    "kalos_olympia": "Psychic Badge", "kalos_wulfric": "Iceberg Badge",
    "galar_milo": "Grass Badge", "galar_nessa": "Water Badge",
    "galar_kabu": "Fire Badge", "galar_bea": "Fighting Badge",
    "galar_allister": "Ghost Badge", "galar_opal": "Fairy Badge",
    "galar_gordie": "Rock Badge", "galar_melony": "Ice Badge",
    "galar_piers": "Dark Badge", "galar_raihan": "Dragon Badge",
    "paldea_katy": "Bug Badge SV", "paldea_brassius": "Grass Badge SV",
    "paldea_iono": "Electric Badge SV", "paldea_kofu": "Water Badge SV",
    "paldea_larry": "Normal Badge SV", "paldea_ryme": "Ghost Badge SV",
    "paldea_tulip": "Psychic Badge SV", "paldea_grusha": "Ice Badge SV",
}

WIKI_API = "https://pokemon.fandom.com/api.php"
PROXY = "https://wsrv.nl/?output=png&w=80&h=80&fit=inside&we&trim=10&url="
UA = {"User-Agent": "Mozilla/5.0 (pokemon-encounter icon fetcher)"}


def get(url: str) -> bytes:
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=60).read()


def wiki_file_urls(titles: list[str]) -> dict[str, str]:
    """File name (without ".png") -> original image URL on the wiki."""
    urls: dict[str, str] = {}
    for start in range(0, len(titles), 20):
        chunk = titles[start:start + 20]
        query = urllib.parse.urlencode({
            "action": "query", "format": "json", "prop": "imageinfo",
            "iiprop": "url", "titles": "|".join(f"File:{t}.png" for t in chunk),
        })
        pages = json.loads(get(f"{WIKI_API}?{query}"))["query"]["pages"]
        for page in pages.values():
            name = page["title"][len("File:"):-len(".png")]
            if "missing" in page:
                print(f"  missing on the wiki: {name}")
                continue
            urls[name] = page["imageinfo"][0]["url"].split("/revision")[0]
    return urls


def fetch_types() -> None:
    out = IMG_DIR / "types"
    out.mkdir(parents=True, exist_ok=True)
    for key, type_id in TYPE_IDS.items():
        (out / f"{key}.png").write_bytes(get(TYPE_URL.format(id=type_id)))
        print(f"  types/{key}.png")


def fetch_badges() -> None:
    out = IMG_DIR / "badges"
    out.mkdir(parents=True, exist_ok=True)
    trainers = json.loads(TRAINERS.read_text(encoding="utf-8"))["trainers"]
    gyms = [t["id"] for t in trainers if t["kind"] == "gym_leader"]
    unknown = [tid for tid in gyms if tid not in BADGE_FILES]
    if unknown:
        print(f"  no badge file name for: {', '.join(unknown)}")
    urls = wiki_file_urls(sorted({BADGE_FILES[t] for t in gyms if t in BADGE_FILES}))
    for tid in gyms:
        source = urls.get(BADGE_FILES.get(tid, ""))
        if not source:
            continue
        raw = get(PROXY + urllib.parse.quote(source.replace("https://", ""), safe="/:"))
        icon = Image.open(BytesIO(raw)).convert("RGBA")
        icon.thumbnail((64, 64), Image.LANCZOS)
        # A 128-colour palette keeps the whole set at ~120 kB; the icons are
        # only ever shown at ~26 px, so the loss is invisible.
        icon.quantize(colors=128, method=Image.FASTOCTREE).save(
            out / f"{tid}.png", optimize=True)
        print(f"  badges/{tid}.png")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("all", "types"):
        print("type plates:")
        fetch_types()
    if what in ("all", "badges"):
        print("gym badges:")
        fetch_badges()
