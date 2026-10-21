"""Zucht-Endpunkte: Pension, Eier, Inkubatoren (Logik: app/breeding.py).

Alles pro Nutzer (Ingress-Header, siehe auth.py). Wie bei anderen Team-
Aenderungen sind Eier-Verschiebungen und Schluepfen waehrend eines Kampfes
gesperrt.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import breeding, healing, i18n
from ..auth import get_current_user

router = APIRouter(prefix="/api", tags=["breeding"])


class DaycareIn(BaseModel):
    parent_a: int
    parent_b: int


class EggLocationIn(BaseModel):
    location: str  # box | party | incubator


def _http(exc: breeding.BreedingError, lang: str) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=i18n.t(lang, exc.key, **exc.params))


def _not_in_battle(user_id: str, lang: str) -> None:
    if healing.has_ongoing_battle(user_id):
        raise HTTPException(status_code=409, detail=i18n.t(lang, "error.team_locked_in_battle"))


@router.get("/eggs")
def get_eggs(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return breeding.overview(current_user["id"])


@router.put("/daycare")
def set_daycare(payload: DaycareIn, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    try:
        breeding.set_daycare(current_user["id"], payload.parent_a, payload.parent_b)
    except breeding.BreedingError as exc:
        raise _http(exc, current_user["language"]) from exc
    return breeding.overview(current_user["id"])


@router.delete("/daycare")
def clear_daycare(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    breeding.clear_daycare(current_user["id"])
    return breeding.overview(current_user["id"])


@router.post("/daycare/collect")
def collect_egg(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    try:
        egg = breeding.collect_egg(current_user["id"])
    except breeding.BreedingError as exc:
        raise _http(exc, current_user["language"]) from exc
    return {"egg": egg, **breeding.overview(current_user["id"])}


@router.put("/eggs/{egg_id}/location")
def move_egg(
    egg_id: int, payload: EggLocationIn, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    user_id, lang = current_user["id"], current_user["language"]
    _not_in_battle(user_id, lang)
    try:
        breeding.move_egg(user_id, egg_id, payload.location)
    except breeding.BreedingError as exc:
        raise _http(exc, lang) from exc
    return breeding.overview(user_id)


@router.post("/eggs/{egg_id}/hatch")
def hatch_egg(egg_id: int, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    user_id, lang = current_user["id"], current_user["language"]
    _not_in_battle(user_id, lang)
    try:
        result = breeding.hatch(user_id, egg_id)
    except breeding.BreedingError as exc:
        raise _http(exc, lang) from exc
    return {"hatched": result, **breeding.overview(user_id)}
