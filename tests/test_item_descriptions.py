import sys
sys.path.insert(0, "pokemon_encounter")

from app.api import team


def test_descriptions():
    fr = team.item_descriptions("fr")
    assert fr["great_ball"] and fr["leftovers"]
    assert team.item_descriptions("pl")["great_ball"]
    assert team.item_descriptions("de") == {}


if __name__ == "__main__":
    test_descriptions()
    print("ok")
