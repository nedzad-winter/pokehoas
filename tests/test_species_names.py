import sys
sys.path.insert(0, "pokemon_encounter")

from app.seed import names_from_species_doc


def test_names():
    doc = {"names": [
        {"language": {"name": "fr"}, "name": "Salamèche"},
        {"language": {"name": "en"}, "name": "Charmander"},
        {"language": {"name": "ja-Hrkt"}, "name": "ヒトカゲ"},
        {"language": {"name": "roomaji"}, "name": "Hitokage"},
    ]}
    names = names_from_species_doc(doc)
    assert names["fr"] == "Salamèche"
    assert names["ja"] == "ヒトカゲ"
    assert "roomaji" not in names and "pl" not in names


if __name__ == "__main__":
    test_names()
    print("ok")