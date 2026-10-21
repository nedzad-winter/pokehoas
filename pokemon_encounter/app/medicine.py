"""Healing items from the shop (Potion, Super Potion, Revive, Antidote, ...).

Items are in `data/shop.json` with `category: "medicine"` and describe their
effect via three optional fields:

* `heal`:   HP healing - number (fixed HP) or `"full"` (full HP).
* `revive`: Fraction of max HP with which a FAINTED Pokemon is revived (0.5 =
            Revive, 1.0 = Max Revive).
* `cures`:  List of healed statuses (see battle.STATUS_BONUS) or `"all"`.

Use:

* Outside battle (`use_out_of_battle`, endpoint `POST /api/pokemon/{id}/use-item`):
  acts directly on `owned_pokemon`. Statuses don't exist outside battle (only live
  in battle state, see battle_session.py) - pure status healers are ineffective there.
* In battle (`use_in_battle`, battle action `item`): costs the player's turn, then
  the opponent acts (like switch or throw ball).

Ineffective uses (potion at full HP, potion on fainted Pokemon, revive on fit one,
antidote without poison) are rejected BEFORE the item is consumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import battle, db, economy, healing, i18n
from .economy import EconomyError

CATEGORY = "medicine"
ALL_STATUSES = frozenset({"burn", "paralysis", "poison", "toxic", "sleep", "freeze"})


# -------------------------------------------------------------- Catalog

def catalog() -> dict[str, dict[str, Any]]:
    return {key: item for key, item in economy.catalog_by_key().items()
            if item.get("category") == CATEGORY}


def get(item_key: str | None) -> dict[str, Any] | None:
    return catalog().get(item_key) if item_key else None


def is_medicine(item_key: str | None) -> bool:
    return get(item_key) is not None


def cures_of(item: dict[str, Any]) -> frozenset[str]:
    cures = item.get("cures") or []
    return ALL_STATUSES if cures == "all" else frozenset(cures)


def label(item: dict[str, Any], lang: str) -> str:
    return (item.get("label_de") if lang == "de" else None) or item["label"]


# --------------------------------------------------------------- Effect

@dataclass
class Effect:
    new_hp: int
    new_status: str | None
    healed: int = 0          # HP gain (on revival: the new HP)
    revived: bool = False
    cured: str | None = None  # healed status


def compute_effect(
    item: dict[str, Any], current_hp: int, max_hp: int, status: str | None,
) -> Effect | str:
    """Pure rules function: effect of `item` on a Pokemon with these values.
    Returns either the new state (`Effect`) or the i18n key explaining why the
    item would have no effect."""
    revive = float(item.get("revive") or 0)
    heal = item.get("heal")
    cures = cures_of(item)

    if current_hp <= 0:
        if revive <= 0:
            return "medicine.error.fainted"
        new_hp = max(1, min(max_hp, int(max_hp * revive)))
        # Like in the main games: fainting ends all status.
        return Effect(new_hp=new_hp, new_status=None, healed=new_hp, revived=True)
    if revive > 0:
        return "medicine.error.not_fainted"

    new_hp, new_status = current_hp, status
    if heal is not None and current_hp < max_hp:
        new_hp = max_hp if heal == "full" else min(max_hp, current_hp + max(0, int(heal)))
    cured = None
    if status and status in cures:
        new_status, cured = None, status

    if new_hp == current_hp and cured is None:
        if heal is not None:
            return "medicine.error.full_hp"
        if cures:
            return "medicine.error.no_status"
        return "medicine.error.no_effect"
    return Effect(new_hp=new_hp, new_status=new_status, healed=new_hp - current_hp, cured=cured)


def _consume(user_id: str, item_key: str, cur=None) -> bool:
    sql = ("UPDATE inventory SET quantity = quantity - 1 "
           "WHERE item_key = ? AND user_id = ? AND quantity > 0")
    if cur is not None:
        cur.execute(sql, (item_key, user_id))
        return cur.rowcount == 1
    with db.cursor() as c:
        c.execute(sql, (item_key, user_id))
        return c.rowcount == 1


def _remaining(user_id: str, item_key: str) -> int:
    row = db.query_one("SELECT quantity FROM inventory WHERE item_key = ? AND user_id = ?",
                       (item_key, user_id))
    return int(row["quantity"]) if row else 0


def _summary(lang: str, name: str, effect: Effect) -> str:
    if effect.revived:
        return i18n.t(lang, "medicine.result.revived", name=name, hp=effect.new_hp)
    parts = []
    if effect.healed:
        parts.append(i18n.t(lang, "medicine.result.healed", name=name, amount=effect.healed))
    if effect.cured:
        parts.append(i18n.t(lang, "medicine.result.cured", name=name,
                            status=i18n.t(lang, f"status.{effect.cured}")))
    return " ".join(parts)


# -------------------------------------------------------- Outside Battle

def use_out_of_battle(user_id: str, owned_id: int, item_key: str) -> dict[str, Any]:
    """Applies a healing item to an own Pokemon (outside of battle).
    Raises EconomyError (-> HTTP status) on invalid use."""
    lang = db.user_language(user_id)
    item = get(item_key)
    if item is None:
        raise EconomyError(400, i18n.t(lang, "error.unknown_item", item=item_key))
    item_name = label(item, lang)
    if healing.has_ongoing_battle(user_id):
        raise EconomyError(409, i18n.t(lang, "medicine.error.in_battle"))
    if db.query_one("SELECT 1 FROM owned_pokemon WHERE id = ? AND user_id = ?",
                    (owned_id, user_id)) is None:
        raise EconomyError(404, i18n.t(lang, "error.pokemon_not_found"))

    # Apply time-based regeneration first so potion works on real HP.
    healing.apply_regen(user_id)
    owned = db.query_one("SELECT * FROM owned_pokemon WHERE id = ? AND user_id = ?",
                         (owned_id, user_id))
    combatant = battle.combatant_from_owned(owned)
    name = owned.get("nickname") or combatant.name
    old_hp = owned["current_hp"]
    current = combatant.max_hp if old_hp is None else min(int(old_hp), combatant.max_hp)

    effect = compute_effect(item, current, combatant.max_hp, None)
    if isinstance(effect, str):
        if effect == "medicine.error.no_status":
            effect = "medicine.error.no_status_outside_battle"
        raise EconomyError(400, i18n.t(lang, effect, name=name, item=item_name))

    with db.cursor() as cur:
        if not _consume(user_id, item_key, cur):
            raise EconomyError(409, i18n.t(lang, "error.item_not_in_bag", item=item_name))
        # Compare-and-set: only write if HP hasn't changed since reading
        # (double-click, parallel battle start).
        if old_hp is None:
            cur.execute("UPDATE owned_pokemon SET current_hp = ? "
                        "WHERE id = ? AND user_id = ? AND current_hp IS NULL",
                        (effect.new_hp, owned_id, user_id))
        else:
            cur.execute("UPDATE owned_pokemon SET current_hp = ? "
                        "WHERE id = ? AND user_id = ? AND current_hp = ?",
                        (effect.new_hp, owned_id, user_id, old_hp))
        if cur.rowcount != 1:
            raise EconomyError(409, i18n.t(lang, "error.pokemon_changed"))

    return {
        "ok": True, "item_key": item_key, "owned_id": owned_id, "name": name,
        "healed": effect.healed, "revived": effect.revived, "cured": effect.cured,
        "current_hp": effect.new_hp, "max_hp": combatant.max_hp,
        "remaining": _remaining(user_id, item_key),
        "message": _summary(lang, name, effect),
    }


def targets_for(user_id: str, item_key: str) -> list[dict[str, Any]]:
    """Own Pokemon where the item would work outside of battle
    (for selection in bag). Team first."""
    item = get(item_key)
    if item is None:
        return []
    rows = db.query(
        "SELECT * FROM owned_pokemon WHERE user_id = ? "
        "ORDER BY party_slot IS NULL, party_slot, level DESC, id",
        (user_id,),
    )
    result = []
    for row in rows:
        c = battle.combatant_from_owned(row)
        current = c.max_hp if row["current_hp"] is None else min(int(row["current_hp"]), c.max_hp)
        if isinstance(compute_effect(item, current, c.max_hp, None), str):
            continue
        result.append({
            "owned_id": row["id"], "name": row.get("nickname") or c.name, "level": row["level"],
            "current_hp": current, "max_hp": c.max_hp, "fainted": current <= 0,
            "in_party": row["party_slot"] is not None,
        })
    return result


# ------------------------------------------------------------------ Battle

def battle_inventory(user_id: str) -> list[dict[str, Any]]:
    """Healing items in bag for battle menu (catalog order)."""
    counts = {
        row["item_key"]: row["quantity"]
        for row in db.query(
            "SELECT item_key, quantity FROM inventory WHERE user_id = ? AND quantity > 0",
            (user_id,),
        )
    }
    return [
        {"key": key, "label": item["label"], "label_de": item.get("label_de"),
         "description": item.get("description", ""), "description_de": item.get("description_de"),
         "sprite_url": item.get("sprite_url"), "count": counts[key],
         "revive": bool(item.get("revive")), "heal": item.get("heal"),
         "cures": sorted(cures_of(item))}
        for key, item in catalog().items() if counts.get(key)
    ]


def use_in_battle(b: battle.Battle, user_id: str, item_key: str | None,
                  target_owned_id: int | None) -> None:
    """Battle action `item`: healing item on a team member (active or bench,
    including fainted for revives). Costs the turn - opponent acts after. Raises
    ValueError (-> 409) on invalid use; the item is then NOT consumed."""
    if b.outcome is not None:
        raise ValueError(i18n.t(b.lang, "battle.over"))
    if b.needs_switch:
        raise ValueError(i18n.t(b.lang, "battle.error.must_switch"))
    item = get(item_key)
    if item is None:
        raise ValueError(i18n.t(b.lang, "error.unknown_item", item=item_key))
    if target_owned_id is None:
        target_owned_id = b.active_mon.owned_id
    member = next((m for m in b.team if m.owned_id == target_owned_id), None)
    if member is None:
        raise ValueError(i18n.t(b.lang, "battle.error.not_in_team"))

    item_name = label(item, b.lang)
    effect = compute_effect(item, member.current_hp, member.max_hp, member.status)
    if isinstance(effect, str):
        raise ValueError(i18n.t(b.lang, effect, name=member.name, item=item_name))
    if not _consume(user_id, item["key"]):
        raise ValueError(i18n.t(b.lang, "error.item_not_in_bag", item=item_name))

    member.current_hp = effect.new_hp
    member.status = effect.new_status
    if effect.new_status is None:
        member.status_counter = 0
    b.log.append(i18n.t(b.lang, "medicine.log.used", item=item_name, name=member.name))
    b.log.append(_summary(b.lang, member.name, effect))
    battle.run_turn(b, None)  # Item costs the turn
