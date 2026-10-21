"""Endpoints for the daily challenge quests (logic in `challenge.py`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import breeding, challenge
from ..auth import get_current_user

router = APIRouter(prefix="/api", tags=["challenges"])


class ChallengeTeamIn(BaseModel):
    # Owned Pokemon ids to fight with, in send-out order - omit to use the
    # live party. Never written to party_slot (see challenge.start).
    team: list[int] | None = Field(default=None, max_length=breeding.PARTY_SIZE)


@router.get("/challenges")
def list_challenges(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Today's three quests (rolled on first request of the day)."""
    return challenge.overview(current_user["id"])


@router.post("/challenges/{slot}/battle")
def start_challenge(
    slot: int, payload: ChallengeTeamIn = ChallengeTeamIn(),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Starts (or continues) the fight of quest `slot` (1 easy, 2 normal, 3 hard).
    The battle then runs through /api/pvp-battles/{id}/action."""
    return challenge.start(current_user["id"], slot, payload.team)
