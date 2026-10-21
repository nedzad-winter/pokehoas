"""Tests fuer das Freilassen eigener Pokemon aus der Box (nur Box, nicht Team)
und die Bonbon-Belohnung (1 Rare Candy pro 10 Level), sowie das Benutzen
eines Rare Candy (+1 Level).

Aufruf:  python3 tests/test_release.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-release-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import battle, db, economy, seed  # noqa: E402
from app.api import shop as shop_api  # noqa: E402
from app.api import team as team_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


def expect_http(label: str, status: int, fn, *args) -> None:
    try:
        fn(*args)
        check(label, False, "keine Exception")
    except HTTPException as exc:
        check(label, exc.status_code == status, f"{exc.status_code}: {exc.detail}")


NOW = datetime.now(timezone.utc).isoformat()
IVS = {k: 10 for k in ("hp", "atk", "def", "spa", "spd", "spe")}

db.init_db()
seed.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")


def add_owned(user_id: str, species_id: int, level: int, party_slot: int | None = None) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "party_slot, current_hp, caught_at) VALUES (?,?,?,?,0,?,?,?,?,?)",
        (user_id, species_id, level, battle.exp_for_level(level, "medium_slow"), json.dumps(IVS),
         "hardy", party_slot, None, NOW),
    )


def owned(owned_id: int) -> dict:
    return db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))


def inventory_of(user_id: str, item_key: str) -> int:
    row = db.query_one(
        "SELECT quantity FROM inventory WHERE user_id = ? AND item_key = ?", (user_id, item_key)
    )
    return int(row["quantity"]) if row else 0


# --------------------------------------------------------- Katalog-Eintrag
item = economy.catalog_by_key().get("rare_candy")
check("Rare Candy ist im Katalog", item is not None, item)
check("Rare Candy ist nicht kaeuflich", item is not None and not economy.is_buyable(item))


# ------------------------------------------------------------- Freilassen
expect_http("Fremdes Pokémon -> 403", 403, team_api.release, add_owned("bob", 1, 5), alice)

in_team = add_owned("alice", 1, 25, party_slot=1)
expect_http("Aus dem Team -> 400 (nur Box)", 400, team_api.release, in_team, alice)
check("Im Team gesperrter Versuch aendert nichts", owned(in_team) is not None)

low_level = add_owned("alice", 1, 9)
res_low = team_api.release(low_level, alice)
check("Lv. 9: kein Bonbon (9 // 10 == 0)", res_low["rare_candy_awarded"] == 0, res_low)
check("Lv. 9: trotzdem freigelassen", owned(low_level) is None)
check("Lv. 9: kein Bonbon im Inventar angelegt", inventory_of("alice", "rare_candy") == 0)

mid_level = add_owned("alice", 1, 25)
before_candy = inventory_of("alice", "rare_candy")
res_mid = team_api.release(mid_level, alice)
check("Lv. 25: 2 Bonbons (25 // 10)", res_mid["rare_candy_awarded"] == 2, res_mid)
check("Lv. 25: Pokémon aus der Box entfernt", owned(mid_level) is None)
check("Lv. 25: Bonbons im Inventar gutgeschrieben",
      inventory_of("alice", "rare_candy") == before_candy + 2)

exact_level = add_owned("alice", 1, 10)
res_exact = team_api.release(exact_level, alice)
check("Lv. 10: genau 1 Bonbon", res_exact["rare_candy_awarded"] == 1, res_exact)

expect_http("Bereits geloescht -> 404", 404, team_api.release, low_level, alice)

# Laufender Kampf sperrt (wie alle Team-Aenderungen).
from app import healing  # noqa: E402
orig_has_battle = healing.has_ongoing_battle
healing.has_ongoing_battle = lambda user_id: True
locked_box = add_owned("alice", 1, 5)
expect_http("Im Kampf gesperrt -> 409", 409, team_api.release, locked_box, alice)
healing.has_ongoing_battle = orig_has_battle
check("Gesperrter Versuch aendert nichts", owned(locked_box) is not None)


# ------------------------------------------- Mehrfach-Freilassen (Auswahl)
from app.api.team import ReleaseIn  # noqa: E402

expect_http("Mehrfach: leere Auswahl -> 400", 400, team_api.release_many, ReleaseIn(ids=[]), alice)

multi_a = add_owned("alice", 1, 10)
multi_b = add_owned("alice", 1, 25)
multi_c = add_owned("alice", 1, 9)
foreign = add_owned("bob", 1, 5)
multi_team = add_owned("alice", 1, 30, party_slot=3)
expect_http("Mehrfach: fremdes Pokémon in der Auswahl -> 403", 403, team_api.release_many,
            ReleaseIn(ids=[multi_a, foreign]), alice)
check("Mehrfach: 403 gibt nichts frei (alles oder nichts)", owned(multi_a) is not None)
expect_http("Mehrfach: Team-Pokémon in der Auswahl -> 400", 400, team_api.release_many,
            ReleaseIn(ids=[multi_b, multi_team]), alice)
check("Mehrfach: 400 gibt nichts frei", owned(multi_b) is not None and owned(multi_team) is not None)
expect_http("Mehrfach: unbekannte ID -> 404", 404, team_api.release_many,
            ReleaseIn(ids=[multi_a, 999999]), alice)

healing.has_ongoing_battle = lambda user_id: True
expect_http("Mehrfach: im Kampf gesperrt -> 409", 409, team_api.release_many,
            ReleaseIn(ids=[multi_a]), alice)
healing.has_ongoing_battle = orig_has_battle

candy_before_multi = inventory_of("alice", "rare_candy")
res_multi = team_api.release_many(ReleaseIn(ids=[multi_a, multi_b, multi_c, multi_a]), alice)
check("Mehrfach: Duplikate in der Auswahl werden ignoriert",
      [e["id"] for e in res_multi["released"]] == [multi_a, multi_b, multi_c], res_multi)
check("Mehrfach: alle ausgewählten Pokémon sind weg",
      owned(multi_a) is None and owned(multi_b) is None and owned(multi_c) is None)
check("Mehrfach: Team-Pokémon bleibt unberührt", owned(multi_team) is not None)
check("Mehrfach: Bonbons aller Pokémon gesammelt (1 + 2 + 0)",
      res_multi["rare_candy_awarded"] == 3 and inventory_of("alice", "rare_candy") == candy_before_multi + 3,
      res_multi)
check("Mehrfach: Antwort enthält Name und Level je Pokémon",
      all(e["name"] and e["level"] for e in res_multi["released"]), res_multi)
check("Mehrfach: fremdes Pokémon bleibt unberührt", owned(foreign) is not None)

# Einzel-Freilassen nutzt denselben Helfer und liefert weiterhin die alte Form.
single_again = add_owned("alice", 1, 12)
res_single = team_api.release(single_again, alice)
check("Einzel-Freilassen: Antwort unveraendert (ok, id, name, level, Bonbons)",
      res_single["ok"] is True and res_single["id"] == single_again and res_single["level"] == 12
      and res_single["rare_candy_awarded"] == 1 and res_single["name"], res_single)


# ------------------------------------------------------ Rare Candy nutzen
candy_total_before = inventory_of("alice", "rare_candy")
check("Alice hat Bonbons aus den Freilassungen", candy_total_before >= 3, candy_total_before)

candy_target = add_owned("alice", 1, 20, party_slot=2)
db.execute("UPDATE owned_pokemon SET current_hp = 1 WHERE id = ?", (candy_target,))
before = owned(candy_target)
species = db.query_one("SELECT * FROM species WHERE id = 1")
old_max = battle.stat_hp(species["base_hp"], IVS["hp"], 20)
new_max = battle.stat_hp(species["base_hp"], IVS["hp"], 21)

result = shop_api.use_item(candy_target, shop_api.UseItemIn(item_key="rare_candy"), alice)
after = owned(candy_target)
check("Rare Candy: Level +1", after["level"] == 21, after["level"])
check("Rare Candy: EXP passt zum neuen Level",
      after["exp"] == battle.exp_for_level(21, species["growth_rate"]), after["exp"])
check("Rare Candy: KP-Anteil bleibt (nicht ohnmaechtig)",
      after["current_hp"] == min(new_max, 1 + max(0, new_max - old_max)), after["current_hp"])
check("Rare Candy: aus dem Inventar verbraucht",
      inventory_of("alice", "rare_candy") == candy_total_before - 1)
check("Rare Candy: Antwort hat lokalisierte Meldung", "message" in result and result["message"], result)

# Ohnmaechtiges Pokémon bleibt bei 0 KP (kein Wiederbeleben durch Candy).
db.execute("UPDATE owned_pokemon SET level = 5, current_hp = 0 WHERE id = ?", (candy_target,))
shop_api.use_item(candy_target, shop_api.UseItemIn(item_key="rare_candy"), alice)
check("Rare Candy: ohnmaechtig bleibt ohnmaechtig", owned(candy_target)["current_hp"] == 0)

# Kein Bonbon mehr -> Fehler, Level unveraendert.
db.execute("DELETE FROM inventory WHERE user_id = 'alice' AND item_key = 'rare_candy'")
level_before = owned(candy_target)["level"]
expect_http("Kein Bonbon im Beutel -> 409", 409, shop_api.use_item,
            candy_target, shop_api.UseItemIn(item_key="rare_candy"), alice)
check("Ohne Bonbon aendert sich nichts", owned(candy_target)["level"] == level_before)

# Maximallevel -> Fehler.
db.execute(
    "INSERT INTO inventory (item_key, user_id, quantity) VALUES ('rare_candy','alice',5) "
    "ON CONFLICT(item_key, user_id) DO UPDATE SET quantity = quantity + excluded.quantity"
)
max_mon = add_owned("alice", 1, 100)
expect_http("Maximallevel -> 400", 400, shop_api.use_item,
            max_mon, shop_api.UseItemIn(item_key="rare_candy"), alice)

# targets_for/rare_candy_targets: max_mon (Lv. 100) taucht nicht auf.
targets = economy.rare_candy_targets("alice")
check("rare_candy_targets: Lv.-100-Pokémon ausgeschlossen",
      max_mon not in [t["owned_id"] for t in targets], targets)


# -------------------------------------------------------------------- Ende
print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen: {failures}")
    sys.exit(1)
print("Alle Tests bestanden.")
