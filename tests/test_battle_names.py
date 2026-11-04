import sys
sys.path.insert(0, "pokemon_encounter")

from app import battle

SPECIES = {
    "id": 4, "name": "charmander", "name_en": "Charmander", "name_de": "Glumanda",
    "names": '{"en": "Charmander", "fr": "Salamèche"}',
    "type1": "fire", "type2": None, "generation": 1,
    "base_hp": 39, "base_atk": 52, "base_def": 43, "base_spa": 60, "base_spd": 50, "base_spe": 65,
}


def test_names():
    mon = battle.Combatant(species=SPECIES, level=5, ivs={})
    assert mon.name == "Charmander"
    assert mon.name_in("fr") == "Salamèche"
    assert mon.name_in("pl") == "Charmander"
    assert "Salamèche" in battle.who(mon, "fr")


if __name__ == "__main__":
    test_names()
    print("ok")
