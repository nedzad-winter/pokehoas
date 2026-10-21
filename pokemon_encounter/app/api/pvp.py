"""Endpunkte fuer Spieler-gegen-Spieler (Logik in `pvp.py`, `team_code.py`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import breeding, pvp, team_code
from ..auth import get_current_user
from .battles import BattleAction, BattleTeamIn

router = APIRouter(prefix="/api", tags=["pvp"])


class TeamCodeIn(BaseModel):
    # Laenge wird in team_code.decode nochmal geprueft (mit uebersetztem Text);
    # das Limit hier haelt uebergrosse Bodies gleich vom Parser fern.
    code: str = Field(max_length=team_code.MAX_CODE_LENGTH + 100)
    # Owned Pokemon ids to fight with (see BattleTeamIn); omit for the live party.
    team: list[int] | None = Field(default=None, max_length=breeding.PARTY_SIZE)


@router.get("/pvp/opponents")
def list_opponents(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return pvp.opponents_overview(current_user["id"])


@router.post("/pvp/challenge/{user_id}")
def challenge_user(
    user_id: str, current_user: dict = Depends(get_current_user),
    payload: BattleTeamIn = BattleTeamIn(),
) -> dict[str, Any]:
    """Kampf gegen eine KI-Kopie des Teams dieses Nutzers (oder Fortsetzung)."""
    return pvp.start_vs_user(current_user["id"], user_id, payload.team)


@router.get("/pvp/team-code")
def my_team_code(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return pvp.team_code_for_user(current_user["id"])


@router.post("/pvp/preview")
def preview_code(payload: TeamCodeIn, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return pvp.decode_preview(payload.code, current_user["id"])


@router.post("/pvp/code-battle")
def battle_code(payload: TeamCodeIn, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return pvp.start_vs_code(current_user["id"], payload.code, payload.team)


@router.get("/pvp-battles/{battle_id}")
def get_pvp_battle(battle_id: int, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return pvp.get_view(battle_id, current_user["id"])


@router.post("/pvp-battles/{battle_id}/action")
def pvp_battle_action(
    battle_id: int, payload: BattleAction, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    return pvp.act(battle_id, current_user["id"], payload.model_dump())
