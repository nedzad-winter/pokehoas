"""Ability / move / type names in the PokeAPI languages the UI supports (es, fr, it, ja, ko),
read from data/names_i18n.json (see scripts/build_name_translations.py). English and German keep
their catalog names. The games have no Polish release, so Polish players use the English game
terms and those languages always return None."""

from __future__ import annotations

import json
from pathlib import Path

_PATH = Path(__file__).parent / "data" / "names_i18n.json"
_tables: dict | None = None


def _load() -> dict:
    global _tables
    if _tables is None:
        try:
            _tables = json.loads(_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _tables = {}
    return _tables


def lookup(kind: str, key: str | None, lang: str) -> str | None:
    """Translated name of an ability/move/type, or None (caller falls back)."""
    if not key or lang in ("en", "de"):
        return None
    return (_load().get(kind, {}).get(key) or {}).get(lang)
