"""Endpoints for manual battle (logic in `battle_session.py`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import battle_history, breeding, battle_session, pvp, trainer_battle
from ..auth import get_current_user

router = APIRouter(prefix="/api", tags=["battles"])


class BattleTeamIn(BaseModel):
    # Owned Pokemon ids to fight with, in send-out order - omit to use the
    # live party. Never written to party_slot.
    team: list[int] | None = Field(default=None, max_length=breeding.PARTY_SIZE)


class BattleAction(BaseModel):
    action: str                 # move | switch | ball | mega | item | run
    move: str | None = None     # move key for action=move
    target: int | None = None   # owned_pokemon.id for action=switch
    ball: str | None = None     # ball key for action=ball
    item: str | None = None     # healing item key for action=item (target: target, else active Pokemon)
    # action=mega braucht kein Feld: entwickelt immer das aktive Pokémon
    # (Mega-Stein und Regeln siehe app/mega.py).


@router.post("/encounters/{encounter_id}/battle")
def start_battle(
    encounter_id: int, current_user: dict = Depends(get_current_user),
    payload: BattleTeamIn = BattleTeamIn(),
) -> dict[str, Any]:
    """Starts a battle (claims the encounter) or continues your own."""
    return battle_session.start(encounter_id, current_user["id"], payload.team)


@router.get("/battles/current")
def current_battle(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    user_id = current_user["id"]
    return {"battle": battle_session.current_view(user_id) or trainer_battle.current_view(user_id)
            or pvp.current_view(user_id)}


@router.get("/battles/history")
def battle_history_list(
    limit: int = battle_history.DEFAULT_LIMIT,
    current_user: dict = Depends(get_current_user),
    offset: int = 0,
) -> dict[str, Any]:
    """User's battles (wild, auto-battle, gym/league, PvP), paged newest-first."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    user_id = current_user["id"]
    return {
        "battles": battle_history.recent(user_id, limit, offset),
        "limit": limit, "offset": offset, "total": battle_history.count(user_id),
    }


@router.get("/battles/{battle_id}")
def get_battle(battle_id: int, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return battle_session.get_view(battle_id, current_user["id"])


@router.post("/battles/{battle_id}/action")
def battle_action(
    battle_id: int, payload: BattleAction, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    return battle_session.act(battle_id, current_user["id"], payload.model_dump())
