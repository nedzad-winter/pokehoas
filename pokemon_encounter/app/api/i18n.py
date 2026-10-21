"""Uebersetzungs-Katalog fuers Frontend (siehe app/i18n.py fuer die
Backend-Seite derselben JSON-Dateien)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import i18n

router = APIRouter(prefix="/api", tags=["i18n"])


@router.get("/i18n/{language}")
def get_catalog(language: str) -> dict[str, Any]:
    return {
        "language": language,
        "available": i18n.available_languages(),
        "strings": i18n.catalog(language),
    }
