"""Tests for auto heal: cheapest potion cover, team run, auto buy, settings API.

Run:  python3 tests/test_autoheal.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-autoheal-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import autoheal, battle, db, economy, i18n, seed  # noqa: E402
from app import healing as healing_mod  # noqa: E402
from app.api import team as team_api  # noqa: E402
from app.api import users as users_api  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


def http_status(fn, *args) -> int | None:
    try:
        fn(*args)
    except HTTPException as exc:
        return exc.status_code
    return None


db.init_db()
seed.seed_from_fallback()
alice_row = db.upsert_user("alice", "Alice")
alice = {**alice_row}
NOW = datetime.now().astimezone()
IVS = {k: 15 for k in ("hp", "atk", "def", "spa", "spd", "spe")}
PIKA = db.query_one("SELECT * FROM species WHERE name = 'rowlet'")  # highest HP in the fallback data


def make_owned(slot: int | None, missing: int | None, level: int = 100, user: str = "alice") -> int:
    """Owned Pokemon missing `missing` HP (None = full, 'fainted' via missing = max)."""
    oid = db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "current_hp, party_slot, caught_at, hp_updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (user, PIKA["id"], level, battle.exp_for_level(level, PIKA["growth_rate"]), 0,
         json.dumps(IVS), "hardy", None, slot, NOW.isoformat(), NOW.isoformat()),
    )
    if missing is not None:
        mx = battle.combatant_from_owned(db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (oid,))).max_hp
        db.execute("UPDATE owned_pokemon SET current_hp = ? WHERE id = ?", (max(0, mx - missing), oid))
    return oid


def hp_of(oid: int) -> tuple[int, int]:
    row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (oid,))
    mx = battle.combatant_from_owned(row).max_hp
    return (mx if row["current_hp"] is None else row["current_hp"]), mx


def set_stock(**items: int) -> None:
    db.execute("DELETE FROM inventory WHERE user_id = 'alice'")
    for key, n in items.items():
        db.execute("INSERT INTO inventory (item_key, user_id, quantity) VALUES (?,?,?)", (key, "alice", n))


def stock(key: str) -> int:
    row = db.query_one("SELECT quantity FROM inventory WHERE item_key = ? AND user_id = 'alice'", (key,))
    return row["quantity"] if row else 0


def reset_team() -> None:
    db.execute("DELETE FROM owned_pokemon WHERE user_id = 'alice'")


# ------------------------------------------------------- cheapest_cover
OPTS = lambda **have: [  # noqa: E731
    ("potion", 20, 200, have.get("potion", 0)), ("super_potion", 60, 600, have.get("super_potion", 0)),
    ("hyper_potion", 120, 1200, have.get("hyper_potion", 0)), ("max_potion", None, 2500, have.get("max_potion", 0)),
]
check("180 HP -> hyper + super, not two hypers",
      autoheal.cheapest_cover(180, OPTS(potion=9, super_potion=9, hyper_potion=9)) == {"hyper_potion": 1, "super_potion": 1})
check("nothing missing -> empty plan", autoheal.cheapest_cover(0, OPTS()) == {})
check("30 HP -> two potions (20+20 beats a super potion)",
      autoheal.cheapest_cover(30, OPTS(potion=5, super_potion=5, hyper_potion=5)) == {"potion": 2})
check("30 HP without potions -> one super potion",
      autoheal.cheapest_cover(30, OPTS(super_potion=1, hyper_potion=1)) == {"super_potion": 1})
check("only hypers left -> minimal overheal still works",
      autoheal.cheapest_cover(180, OPTS(hyper_potion=5)) == {"hyper_potion": 2})
check("stock limits the plan (fallback to smaller potions)",
      autoheal.cheapest_cover(180, OPTS(potion=10, hyper_potion=1)) == {"hyper_potion": 1, "potion": 3})
check("stock too small -> None", autoheal.cheapest_cover(180, OPTS(potion=2, super_potion=1)) is None)
check("large gap prefers a max potion over many hypers",
      autoheal.cheapest_cover(600, OPTS(hyper_potion=9, max_potion=1)) == {"max_potion": 1})
check("prefers fewer items at equal price (60 HP = 1 super, not 3 potions)",
      autoheal.cheapest_cover(60, OPTS(potion=9, super_potion=1)) == {"super_potion": 1})

check("potion catalog: only plain HP potions",
      [i["key"] for i in autoheal.potion_catalog()] == ["potion", "super_potion", "hyper_potion", "max_potion"],
      [i["key"] for i in autoheal.potion_catalog()])

# ----------------------------------------------------------- settings
check("defaults are off", db.user_auto_heal("alice") == {"auto_heal": False, "auto_buy_potions": False})
check("auto buy without auto heal -> 400",
      http_status(users_api.set_auto_heal, users_api.AutoHealChoice(auto_buy_potions=True), alice) == 400)
check("still off after rejected request", db.user_auto_heal("alice")["auto_buy_potions"] is False)
check("run while disabled -> 400", http_status(team_api.auto_heal, alice) == 400)
check("enable auto heal", users_api.set_auto_heal(users_api.AutoHealChoice(auto_heal=True), alice)
      == {"auto_heal": True, "auto_buy_potions": False})
check("enable auto buy", users_api.set_auto_heal(users_api.AutoHealChoice(auto_buy_potions=True), alice)
      == {"auto_heal": True, "auto_buy_potions": True})
check("disabling auto heal also disables auto buy",
      users_api.set_auto_heal(users_api.AutoHealChoice(auto_heal=False), alice)
      == {"auto_heal": False, "auto_buy_potions": False})
check("auto buy can't be smuggled in while turning auto heal off",
      http_status(users_api.set_auto_heal, users_api.AutoHealChoice(auto_heal=False, auto_buy_potions=True), alice) == 400)
check("/users/me reports the preferences",
      users_api.me(alice, False)["auto_heal"] is False and "auto_buy_potions" in users_api.me(alice, False))
check("team endpoint has no plan while disabled", team_api.get_team(alice)["auto_heal"]["plan"] is None)
users_api.set_auto_heal(users_api.AutoHealChoice(auto_heal=True), alice)

# ------------------------------------------------------------ team run
reset_team()
a = make_owned(1, 180)
b = make_owned(2, 20)
full = make_owned(3, None)
box = make_owned(None, 100)
set_stock(potion=3, super_potion=2, hyper_potion=2)

preview = team_api.get_team(alice)["auto_heal"]["plan"]
check("preview lists the potions and changes nothing",
      preview["heals"] == 2 and preview["items"] == {"potion": 1, "hyper_potion": 1, "super_potion": 1}
      and stock("hyper_potion") == 2 and hp_of(a)[0] < hp_of(a)[1], preview)

result = team_api.auto_heal(alice)
check("missing 180 used hyper + super", stock("hyper_potion") == 1 and stock("super_potion") == 1, result["used"])
check("missing 20 used a potion", stock("potion") == 2)
check("team is at full HP", hp_of(a)[0] == hp_of(a)[1] and hp_of(b)[0] == hp_of(b)[1])
check("box Pokemon untouched", hp_of(box)[0] < hp_of(box)[1])
check("no auto buy -> money unchanged", result["bought"] == {} and result["spent"] == 0)
check("message mentions the count", "2" in result["message"], result["message"])

again = team_api.auto_heal(alice)
check("second run with a healthy team uses nothing", again["used"] == {} and again["healed"] == 0, again["message"])

# ---------------------------------------- not enough / fainted / battle
reset_team()
small = make_owned(1, 40)
big = make_owned(2, 250)
dead = make_owned(3, 10**6)
set_stock(potion=2)
result = team_api.auto_heal(alice)
check("insufficient stock: the fixable Pokemon is healed, the other one untouched",
      hp_of(small)[0] == hp_of(small)[1] and hp_of(big)[0] < hp_of(big)[1] and stock("potion") == 0,
      result)
check("insufficient + fainted are reported",
      len(result["insufficient"]) == 1 and len(result["fainted"]) == 1 and hp_of(dead)[0] == 0)

reset_team()
mon = make_owned(1, 50)
set_stock(potion=5)
orig = healing_mod.has_ongoing_battle
healing_mod.has_ongoing_battle = lambda uid: True
check("no auto heal during a battle -> 409", http_status(team_api.auto_heal, alice) == 409)
healing_mod.has_ongoing_battle = orig
check("nothing consumed by the rejected call", stock("potion") == 5)

# ------------------------------------------------------------ auto buy
users_api.set_auto_heal(users_api.AutoHealChoice(auto_buy_potions=True), alice)
reset_team()
a = make_owned(1, 180)
set_stock(potion=1, super_potion=1, hyper_potion=1)
db.execute("UPDATE app_user SET money = 5000 WHERE id = 'alice'")
result = team_api.auto_heal(alice)
check("auto buy restores the used potions",
      stock("hyper_potion") == 1 and stock("super_potion") == 1 and stock("potion") == 1, result)
price = economy.unit_price(economy.catalog_by_key()["hyper_potion"]) + economy.unit_price(economy.catalog_by_key()["super_potion"])
check("auto buy charges the shop price", economy.balance("alice") == 5000 - price and result["spent"] == price, result["spent"])

make_owned(2, 180)
db.execute("UPDATE app_user SET money = ? WHERE id = 'alice'",
           (economy.unit_price(economy.catalog_by_key()["super_potion"]) + 5,))
result = team_api.auto_heal(alice)
check("auto buy stops at what the player can afford",
      sum(result["bought"].values()) == 1 and economy.balance("alice") == 5 and result["spent"] > 0, result["bought"])

users_api.set_auto_heal(users_api.AutoHealChoice(auto_buy_potions=False), alice)
make_owned(3, 180)
set_stock(hyper_potion=1, super_potion=1)
db.execute("UPDATE app_user SET money = 9999 WHERE id = 'alice'")
team_api.auto_heal(alice)
check("auto buy off -> potions stay used up", stock("hyper_potion") == 0 and economy.balance("alice") == 9999)

# --------------------------------------------------------------- i18n
en, de = (json.loads((ROOT / "pokemon_encounter/app/data/i18n" / f"{lang}.json").read_text(encoding="utf-8"))
          for lang in ("en", "de"))
keys = [k for k in en if k.startswith(("autoheal.", "team.auto_heal"))]
check("i18n keys exist in en and de", keys and all(k in de for k in keys), keys)
check("result message is translated",
      i18n.t("de", "autoheal.result.nothing") != i18n.t("en", "autoheal.result.nothing"))

print()
if failures:
    print(f"{len(failures)} FAIL: {failures}")
    sys.exit(1)
print("all ok")
