import sys
sys.path.insert(0, "pokemon_encounter")

from app import economy, medicine


def test_label():
    fr = economy.item_label("revive", "fr")
    assert fr and fr != "Revive"
    assert medicine.label({"key": "revive", "label": "Revive"}, "fr") == fr
    assert medicine.label({"key": "revive", "label": "Revive"}, "en") == "Revive"


if __name__ == "__main__":
    test_label()
    print("ok")
