import sys
sys.path.insert(0, "pokemon_encounter")

from app.api import team


def test_name_tables():
    fr = team.name_tables("fr")
    assert fr["types"]["fire"] == "Feu"
    assert fr["abilities"]["blaze"] and fr["moves"]["tackle"]
    assert team.name_tables("pl") == {"abilities": {}, "moves": {}, "types": {}}


if __name__ == "__main__":
    test_name_tables()
    print("ok")
