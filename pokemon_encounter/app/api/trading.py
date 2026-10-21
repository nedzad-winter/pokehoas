"""Endpoints for trading Pokémon between household members (logic in `trading.py`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import trading
from ..auth import get_current_user

router = APIRouter(prefix="/api", tags=["trading"])


class TradeRequestIn(BaseModel):
    partner_id: str
    pokemon_id: int


class TradeOfferIn(BaseModel):
    pokemon_id: int


@router.get("/trades")
def list_trades(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return trading.overview(current_user["id"])


@router.get("/trades/pending")
def pending_trades(current_user: dict = Depends(get_current_user)) -> dict[str, int]:
    return {"count": trading.pending_count(current_user["id"])}


@router.get("/trades/tradable")
def tradable_pokemon(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return {"pokemon": trading.tradable_pokemon(current_user["id"])}


@router.post("/trades")
def request_trade(payload: TradeRequestIn, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return trading.request_trade(current_user["id"], payload.partner_id, payload.pokemon_id)


@router.post("/trades/{trade_id}/offer")
def offer(trade_id: int, payload: TradeOfferIn, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return trading.offer(current_user["id"], trade_id, payload.pokemon_id)


@router.post("/trades/{trade_id}/accept")
def accept(trade_id: int, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return trading.accept(current_user["id"], trade_id)


@router.post("/trades/{trade_id}/decline")
def decline(trade_id: int, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return trading.decline(current_user["id"], trade_id)


@router.post("/trades/{trade_id}/cancel")
def cancel(trade_id: int, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return trading.cancel(current_user["id"], trade_id)


@router.delete("/trades/{trade_id}")
def delete_trade(trade_id: int, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return trading.delete(current_user["id"], trade_id)
