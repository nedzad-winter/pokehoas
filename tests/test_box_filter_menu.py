import json
from pathlib import Path

html = Path("pokemon_encounter/www/index.html").read_text(encoding="utf-8")
I18N = Path("pokemon_encounter/app/data/i18n")


def test_filter_menu():
    assert "function filterMenuHtml(" in html and "function keepBoxSearchFocus(" in html
    assert "search: true, evolve: true, locked: true, duplicates: true" in html
    for lang in ("en", "de", "es", "fr", "it", "ja", "ko", "pl"):
        strings = json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))
        assert strings["team.box_filters"], lang
    fr = json.loads((I18N / "fr.json").read_text(encoding="utf-8"))
    for key in ("pokedex.shiny_only", "team.favorites_only", "team.box_locked_only", "team.box_duplicates_only"):
        assert "uniquement" not in fr[key], key


if __name__ == "__main__":
    test_filter_menu()
    print("ok")
