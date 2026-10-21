"""Money, shop, bag and item usage (evolutions).

Logic is in `app/economy.py` and `app/evolution.py`; just the HTTP layer here.
Everything per user (Ingress header, see auth.py).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import battle, db, economy, evolution, evs, god_mode, held_items, medicine, mega, moves, tms
from ..auth import get_current_user

router = APIRouter(prefix="/api", tags=["economy"])


class PurchaseIn(BaseModel):
    item_key: str
    quantity: int = 1


class UseItemIn(BaseModel):
    item_key: str
    quantity: int = Field(1, ge=1, le=100)  # Rare Candy and EV items: how many to use at once


def _http(exc: economy.EconomyError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/wallet")
def wallet(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"money": economy.balance(current_user["id"])}


@router.get("/shop")
def shop(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    user_id = current_user["id"]
    multiplier = float(db.get_setting("shop_price_multiplier", 1.0))
    owned = {
        row["item_key"]: row["quantity"]
        for row in db.query("SELECT item_key, quantity FROM inventory WHERE user_id = ?", (user_id,))
    }
    usable = evolution.usable_items_by_owned(user_id)
    # Mega-Steine fuer Arten, die dieser Pokédex gar nicht kennt (nicht
    # geseedete Generationen), waeren unbenutzbar - die bietet der Shop nicht an.
    stones = mega.available_stones()
    # God mode (app/god_mode.py): every item is for sale and free.
    free = god_mode.active(user_id)
    items = [
        {
            "key": item["key"],
            "category": item["category"],
            "label": item["label"],
            "label_de": item.get("label_de"),
            "description": item.get("description", ""),
            "description_de": item.get("description_de"),
            "sprite_url": item.get("sprite_url"),
            "price": 0 if free else economy.unit_price(item, multiplier),
            "move": item.get("move"),
            "move_type": moves.all_moves().get(item["move"], {}).get("type") if item.get("move") else None,
            "owned": owned.get(item["key"], 0),
            # How many own Pokémon could use this item (evolution items only).
            "usable_count": len(usable.get(item["key"], [])),
        }
        for item in economy.catalog()
        if (free or economy.is_buyable(item)) and (not mega.is_stone(item["key"]) or item["key"] in stones)
    ]
    return {"money": economy.balance(user_id), "items": items, "god_mode": free}


@router.post("/shop/buy")
def buy(payload: PurchaseIn, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    try:
        return economy.purchase(current_user["id"], payload.item_key, payload.quantity)
    except economy.EconomyError as exc:
        raise _http(exc) from exc


@router.get("/bag")
def bag(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Inventory with labels; for evolution items, the matching own Pokémon."""
    user_id = current_user["id"]
    rows = db.query(
        "SELECT item_key, quantity FROM inventory WHERE user_id = ? AND quantity > 0 "
        "ORDER BY item_key",
        (user_id,),
    )
    known = economy.catalog_by_key()
    order = {key: index for index, key in enumerate(known)}
    multiplier = float(db.get_setting("shop_price_multiplier", 1.0))
    usable = evolution.usable_items_by_owned(user_id)
    items = []
    for row in rows:
        key = row["item_key"]
        item = known.get(key, {})
        category = item.get("category") or ("ball" if key in battle.BALLS else "other")
        items.append({
            "key": key,
            "label": economy.item_label(key),
            "label_de": economy.item_label(key, "de"),
            "category": category,
            "description": item.get("description", ""),
            "description_de": item.get("description_de"),
            "sprite_url": item.get("sprite_url") or held_items.sprite_url_for(key),
            "move": item.get("move"),
            "quantity": row["quantity"],
            # Only for sorting in bag (same categories as shop);
            # items outside the catalog have no price.
            "price": economy.unit_price(item, multiplier) if item.get("price") is not None else None,
            "targets": (usable.get(key, []) if category == "evolution"
                        else medicine.targets_for(user_id, key) if category == medicine.CATEGORY
                        else economy.rare_candy_targets(user_id) if key == economy.RARE_CANDY
                        else tms.targets_for(user_id, key) if category == tms.CATEGORY
                        else evs.targets_for(user_id, key) if category == evs.CATEGORY
                        else []),
            # Pure status healers: status only exists in battle.
            "battle_only": category == medicine.CATEGORY
                           and item.get("heal") is None and not item.get("revive"),
        })
    items.sort(key=lambda it: order.get(it["key"], len(order)))
    return {"money": economy.balance(user_id), "items": items}


@router.post("/pokemon/{owned_id}/use-item")
def use_item(
    owned_id: int, payload: UseItemIn, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    """Evolution item, TM, EV item (vitamin, wing, berry) or healing item
    (potion, revive, ...) outside of battle."""
    try:
        if medicine.is_medicine(payload.item_key):
            return medicine.use_out_of_battle(current_user["id"], owned_id, payload.item_key)
        if payload.item_key == economy.RARE_CANDY:
            return economy.use_rare_candy(current_user["id"], owned_id, payload.quantity)
        if tms.is_tm(payload.item_key):
            return tms.teach(current_user["id"], owned_id, payload.item_key)
        if evs.is_ev_item(payload.item_key):
            return evs.apply_item(current_user["id"], owned_id, payload.item_key, payload.quantity)
        return evolution.use_item(current_user["id"], owned_id, payload.item_key)
    except economy.EconomyError as exc:
        raise _http(exc) from exc
