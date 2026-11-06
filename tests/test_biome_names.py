import json
import sys
from pathlib import Path

sys.path.insert(0, "pokemon_encounter")

DATA = Path("pokemon_encounter/app/data")
LANGS = ("en", "de", "es", "fr", "it", "ja", "ko", "pl")


def test_every_biome_is_translated():
    biomes = json.loads((DATA / "biomes.json").read_text(encoding="utf-8"))["biomes"]
    for lang in LANGS:
        strings = json.loads((DATA / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        for key in biomes:
            assert strings.get(f"biome.{key}"), f"{lang}: biome.{key} manque"


if __name__ == "__main__":
    test_every_biome_is_translated()
    print("ok")
