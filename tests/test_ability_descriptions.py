import sys
sys.path.insert(0, "pokemon_encounter")

from app.api import team


def test_descriptions():
    fr = team.ability_descriptions("fr")
    assert fr["blaze"]
    assert team.ability_descriptions("pl") == {}
    assert team.ability_descriptions("de") == {}


if __name__ == "__main__":
    test_descriptions()
    print("ok")
