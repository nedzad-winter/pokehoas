import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "pokemon_encounter")

from app import db


def test_tables():
    db.connect(Path(tempfile.mkdtemp()) / "test.db")
    db.init_db()
    db.execute(
        "INSERT INTO species (id, name, name_en, type1, names) VALUES (4, 'charmander', 'Charmander', 'fire', ?)",
        (json.dumps({"fr": "Salamèche", "de": "Glumanda"}),),
    )
    tables = db.species_name_tables("fr")
    assert tables["names"][4] == "Salamèche"
    assert tables["by_en"]["Charmander"] == "Salamèche"
    assert db.species_name_tables("ko") == {"names": {}, "by_en": {}}


if __name__ == "__main__":
    test_tables()
    print("ok")