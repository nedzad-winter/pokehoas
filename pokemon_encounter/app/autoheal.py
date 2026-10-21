"""Auto heal: heals the whole team with as few (cheap) potions as possible.

Per Pokemon the missing HP is covered with the cheapest combination of the
plain HP potions in the bag (`heal` is a number or "full", no revive, no status
cure - so Potion, Super/Hyper/Max Potion; Full Restore is deliberately left to
manual use). A Pokemon missing 180 HP gets a Hyper Potion + a Super Potion
instead of two Hyper Potions. Ranking: lowest total price first (= least
overheal, as the numeric potions cost the same per HP), then the fewest items.

* Only team members (not the box) are considered; fainted ones are skipped
  (a potion cannot revive).
* Pokemon are processed by ascending missing HP, so a small stock of potions
  heals as many Pokemon completely as possible. A Pokemon the stock cannot fully
  heal is left untouched (`insufficient`) instead of draining the bag for a
  partial heal.
* With `app_user.auto_buy_potions` the potions used are bought again afterwards
  (as many as the player can afford) so the bag stays stocked. That option only
  exists together with `app_user.auto_heal`, which also gates the endpoint.
"""

from __future__ import annotations

from typing import Any

from . import battle, db, economy, healing, i18n, medicine
from .economy import EconomyError

_INF = (float("inf"), 0)


def potion_catalog() -> list[dict[str, Any]]:
    """Plain HP potions, ascending by heal amount ("full" last)."""
    items = [item for item in medicine.catalog().values()
             if item.get("heal") is not None and not item.get("revive") and not item.get("cures")]
    return sorted(items, key=lambda i: float("inf") if i["heal"] == "full" else int(i["heal"]))


def cheapest_cover(
    missing: int, options: list[tuple[str, int | None, int, int]],
) -> dict[str, int] | None:
    """Cheapest multiset of potions healing at least `missing` HP.

    `options`: (key, heal or None for "full", unit price, available). Minimizes
    (total price, item count); returns {key: count}, or None when the stock
    cannot cover `missing`. Bounded knapsack over the covered HP (0..missing).
    """
    if missing <= 0:
        return {}
    dp = [_INF] * (missing + 1)
    dp[0] = (0, 0)
    choices: list[list[tuple[int, int] | None]] = []
    for key, heal, price, available in options:
        heal = missing if heal is None else max(1, int(heal))
        max_copies = min(available, -(-missing // heal))
        new = list(dp)
        choice: list[tuple[int, int] | None] = [None] * (missing + 1)  # (previous HP, copies)
        for start in range(missing):
            if dp[start] is _INF:
                continue
            for copies in range(1, max_copies + 1):
                target = min(missing, start + copies * heal)
                cand = (dp[start][0] + copies * price, dp[start][1] + copies)
                if cand < new[target]:
                    new[target], choice[target] = cand, (start, copies)
        dp = new
        choices.append(choice)
    if dp[missing] is _INF:
        return None
    plan: dict[str, int] = {}
    at = missing
    for (key, *_), choice in zip(reversed(options), reversed(choices)):
        if choice[at] is not None:
            at, copies = choice[at]
            plan[key] = copies
    return plan


def _stock(user_id: str) -> dict[str, int]:
    return {row["item_key"]: int(row["quantity"]) for row in db.query(
        "SELECT item_key, quantity FROM inventory WHERE user_id = ? AND quantity > 0", (user_id,))}


def plan_team(user_id: str) -> dict[str, Any]:
    """Dry run: which potions would heal the team. Does not change anything
    (regeneration is applied by the caller). `items` is the total per potion."""
    potions = potion_catalog()
    stock = _stock(user_id)
    remaining = {item["key"]: stock.get(item["key"], 0) for item in potions}
    rows = db.query(
        "SELECT * FROM owned_pokemon WHERE user_id = ? AND party_slot IS NOT NULL "
        "ORDER BY party_slot", (user_id,))
    hurt, fainted = [], []
    for row in rows:
        combatant = battle.combatant_from_owned(row)
        current = combatant.max_hp if row["current_hp"] is None else min(int(row["current_hp"]), combatant.max_hp)
        entry = {"owned_id": row["id"], "name": row.get("nickname") or combatant.name,
                 "missing": combatant.max_hp - current}
        if current <= 0:
            fainted.append(entry)
        elif entry["missing"] > 0:
            hurt.append(entry)

    pokemon, insufficient, totals = [], [], {}
    for entry in sorted(hurt, key=lambda e: e["missing"]):
        options = [(item["key"], None if item["heal"] == "full" else int(item["heal"]),
                    economy.unit_price(item), remaining[item["key"]]) for item in potions]
        cover = cheapest_cover(entry["missing"], options)
        if cover is None:
            insufficient.append(entry)
            continue
        for key, count in cover.items():
            remaining[key] -= count
            totals[key] = totals.get(key, 0) + count
        pokemon.append({**entry, "items": cover})
    return {"pokemon": pokemon, "insufficient": insufficient, "fainted": fainted, "items": totals}


def _items_text(lang: str, items: dict[str, int]) -> str:
    return ", ".join(f"{count}× {economy.item_label(key, lang)}" for key, count in items.items())


def status(user_id: str) -> dict[str, Any]:
    """Preferences plus a preview of the plan (for the team view button)."""
    prefs = db.user_auto_heal(user_id)
    if not prefs["auto_heal"]:
        return {**prefs, "plan": None}
    plan = plan_team(user_id)
    return {**prefs, "plan": {
        "items": plan["items"], "items_text": _items_text(db.user_language(user_id), plan["items"]),
        "heals": len(plan["pokemon"]),
        "insufficient": len(plan["insufficient"]), "fainted": len(plan["fainted"]),
    }}


def run(user_id: str, *, from_rule: bool = False) -> dict[str, Any]:
    """Heals the team with the planned potions and, if enabled, buys the used
    ones again. Raises EconomyError (-> HTTP status) when disabled or in battle.

    `from_rule`: an auto rule asked for the heal (see auto_rules.py) - the rule
    is the player's opt-in, so the Auto heal switch isn't required; re-buying
    still follows the player's "auto buy used potions" preference."""
    lang = db.user_language(user_id)
    prefs = db.user_auto_heal(user_id)
    if not prefs["auto_heal"] and not from_rule:
        raise EconomyError(400, i18n.t(lang, "autoheal.error.disabled"))
    if healing.has_ongoing_battle(user_id):
        raise EconomyError(409, i18n.t(lang, "error.cant_heal_in_battle"))

    healing.apply_regen(user_id)  # Plan on the real HP, like a manual potion
    plan = plan_team(user_id)
    used: dict[str, int] = {}
    healed = 0
    for entry in plan["pokemon"]:
        try:
            for key, count in entry["items"].items():
                for _ in range(count):
                    medicine.use_out_of_battle(user_id, entry["owned_id"], key)
                    used[key] = used.get(key, 0) + 1
            healed += 1
        except EconomyError:
            continue  # Changed underneath us (e.g. item gone) - carry on with the rest

    bought: dict[str, int] = {}
    spent = 0
    if prefs["auto_buy_potions"]:
        for key, count in used.items():
            price = economy.unit_price(economy.catalog_by_key()[key])
            count = min(count, economy.balance(user_id) // price, economy.MAX_PURCHASE_QUANTITY)
            if count < 1 or not economy.is_buyable(economy.catalog_by_key()[key]):
                continue
            try:
                economy.purchase(user_id, key, count)
            except EconomyError:
                continue
            bought[key] = count
            spent += count * price

    parts = []
    if healed:
        parts.append(i18n.t(lang, "autoheal.result.healed", count=healed, items=_items_text(lang, used)))
    else:
        parts.append(i18n.t(lang, "autoheal.result.nothing"))
    if bought:
        parts.append(i18n.t(lang, "autoheal.result.bought", items=_items_text(lang, bought), cost=spent))
    if plan["insufficient"]:
        parts.append(i18n.t(lang, "autoheal.result.insufficient",
                            names=", ".join(e["name"] for e in plan["insufficient"])))
    if plan["fainted"]:
        parts.append(i18n.t(lang, "autoheal.result.fainted",
                            names=", ".join(e["name"] for e in plan["fainted"])))
    return {
        "ok": True, "healed": healed, "used": used, "bought": bought, "spent": spent,
        "insufficient": [e["name"] for e in plan["insufficient"]],
        "fainted": [e["name"] for e in plan["fainted"]],
        "money": economy.balance(user_id), "message": " ".join(parts),
    }
