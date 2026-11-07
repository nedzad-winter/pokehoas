import sys
sys.path.insert(0, "pokemon_encounter")

from app import battle, economy, held_items, name_tables, tms


def test_item_names_server():
    great = name_tables.lookup("items", "great_ball", "fr")
    assert great and great != "Great Ball"
    assert battle.ball_label("great_ball", "fr") == great
    assert economy.item_label("great_ball", "fr") == great
    assert battle.ball_label("great_ball", "en") == "Great Ball"
    assert battle.ball_label("great_ball", "pl") == "Great Ball"
    assert held_items.label("leftovers", "fr") == name_tables.lookup("items", "leftovers", "fr")
    assert held_items.label("leftovers", "en") == "Leftovers"
    tm = next(iter(tms.all_tms()))
    move = tms.moves_mod.get(tms.move_key(tm))
    assert name_tables.lookup("moves", move["key"], "fr") in tms.label(tm, "fr")


if __name__ == "__main__":
    test_item_names_server()
    print("ok")
