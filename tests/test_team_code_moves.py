import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "pokemon_encounter")

from app import db, moves as moves_mod, team_code


def test_moves():
    db.connect(Path(tempfile.mkdtemp()) / "test.db")
    db.init_db()
    db.execute("INSERT INTO species (id, name, name_en, type1, names) VALUES (2, 'ivysaur', 'Ivysaur', 'grass', ?)",
               (json.dumps({"fr": "Herbizarre"}),))
    species = db.query_one("SELECT * FROM species WHERE id = 2")

    # seed_bomb: learned at level 20 by level-up, but a TM only teaches it from level 25
    entry = [2, 22, [0] * 6, None, None, ["seed_bomb"]]
    assert team_code._mon_from_entry(entry, "fr")["moves"] == ["seed_bomb"]

    learn = moves_mod.learnset(species)
    bad = next(k for k, m in moves_mod.all_moves().items()
               if m["min_level"] > 22 and k not in learn["level"] and k not in learn["tm"] and k not in learn["egg"])
    try:
        team_code._mon_from_entry([2, 22, [0] * 6, None, None, [bad]], "fr")
    except team_code.CodeError as exc:
        assert "Herbizarre" in str(exc), str(exc)
        assert bad not in str(exc), str(exc)
    else:
        raise AssertionError("une attaque inconnue aurait dû être refusée")


if __name__ == "__main__":
    test_moves()
    print("ok")
