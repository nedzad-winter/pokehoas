"""Uebersetzungen: ein flaches JSON-Woerterbuch pro Sprache in
`data/i18n/<sprache>.json` (Key -> Text, `{platzhalter}` per str.format).

Sowohl das Backend (Kampf-Log-Zeilen, Fehlermeldungen - siehe `t()`) als
auch das Frontend (statische Oberflaechentexte, ueber `GET /api/i18n/{sprache}`,
siehe `api/i18n.py`) lesen aus DENSELBEN Dateien - eine neue Sprache
hinzuzufuegen heisst also: EINE neue JSON-Datei mit denselben Keys anlegen,
sonst nichts. Fehlt ein Key in einer Sprache, faellt er automatisch auf
Englisch zurueck (siehe `t()`/`catalog()`) - eine neue Sprache muss also
nicht sofort vollstaendig sein, um benutzbar zu sein.

Englisch (`en.json`) ist die Standardsprache der App und zugleich das
Fallback fuer alle anderen Sprachen.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data" / "i18n"
DEFAULT_LANGUAGE = "en"

_catalogs: dict[str, dict[str, str]] | None = None
# Language of the current request's user, for code paths too deep to be handed
# one (see `t_request`). Set by admin.require_admin.
_request_language: ContextVar[str] = ContextVar("i18n_request_language", default=DEFAULT_LANGUAGE)


def _load() -> dict[str, dict[str, str]]:
    global _catalogs
    if _catalogs is None:
        catalogs: dict[str, dict[str, str]] = {}
        for path in sorted(DATA_DIR.glob("*.json")):
            catalogs[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        # Optional extension texts: data/i18n/ext/<name>.<sprache>.json, merged
        # into the matching language (absent in the public build).
        for path in sorted(DATA_DIR.glob("ext/*.*.json")):
            language = path.stem.rsplit(".", 1)[1]
            if language in catalogs:
                catalogs[language].update(json.loads(path.read_text(encoding="utf-8")))
        _catalogs = catalogs
    return _catalogs


def available_languages() -> list[str]:
    return sorted(_load().keys())


def catalog(language: str) -> dict[str, str]:
    """Uebersetzungs-Woerterbuch einer Sprache, MIT bereits eingemischtem
    Englisch-Fallback fuer fehlende Keys (das Frontend muss sich also nicht
    selbst um Luecken kuemmern)."""
    catalogs = _load()
    merged = dict(catalogs.get(DEFAULT_LANGUAGE, {}))
    merged.update(catalogs.get(language, {}))
    return merged


def t(language: str, key: str, **params: Any) -> str:
    """Uebersetzt EINEN Key, mit Platzhalter-Interpolation. Faellt bei
    fehlendem Key/fehlender Sprache auf Englisch zurueck, und im
    aeussersten Notfall auf den Key selbst (nie ein Absturz wegen einer
    unvollstaendigen Sprachdatei)."""
    catalogs = _load()
    template = catalogs.get(language, {}).get(key)
    if template is None:
        template = catalogs.get(DEFAULT_LANGUAGE, {}).get(key, key)
    try:
        return template.format(**params)
    except (KeyError, IndexError):
        log.warning("i18n: Platzhalter fehlt fuer %s/%s (%s)", language, key, params)
        return template


def set_request_language(language: str) -> None:
    _request_language.set(language)


def t_request(key: str, **params: Any) -> str:
    """`t()` in the language set by `set_request_language` for this request."""
    return t(_request_language.get(), key, **params)


def language_from_accept(header: str | None) -> str:
    """First supported language of an Accept-Language header (no user known yet)."""
    for part in (header or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in available_languages():
            return code
    return DEFAULT_LANGUAGE


# Thousands separator per language; anything not listed uses ",".
_THOUSANDS = {"de": ".", "es": ".", "it": ".", "fr": "\u202f", "pl": "\u00a0"}


def group_digits(amount: int, language: str) -> str:
    """200000 -> "200,000" (en) / "200.000" (de) / "200 000" (fr, pl)."""
    return f"{int(amount):,}".replace(",", _THOUSANDS.get(language, ","))
