"""Auto rules: per-player IF/DO rules for encounters.

Logic is in `app/auto_rules.py`; just the HTTP layer here. Everything per user
(Ingress header, see auth.py).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError

from .. import auto_rules, i18n
from ..auth import get_current_user

router = APIRouter(prefix="/api", tags=["auto-rules"])


class SettingsIn(BaseModel):
    enabled: bool | None = None
    daily_cap: int | None = Field(None, ge=0)


class OrderIn(BaseModel):
    ids: list[int]


def _http(exc: auto_rules.AutoRuleError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _parse(payload: dict[str, Any], lang: str) -> auto_rules.RuleIn:
    """Validate here instead of in the signature, so the player gets a
    translated message instead of FastAPI's raw 422."""
    try:
        return auto_rules.RuleIn.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first.get("loc", ()))
        raise HTTPException(status_code=400, detail=i18n.t(
            lang, "auto_rules.error.invalid", field=field)) from exc


@router.get("/auto-rules")
def get_rules(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    user_id = current_user["id"]
    return {
        "settings": auto_rules.user_settings(user_id),
        "rules": auto_rules.list_rules(user_id),
        "catalog": auto_rules.catalog(),
    }


@router.put("/auto-rules/settings")
def put_settings(payload: SettingsIn,
                 current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return auto_rules.set_user_settings(current_user["id"], payload.enabled, payload.daily_cap)


@router.post("/auto-rules")
def create_rule(payload: dict[str, Any] = Body(...),
                current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    rule = _parse(payload, current_user["language"])
    try:
        return auto_rules.create_rule(current_user["id"], rule)
    except auto_rules.AutoRuleError as exc:
        raise _http(exc) from exc


@router.post("/auto-rules/templates/{key}")
def create_from_template(key: str,
                         current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    try:
        return auto_rules.create_from_template(current_user["id"], key)
    except auto_rules.AutoRuleError as exc:
        raise _http(exc) from exc


@router.put("/auto-rules/order")
def reorder(payload: OrderIn,
            current_user: dict = Depends(get_current_user)) -> list[dict[str, Any]]:
    return auto_rules.reorder(current_user["id"], payload.ids)


@router.get("/auto-rules/preview")
def preview(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Dry run over the waiting queue, plus the switches (queue toolbar)."""
    user_id = current_user["id"]
    return {"settings": auto_rules.user_settings(user_id),
            "encounters": auto_rules.preview(user_id)}


@router.get("/auto-rules/log")
def rule_log(current_user: dict = Depends(get_current_user)) -> list[dict[str, Any]]:
    return auto_rules.recent_log(current_user["id"])


@router.post("/auto-rules/run")
def run(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    try:
        return auto_rules.run_for_user(current_user["id"])
    except auto_rules.AutoRuleError as exc:
        raise _http(exc) from exc


@router.put("/auto-rules/{rule_id}")
def update_rule(rule_id: int, payload: dict[str, Any] = Body(...),
                current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    rule = _parse(payload, current_user["language"])
    try:
        return auto_rules.update_rule(current_user["id"], rule_id, rule)
    except auto_rules.AutoRuleError as exc:
        raise _http(exc) from exc


@router.delete("/auto-rules/{rule_id}")
def delete_rule(rule_id: int, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    try:
        auto_rules.delete_rule(current_user["id"], rule_id)
    except auto_rules.AutoRuleError as exc:
        raise _http(exc) from exc
    return {"ok": True}
