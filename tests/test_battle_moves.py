import sys
sys.path.insert(0, "pokemon_encounter")

from app import abilities, battle, name_tables


def test_labels():
    tackle = {"key": "tackle", "name": "Tackle", "name_de": "Tackle"}
    assert battle.move_label(tackle, "en") == "Tackle"
    assert battle.move_label(tackle, "fr") == name_tables.lookup("moves", "tackle", "fr")
    assert battle.move_label(tackle, "fr") not in (None, "", "Tackle")
    assert battle.move_label(tackle, "pl") == "Tackle"
    assert abilities.label("blaze", "fr") == name_tables.lookup("abilities", "blaze", "fr")
    assert abilities.label("blaze", "en") == "Blaze"


if __name__ == "__main__":
    test_labels()
    print("ok")
