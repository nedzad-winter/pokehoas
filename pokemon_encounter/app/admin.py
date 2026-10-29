"""Admin detection.

Ingress headers (see `auth.py`) only say WHO is requesting - not whether the
person is an administrator in Home Assistant. We therefore ask HA ourselves:
the WebSocket command `config/auth/list` delivers all HA users with `group_ids`
(`system-admin` = administrator) and `is_owner`. The SUPERVISOR_TOKEN is
allowed to call this command (checked in live add-on).

Check order:
1. Add-on option `admin_users` (HA username or user ID) -> always admin.
   Useful as emergency access if the HA query ever fails.
2. Auth mode `dev` (local development): the dev fallback user is admin,
   unless `POKEMON_DEV_ADMIN=0`.
3. HA user list (briefly cached): active and (`system-admin` or owner).
4. Auth mode `ha_oauth` only: if the configured token may not list HA users,
   the admin flag HA reported for this user at login (`auth/current_user`).

If the HA query fails, a not-too-old list is reused; without any list: no admin
(fail closed).

Outside the add-on (`POKEMON_HA_TOKEN`, a long-lived token) the token must
belong to an HA administrator for `config/auth/list` to work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from fastapi import Depends, HTTPException

from . import auth
from .auth import get_current_user
from .config import settings

log = logging.getLogger(__name__)

ADMIN_GROUP = "system-admin"
CACHE_TTL_SECONDS = 60
# On HA failure, an older list may be reused for this long.
STALE_GRACE_SECONDS = 15 * 60
# Nach einem Fehlschlag nicht bei jedem Request erneut HA anfragen.
ERROR_BACKOFF_SECONDS = 15
WS_TIMEOUT_SECONDS = 10

_cache_users: dict[str, dict[str, Any]] | None = None
_cache_at: float = 0.0
_last_error_at: float = 0.0
_fetch_lock: asyncio.Lock | None = None


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


def dev_mode() -> bool:
    # Zur Laufzeit lesen (nicht beim Import), damit Tests umschalten koennen.
    return auth.dev_mode()


def _websocket_url() -> str:
    base = settings.ha_url
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return base + "/websocket"


def _token() -> str:
    return os.getenv("SUPERVISOR_TOKEN") or settings.ha_token


async def _fetch_ha_users() -> list[dict[str, Any]]:
    """Alle HA-Nutzer ueber `config/auth/list` (erfordert Admin-Token)."""
    # `websockets` kommt mit uvicorn[standard] - keine Zusatzabhaengigkeit.
    from websockets.asyncio.client import connect

    async def run() -> list[dict[str, Any]]:
        async with connect(_websocket_url(), max_size=None, open_timeout=WS_TIMEOUT_SECONDS) as ws:
            hello = json.loads(await ws.recv())
            if hello.get("type") != "auth_required":
                raise RuntimeError(f"Unerwartete HA-Begruessung: {hello.get('type')}")
            await ws.send(json.dumps({"type": "auth", "access_token": _token()}))
            answer = json.loads(await ws.recv())
            if answer.get("type") != "auth_ok":
                raise RuntimeError(f"HA-WebSocket-Anmeldung fehlgeschlagen: {answer.get('type')}")
            await ws.send(json.dumps({"id": 1, "type": "config/auth/list"}))
            while True:
                result = json.loads(await ws.recv())
                if result.get("id") == 1:
                    break
            if not result.get("success"):
                raise RuntimeError(f"config/auth/list abgelehnt: {result.get('error')}")
            return list(result.get("result") or [])

    return await asyncio.wait_for(run(), timeout=WS_TIMEOUT_SECONDS * 2)


async def ha_users(force: bool = False) -> dict[str, dict[str, Any]] | None:
    """HA-Nutzer nach ID, gecacht. `None`, wenn nichts Brauchbares vorliegt."""
    global _cache_users, _cache_at, _last_error_at, _fetch_lock
    if not _token():
        return None

    now = time.monotonic()
    if not force and _cache_users is not None and now - _cache_at < CACHE_TTL_SECONDS:
        return _cache_users
    if not force and now - _last_error_at < ERROR_BACKOFF_SECONDS:
        return _stale_or_none(now)

    if _fetch_lock is None:
        _fetch_lock = asyncio.Lock()
    async with _fetch_lock:
        # Ein paralleler Request hat evtl. gerade schon aktualisiert.
        now = time.monotonic()
        if not force and _cache_users is not None and now - _cache_at < CACHE_TTL_SECONDS:
            return _cache_users
        try:
            users = await _fetch_ha_users()
        except Exception as exc:  # noqa: BLE001 - HA-Ausfall darf die App nicht stoeren
            _last_error_at = time.monotonic()
            log.warning("HA-Nutzerliste fuer Admin-Pruefung nicht abrufbar: %s", exc)
            return _stale_or_none(_last_error_at)
        _cache_users = {user["id"]: user for user in users if user.get("id")}
        _cache_at = time.monotonic()
        return _cache_users


def _stale_or_none(now: float) -> dict[str, dict[str, Any]] | None:
    if _cache_users is not None and now - _cache_at < STALE_GRACE_SECONDS:
        return _cache_users
    return None


def clear_cache() -> None:
    global _cache_users, _cache_at, _last_error_at
    _cache_users, _cache_at, _last_error_at = None, 0.0, 0.0


def _in_override_list(user: dict[str, Any]) -> bool:
    wanted = {entry.casefold() for entry in settings.admin_users}
    if not wanted:
        return False
    candidates = {str(user.get("id") or ""), str(user.get("name") or "")}
    return any(c and c.casefold() in wanted for c in candidates)


def ha_user_is_admin(ha_user: dict[str, Any] | None) -> bool:
    if not ha_user or not ha_user.get("is_active", True):
        return False
    return bool(ha_user.get("is_owner")) or ADMIN_GROUP in (ha_user.get("group_ids") or [])


async def is_admin(user: dict[str, Any]) -> bool:
    """`user` ist der `app_user`-Datensatz (id = HA-User-ID, name = Benutzername)."""
    if _in_override_list(user):
        return True
    if dev_mode() and user.get("id") == auth.DEV_USER_ID:
        return _truthy(os.getenv("POKEMON_DEV_ADMIN"), True)
    users = await ha_users()
    if users is None:
        if auth.auth_mode() == "ha_oauth":
            return bool(auth.login_admin_flag(str(user.get("id") or "")))
        return False
    return ha_user_is_admin(users.get(user.get("id")))


async def current_user_is_admin(current_user: dict = Depends(get_current_user)) -> bool:
    """Dependency fuer Endpunkte, die das Flag nur anzeigen (z.B. /api/users/me)."""
    return await is_admin(current_user)


async def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """Dependency fuer alle /api/admin/...-Endpunkte: 403 fuer Nicht-Admins."""
    if not await is_admin(current_user):
        raise HTTPException(
            status_code=403,
            detail="Nur fuer Administratoren. Admin ist, wer in Home Assistant "
                   "Administrator ist oder in der Option `admin_users` "
                   "(Docker: POKEMON_ADMIN_USERS) steht.",
        )
    return current_user
