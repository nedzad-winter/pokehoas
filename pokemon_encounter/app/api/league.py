"""Endpunkte fuer Arena/Liga (Logik in `trainer_battle.py`) und Erfolge."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends

from .. import achievements, trainer_battle, training
from ..auth import get_current_user
from .battles import BattleAction, BattleTeamIn

router = APIRouter(prefix="/api", tags=["league"])


@router.get("/trainers")
def list_trainers(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return trainer_battle.overview(current_user["id"])


@router.post("/trainers/{trainer_id}/battle")
def start_trainer_battle(
    trainer_id: str, current_user: dict = Depends(get_current_user),
    payload: BattleTeamIn = BattleTeamIn(),
) -> dict[str, Any]:
    """Startet einen Kampf gegen den Trainer oder setzt den eigenen fort."""
    return trainer_battle.start(trainer_id, current_user["id"], team_ids=payload.team)


@router.get("/training")
def training_status(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Whether Training is unlocked (shown in the Pokemon summary)."""
    return training.overview(current_user["id"])


@router.post("/training/{owned_id}/battle")
def start_training(
    owned_id: int,
    mode: Literal["moves", "type"] = training.DEFAULT_MODE,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Training with this Pokemon (team or box), see training.py. `mode`:
    sparring team by its first type (x4 weaknesses, default) or by its moves."""
    return trainer_battle.start(training.TRAINER_ID, current_user["id"], owned_id=owned_id,
                                training_mode=mode)


@router.get("/trainer-battles/{battle_id}")
def get_trainer_battle(
    battle_id: int, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    return trainer_battle.get_view(battle_id, current_user["id"])


@router.post("/trainer-battles/{battle_id}/action")
def trainer_battle_action(
    battle_id: int, payload: BattleAction, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    return trainer_battle.act(battle_id, current_user["id"], payload.model_dump())


@router.get("/achievements")
def list_achievements(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return achievements.overview(current_user["id"])
