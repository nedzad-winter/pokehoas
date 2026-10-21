"""Tests for Pokémon trading between household members: the request ->
counter-offer -> final decision handshake, every way to end it, validation of
tradable Pokémon, the atomic swap, and cleanup of finished trades.

Run:  python3 tests/test_trading.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-trading-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"

from app import abilities, battle, db, seed, spawn, trading  # noqa: E402
from app.api import admin as admin_api  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


def expect_error(label: str, code: int, fn, *args) -> None:
    try:
        fn(*args)
    except HTTPException as exc:
        check(label, exc.status_code == code, f"{exc.status_code}: {exc.detail}")
    else:
        check(label, False, "no error")


# ---------------------------------------------------------------- Setup
db.init_db()
seed.seed_from_fallback()
abilities.seed_from_fallback()
alice = db.upsert_user("alice", "Alice")
bob = db.upsert_user("bob", "Bob")
carol = db.upsert_user("carol", "Carol")
now = trading._now()

pika = db.query_one("SELECT * FROM species WHERE name = 'pikachu'")
karp = db.query_one("SELECT * FROM species WHERE name = 'magikarp'")


def give(user_id: str, species: dict, level: int, slot: int | None = None, **extra) -> int:
    return db.execute(
        "INSERT INTO owned_pokemon (user_id, species_id, level, exp, shiny, ivs, nature, "
        "ability, held_item, current_hp, caught_at, party_slot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, species["id"], level, battle.exp_for_level(level), extra.get("shiny", 0),
         json.dumps({key: 31 for key in spawn.STAT_KEYS}), "hardy",
         extra.get("ability"), extra.get("held_item"),
         battle.stat_hp(species["base_hp"], 31, level), now.isoformat(), slot),
    )


def owner_of(pokemon_id: int) -> str | None:
    row = db.query_one("SELECT user_id FROM owned_pokemon WHERE id = ?", (pokemon_id,))
    return row["user_id"] if row else None


def status_of(trade_id: int) -> str:
    return db.query_one("SELECT status FROM trade WHERE id = ?", (trade_id,))["status"]


a_pika = give("alice", pika, 30, held_item="leftovers")
a_second = give("alice", pika, 12)
a_party = give("alice", pika, 8, slot=1)
b_karp = give("bob", karp, 5, shiny=1)
b_second = give("bob", karp, 6)
b_party = give("bob", karp, 7, slot=1)

# ------------------------------------------------------ Routes exist
paths = {(method, route.path) for route in app.routes for method in getattr(route, "methods", [])}
for method, path in (("GET", "/api/trades"), ("GET", "/api/trades/pending"),
                     ("GET", "/api/trades/tradable"), ("POST", "/api/trades"),
                     ("POST", "/api/trades/{trade_id}/offer"), ("POST", "/api/trades/{trade_id}/accept"),
                     ("POST", "/api/trades/{trade_id}/decline"), ("POST", "/api/trades/{trade_id}/cancel"),
                     ("DELETE", "/api/trades/{trade_id}")):
    check(f"Route {method} {path} is registered", (method, path) in paths)

# ------------------------------------------- Tradable Pokémon / request
tradable = {p["id"] for p in trading.tradable_pokemon("alice")}
check("Only box Pokémon are tradable", tradable == {a_pika, a_second}, tradable)
db.execute("UPDATE owned_pokemon SET favorite = 1 WHERE id = ?", (a_pika,))
picked = {p["id"]: p for p in trading.tradable_pokemon("alice")}
sp = picked[a_pika]["species"]
check("Tradable list carries the box filter fields (generation, stage, rarity)",
      sp["generation"] == 1 and sp["stage"] == 1 and sp["rarity_tier"] == "common", sp)
check("Tradable list carries the favorite flag",
      picked[a_pika]["favorite"] is True and picked[a_second]["favorite"] is False)
db.execute("UPDATE owned_pokemon SET favorite = 0 WHERE id = ?", (a_pika,))
expect_error("Team member can't be offered", 400, trading.request_trade, "alice", "bob", a_party)
expect_error("Foreign Pokémon can't be offered", 403, trading.request_trade, "alice", "bob", b_karp)
expect_error("Unknown Pokémon is rejected", 404, trading.request_trade, "alice", "bob", 99999)
expect_error("Trading with yourself is rejected", 400, trading.request_trade, "alice", "alice", a_pika)
expect_error("Unknown partner is rejected", 404, trading.request_trade, "alice", "nobody", a_pika)

db.execute("UPDATE app_user SET buddy_pokemon_id = ? WHERE id = 'alice'", (a_second,))
check("Buddy is not tradable", {p["id"] for p in trading.tradable_pokemon("alice")} == {a_pika})
expect_error("Buddy can't be offered", 400, trading.request_trade, "alice", "bob", a_second)
db.execute("UPDATE app_user SET buddy_pokemon_id = NULL WHERE id = 'alice'")

db.execute("INSERT INTO daycare (user_id, parent_a, parent_b, since) VALUES ('alice', ?, ?, ?)",
           (a_second, a_party, now.isoformat()))
expect_error("Day care parent can't be offered", 400, trading.request_trade, "alice", "bob", a_second)
db.execute("DELETE FROM daycare WHERE user_id = 'alice'")

view = trading.request_trade("alice", "bob", a_pika)
trade_id = view["id"]
check("New request is 'requested' and awaits the partner",
      view["status"] == "requested" and view["role"] == "initiator" and not view["awaiting_me"], view)
check("Initiator can only cancel a fresh request",
      view["actions"] == {"offer": False, "accept": False, "decline": False, "cancel": True, "delete": False},
      view["actions"])
expect_error("Same Pokémon to same partner twice is rejected", 409, trading.request_trade, "alice", "bob", a_pika)
check("Same Pokémon to another partner is allowed", trading.request_trade("alice", "carol", a_pika)["status"] == "requested")

bob_view = next(t for t in trading.overview("bob")["trades"] if t["id"] == trade_id)
check("Partner sees the request, awaiting them",
      bob_view["role"] == "partner" and bob_view["awaiting_me"] and bob_view["other_name"] == "Alice")
check("Partner sees the offered Pokémon (species, level, IV%, held item)",
      bob_view["initiator_pokemon"]["species"]["name"] == "pikachu"
      and bob_view["initiator_pokemon"]["level"] == 30
      and bob_view["initiator_pokemon"]["iv_percent"] == 100
      and bob_view["initiator_pokemon"]["held_item_info"] is not None, bob_view["initiator_pokemon"])
check("Partner Pokémon is still empty", bob_view["partner_pokemon"] is None)
check("Badge counts the request for Bob", trading.pending_count("bob") == 1)
check("Badge counts nothing for Alice yet", trading.pending_count("alice") == 0)
check("Third user sees no trades", [t for t in trading.overview("carol")["trades"] if t["id"] == trade_id] == [])
check("Overview lists other users as partners",
      {p["user_id"] for p in trading.overview("alice")["partners"]} == {"bob", "carol"})
expect_error("Third user can't read/act on the trade", 404, trading.offer, "carol", trade_id, 1)

# ------------------------------------------------- Wrong role / state
expect_error("Initiator can't offer on their own request", 409, trading.offer, "alice", trade_id, a_second)
expect_error("Accept before an offer exists is rejected", 409, trading.accept, "alice", trade_id)
expect_error("Partner can't accept (initiator decides)", 409, trading.accept, "bob", trade_id)
expect_error("Partner can't withdraw a request they received", 409, trading.cancel, "bob", trade_id)
expect_error("Open trade can't be removed", 409, trading.delete, "alice", trade_id)
expect_error("Partner must offer own Pokémon", 403, trading.offer, "bob", trade_id, a_second)
expect_error("Partner can't offer a team member", 400, trading.offer, "bob", trade_id, b_party)

# --------------------------------------------------------- Counter-offer
view = trading.offer("bob", trade_id, b_karp)
check("Counter-offer moves the trade to 'offered'", view["status"] == "offered" and view["partner_pokemon"]["id"] == b_karp)
check("Shiny flag is visible in the offer", view["partner_pokemon"]["shiny"] is True)
check("Alice is now awaited", trading.pending_count("alice") == 1 and trading.pending_count("bob") == 0)
expect_error("Offer can't be made twice", 409, trading.offer, "bob", trade_id, b_second)
expect_error("Partner can't accept their own offer", 409, trading.accept, "bob", trade_id)
check("Nothing moved yet", owner_of(a_pika) == "alice" and owner_of(b_karp) == "bob")

# The other request with Carol mentions a_pika too: it must die on completion.
carol_trade = next(t["id"] for t in trading.overview("alice")["trades"] if t["other_name"] == "Carol")

# ---------------------------------------------------- Final acceptance
view = trading.accept("alice", trade_id)
check("Accepting completes the trade", view["status"] == "completed" and view["actions"]["delete"], view["status"])
check("Pokémon swapped owners", owner_of(a_pika) == "bob" and owner_of(b_karp) == "alice")
row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (a_pika,))
check("Level, held item and shiny flag travel with the Pokémon; it lands in the box",
      row["level"] == 30 and row["held_item"] == "leftovers" and row["party_slot"] is None, row)
check("Receivers' Pokédex counts the new species as caught",
      db.query_one("SELECT caught_count FROM dex_entry WHERE user_id = 'bob' AND species_id = ?", (pika["id"],))["caught_count"] == 1
      and db.query_one("SELECT shiny_caught FROM dex_entry WHERE user_id = 'alice' AND species_id = ?", (karp["id"],))["shiny_caught"] == 1)
check("Other open trade with the traded Pokémon was cancelled", status_of(carol_trade) == "cancelled")
check("Nobody is awaited any more", trading.pending_count("alice") == 0 and trading.pending_count("bob") == 0)
expect_error("Completed trade can't be accepted again", 409, trading.accept, "alice", trade_id)
expect_error("Completed trade can't be declined", 409, trading.decline, "bob", trade_id)

# ------------------------------------------------ Decline / reject / cancel
t1 = trading.request_trade("alice", "bob", a_second)["id"]
view = trading.decline("bob", t1)
check("Partner refusing the request -> 'declined'", view["status"] == "declined" and status_of(t1) == "declined")
check("Nothing moved after decline", owner_of(a_second) == "alice")

t2 = trading.request_trade("alice", "bob", a_second)["id"]
trading.offer("bob", t2, b_second)
expect_error("Partner can't decline their own counter-offer", 409, trading.decline, "bob", t2)
check("Partner may withdraw the counter-offer", trading.cancel("bob", t2)["status"] == "cancelled")

t3 = trading.request_trade("alice", "bob", a_second)["id"]
trading.offer("bob", t3, b_second)
expect_error("Initiator can't withdraw once an offer exists", 409, trading.cancel, "alice", t3)
view = trading.decline("alice", t3)
check("Initiator refusing the final offer -> 'rejected'", view["status"] == "rejected")
check("Nothing moved after rejection", owner_of(a_second) == "alice" and owner_of(b_second) == "bob")

t4 = trading.request_trade("alice", "bob", a_second)["id"]
check("Initiator may withdraw a request", trading.cancel("alice", t4)["status"] == "cancelled")

# ------------------------------------ Revalidation at completion time
t5 = trading.request_trade("alice", "bob", a_second)["id"]
trading.offer("bob", t5, b_second)
db.execute("DELETE FROM owned_pokemon WHERE id = ?", (b_second,))  # released meanwhile
alice_view = next(t for t in trading.overview("alice")["trades"] if t["id"] == t5)
check("Vanished Pokémon is flagged as unavailable", alice_view["unavailable"] is True)
expect_error("Accepting with a vanished Pokémon fails", 409, trading.accept, "alice", t5)
check("Nothing moved after failed accept", owner_of(a_second) == "alice" and status_of(t5) == "offered")
trading.decline("alice", t5)

b_third = give("bob", karp, 9)
t6 = trading.request_trade("alice", "bob", a_second)["id"]
trading.offer("bob", t6, b_third)
db.execute("UPDATE owned_pokemon SET party_slot = 2 WHERE id = ?", (a_second,))  # moved into the team
expect_error("Accepting with an offered Pokémon now in the team fails", 409, trading.accept, "alice", t6)
check("Ownership unchanged after failed accept", owner_of(a_second) == "alice" and owner_of(b_third) == "bob")
db.execute("UPDATE owned_pokemon SET party_slot = NULL WHERE id = ?", (a_second,))
db.execute("UPDATE app_user SET buddy_pokemon_id = ? WHERE id = 'bob'", (b_third,))
expect_error("Accepting with a Pokémon that became a buddy fails", 409, trading.accept, "alice", t6)
db.execute("UPDATE app_user SET buddy_pokemon_id = NULL WHERE id = 'bob'")
check("Same trade completes once the Pokémon is tradable again",
      trading.accept("alice", t6)["status"] == "completed" and owner_of(a_second) == "bob" and owner_of(b_third) == "alice")

# ------------------------------------------------- Removing / purging
check("Alice sees her finished trades", any(t["id"] == trade_id for t in trading.overview("alice")["trades"]))
check("Open trades are sorted before finished ones",
      [t["actions"]["delete"] for t in trading.overview("alice")["trades"]]
      == sorted(t["actions"]["delete"] for t in trading.overview("alice")["trades"]))
trading.delete("alice", trade_id)
check("Removed trade disappears from Alice's view only",
      not any(t["id"] == trade_id for t in trading.overview("alice")["trades"])
      and any(t["id"] == trade_id for t in trading.overview("bob")["trades"]))
expect_error("Third user can't remove foreign trades", 404, trading.delete, "carol", trade_id)
trading.delete("bob", trade_id)
check("Trade row is deleted once both removed it", db.query_one("SELECT 1 FROM trade WHERE id = ?", (trade_id,)) is None)

check("Purge keeps finished trades of today", trading.purge_closed() == 0 and status_of(t1) == "declined")
yesterday = (now - timedelta(days=1)).isoformat()
db.execute("UPDATE trade SET closed_at = ? WHERE id IN (?, ?)", (yesterday, t1, t2))
open_id = trading.request_trade("bob", "alice", a_pika)["id"]  # a_pika belongs to Bob after the first trade
purged = trading.purge_closed()
check("Finished trades from yesterday are purged the next day", purged == 2
      and db.query_one("SELECT 1 FROM trade WHERE id IN (?, ?)", (t1, t2)) is None, purged)
check("Open trades are never purged", status_of(open_id) == "requested")
check("Overview purges lazily too", (db.execute("UPDATE trade SET closed_at = ? WHERE id = ?", (yesterday, t3)),
                                     not any(t["id"] == t3 for t in trading.overview("alice")["trades"]))[1])

# ------------------------------------------------------------ Admin wipe
admin_api.wipe_user("bob")
check("Wiping a user removes their trades", db.query("SELECT id FROM trade WHERE initiator_id = 'bob' OR partner_id = 'bob'") == [])

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(" -", label)
    sys.exit(1)
print("All trading tests passed.")
