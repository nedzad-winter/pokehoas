"""Buddy-Endpunkte: eigenes Buddy-Pokemon und Schritt-Sensor waehlen
(Logik und HA-Anbindung: app/buddy.py).

Aenderungen werden sofort nach HA gepusht; schlaegt das fehl (HA nicht
erreichbar, lokale Entwicklung), bleibt die Auswahl trotzdem gespeichert -
der naechste Poll-Durchlauf holt den Push nach.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import buddy, db, i18n
from ..auth import get_current_user
from ..ha_client import HomeAssistantError
from ..runtime import ha_client

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["buddy"])


class BuddyIn(BaseModel):
    pokemon_id: int | None = None  # None = keinen Buddy


class SensorIn(BaseModel):
    entity_id: str | None = None  # None = keine Schritte zaehlen


async def _push(user_id: str) -> bool:
    try:
        await buddy.push(ha_client, db.get_user(user_id))
    except HomeAssistantError as exc:
        log.info("Buddy-Push nach HA fehlgeschlagen (folgt beim naechsten Poll): %s", exc)
        return False
    return True


@router.get("/buddy")
def get_buddy(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return buddy.status(current_user)


@router.put("/buddy")
async def set_buddy(payload: BuddyIn, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    user_id = current_user["id"]
    try:
        buddy.set_buddy(user_id, payload.pokemon_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=i18n.t(current_user["language"], "error.pokemon_not_found")) from exc
    pushed = await _push(user_id)
    return buddy.status(db.get_user(user_id)) | {"pushed": pushed}


@router.put("/buddy/sensor")
async def set_sensor(payload: SensorIn, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    user_id = current_user["id"]
    entity_id = (payload.entity_id or "").strip() or None
    try:
        buddy.set_sensor(user_id, entity_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=i18n.t(current_user["language"], "error.buddy_invalid_sensor")) from exc
    pushed = await _push(user_id) if db.get_user(user_id).get("buddy_pokemon_id") else False
    return buddy.status(db.get_user(user_id)) | {"pushed": pushed}


@router.get("/buddy/sensors")
async def list_step_sensors(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Schritt-/Distanz-Sensoren aus HA fuer die Auswahl - bewusst nur diese,
    nicht die ganze Entity-Liste (die ist Admins vorbehalten)."""
    try:
        states = await ha_client.list_states()
    except HomeAssistantError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    sensors = [
        {
            "entity_id": state["entity_id"],
            "name": (state.get("attributes") or {}).get("friendly_name") or state["entity_id"],
            "state": state.get("state"),
            "unit": (state.get("attributes") or {}).get("unit_of_measurement"),
        }
        for state in states
        if buddy.is_step_candidate(state)
    ]
    sensors.sort(key=lambda s: s["name"].lower())
    return {"sensors": sensors}
