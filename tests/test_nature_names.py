import json
import sys
from pathlib import Path

sys.path.insert(0, "pokemon_encounter")

from app import spawn

I18N = Path("pokemon_encounter/app/data/i18n")


def test_every_nature_is_translated():
    for lang in ("en", "de", "fr", "es", "it", "ja", "ko", "pl"):
        strings = json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))
        for key in spawn.NATURES:
            assert strings.get(f"nature.{key}"), f"{lang}: nature.{key} manque"


if __name__ == "__main__":
    test_every_nature_is_translated()
    print("ok")
