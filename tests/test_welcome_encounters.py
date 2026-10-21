"""Tests fuer die Willkommens-Encounter nach der Starter-Wahl.

Ein frisch gestarteter Spieler bekommt beim Waehlen des Starters drei
Level-2-Encounter in die eigene Warteschlange: Taubsi (16), Ratfratz (19),
Raupi (10). Fehlt eine dieser Spezies noch im Pokedex (Hintergrund-Seeding),
wird sie uebersprungen statt die Starter-Auswahl zu blockieren.

Aufruf:  python3 tests/test_welcome_encounters.py
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-welcome-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import db, seed  # noqa: E402
from app.api import encounters as encounters_api  # noqa: E402
from app.api import users as users_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def expect_http(label: str, status: int, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
        check(label, False, "keine Exception")
    except HTTPException as exc:
        check(label, exc.status_code == status, f"{exc.status_code}: {exc.detail}")


random.seed(int(os.environ.get("TEST_SEED", "4711")))
db.init_db()
seed.seed_from_fallback()


def make_species(species_id: int, name: str, type1: str = "normal") -> dict:
    db.execute(
        """
        INSERT INTO species (id, name, name_de, type1, generation, catch_rate,
            base_exp, growth_rate, base_hp, base_atk, base_def, base_spa, base_spd, base_spe)
        VALUES (?,?,?,?,1,45,64,'medium_fast',100,80,80,80,80,80)
        ON CONFLICT(id) DO NOTHING
        """,
        (species_id, name, name, type1),
    )
    return db.query_one("SELECT * FROM species WHERE id = ?", (species_id,))


def queue(user: dict) -> list[dict]:
    return encounters_api.list_encounters("waiting", 100, user)


# ---- Erst nur Taubsi (16) und Ratfratz (19) geseedet, Raupi (10) fehlt noch
make_species(16, "pidgey", "normal")
make_species(19, "rattata", "normal")

alice = db.upsert_user("alice", "Alice")
users_api.choose_starter(users_api.StarterChoice(species_id=1), alice)

alice_queue = queue(db.get_user("alice"))
alice_species = sorted(e["species_id"] for e in alice_queue)
check("Fehlende Spezies (Raupi) wird uebersprungen, nicht blockiert",
      alice_species == [16, 19], str(alice_species))
check("Willkommens-Encounter stehen auf Level 2",
      all(e["level"] == 2 for e in alice_queue), str([e["level"] for e in alice_queue]))
check("Willkommens-Encounter gehoeren nur Alice",
      all(e["user_id"] == "alice" for e in alice_queue))
check("Quelle ist als Willkommens-Encounter erkennbar",
      all(e["source_entity"] == "welcome" for e in alice_queue))

expect_http("Starter kann nicht zweimal gewaehlt werden", 409,
            users_api.choose_starter, users_api.StarterChoice(species_id=1), db.get_user("alice"))
check("Erneuter Versuch legt keine weiteren Encounter an",
      len(queue(db.get_user("alice"))) == 2)


# ---- Fuer den naechsten Spieler ist auch Raupi (10) geseedet
make_species(10, "caterpie", "bug")

bob = db.upsert_user("bob", "Bob")
users_api.choose_starter(users_api.StarterChoice(species_id=4), bob)

bob_queue = queue(db.get_user("bob"))
bob_species = sorted(e["species_id"] for e in bob_queue)
check("Mit vollstaendigem Pokedex bekommt der Spieler alle drei",
      bob_species == [10, 16, 19], str(bob_species))
check("Bobs Willkommens-Encounter stecken nicht in Alices Warteschlange",
      not any(e["id"] in [x["id"] for x in bob_queue] for e in alice_queue))


# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
