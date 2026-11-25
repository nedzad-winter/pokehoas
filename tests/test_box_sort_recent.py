import json
from pathlib import Path

html = Path("pokemon_encounter/www/index.html").read_text(encoding="utf-8")
I18N = Path("pokemon_encounter/app/data/i18n")


def test_recent_sort():
    assert "recent_desc: (a, b) =>" in html and "recent_asc: (a, b) =>" in html
    for lang in ("en", "de", "es", "fr", "it", "ja", "ko", "pl"):
        strings = json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))
        assert strings["team.box_sort_recent_desc"] and strings["team.box_sort_recent_asc"], lang


if __name__ == "__main__":
    test_recent_sort()
    print("ok")
