"""Item rewards for achievements: gym badges, cleared regions, the whole league.

Money for a won gym/league battle is handled in `trainer_battle.prize_money` -
this module is only about the ITEMS an achievement hands out, and only the
FIRST time it is unlocked (`achievements.on_trainer_defeated`), so no reward can
be farmed by beating the same trainer again.

Like in the main games, every gym leader awards a TM (`tms.py`). On top of that
each badge comes with balls, scaled to how far into the region that gym is:

    gym 1: 2 Poke Balls                ... gym 4: 5 Poke, 2 Great
    gym 6: 5 Poke, 4 Great, 2 Ultra    ... gym 8: 5 Poke, 5 Great, 5 Ultra

so the reward grows both in amount and in ball tier, and never exceeds the three
regular tiers (Poke/Great/Ultra, at most five of each). Galar has ten gyms - its
last three all pay out like an eighth gym.

Clearing a whole region (every gym leader, the Elite Four and the champion of
it) adds one Master Ball. Beating every region on top of that is the "League
Champion" achievement and keeps its Shiny Charm (see `achievements.py`).
"""

from __future__ import annotations

from typing import Any

from . import db, economy, i18n, tms, trainers

MASTER_BALL = "master_ball"

# Balls per gym, by the gym's position within its region (1..8).
GYM_BALLS: dict[int, dict[str, int]] = {
    1: {"poke_ball": 2},
    2: {"poke_ball": 3},
    3: {"poke_ball": 4},
    4: {"poke_ball": 5, "great_ball": 2},
    5: {"poke_ball": 5, "great_ball": 3},
    6: {"poke_ball": 5, "great_ball": 4, "ultra_ball": 2},
    7: {"poke_ball": 5, "great_ball": 5, "ultra_ball": 3},
    8: {"poke_ball": 5, "great_ball": 5, "ultra_ball": 5},
}
MAX_GYM_INDEX = max(GYM_BALLS)


def gym_index(trainer_id: str) -> int | None:
    """Position of this gym leader within their region (1 = first gym), by the
    order in `data/trainers.json` - which is the order the games send you
    through the gyms (and, with it, the opponents' levels). None for everyone
    who is not a gym leader."""
    trainer = trainers.get(trainer_id)
    if trainer is None or trainer["kind"] != "gym_leader":
        return None
    in_region = [
        t["id"] for t in trainers.all_trainers()
        if t["region"] == trainer["region"] and t["kind"] == "gym_leader"
    ]
    return in_region.index(trainer_id) + 1


def gym_items(trainer_id: str) -> list[dict[str, Any]]:
    """[{key, quantity}, ...] for defeating this gym leader the first time."""
    index = gym_index(trainer_id)
    if index is None:
        return []
    balls = GYM_BALLS[min(index, MAX_GYM_INDEX)]
    items = [{"key": key, "quantity": count} for key, count in balls.items()]
    tm_key = tms.for_gym(trainer_id)
    if tm_key:
        items.append({"key": tm_key, "quantity": 1})
    return items


def region_items(region_key: str) -> list[dict[str, Any]]:
    """Reward for clearing a whole region. `region_key` is unused today (every
    region pays the same Master Ball) but keeps call sites honest about what
    the reward belongs to."""
    return [{"key": MASTER_BALL, "quantity": 1}]


def money_label(amount: int, lang: str = "en") -> str:
    """"₽200,000" (en) / "₽200.000" (de)."""
    grouped = f"{int(amount):,}".replace(",", "." if lang == "de" else ",")
    return i18n.t(lang, "reward.money", amount=grouped)


def label(items: list[dict[str, Any]], lang: str = "en", money: int = 0) -> str:
    """"₽200,000, 2x Poke Ball, TM33 Rock Tomb" - the text shown in the battle
    log and on the achievement card. A single item carries no count."""
    parts = [money_label(money, lang)] if money else []
    for item in items:
        name = economy.item_label(item["key"], lang)
        parts.append(name if int(item["quantity"]) <= 1
                     else i18n.t(lang, "reward.item_count", count=item["quantity"], item=name))
    return ", ".join(parts)


def build(items: list[dict[str, Any]], lang: str = "en", money: int = 0) -> dict[str, Any] | None:
    """Reward as `achievements.definitions()` carries it (`None` = neither
    items nor money)."""
    if not items and not money:
        return None
    reward: dict[str, Any] = {"items": items, "label": label(items, lang, money)}
    if money:
        reward["money"] = int(money)
    return reward


def grant(user_id: str, items: list[dict[str, Any]], money: int = 0) -> None:
    """Add the items to the user's bag (quantities add up - a TM from a second
    region is a second TM, see tms.py) and credit `money`."""
    if money:
        economy.credit(user_id, money)
    for item in items:
        db.execute(
            "INSERT INTO inventory (item_key, user_id, quantity) VALUES (?,?,?) "
            "ON CONFLICT(item_key, user_id) DO UPDATE SET quantity = quantity + excluded.quantity",
            (item["key"], user_id, int(item["quantity"])),
        )
