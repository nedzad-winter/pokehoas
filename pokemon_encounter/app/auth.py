"""User detection - one of several explicit auth modes.

`POKEMON_AUTH_MODE` decides who the requester is:

- `ingress` (default inside the Supervisor add-on): the web app runs via Ingress
  *within* the authenticated HA interface. HA passes the identity of the
  logged-in user as headers (`X-Remote-User-Id`, `X-Remote-User-Name`,
  `X-Remote-User-Display-Name`). No separate login management.
- `ha_oauth` (default of the standalone Docker image, for "Home Assistant
  Container" installs without Supervisor): the user logs in with their HA
  account via HA's own OAuth2 flow (see `app/api/login.py`). Afterwards a signed
  session cookie carries the HA user ID, so per-user data and admin detection
  work exactly like in the add-on - a database can even move between both.
- `trusted_header`: a reverse proxy with forward auth (Authelia, Authentik, ...)
  sets the user name in a header. Only accepted from `POKEMON_TRUSTED_PROXIES`.
- `dev`: local `uvicorn --reload` without HA; everyone is a fixed dev user.

Without an explicit mode the old behaviour applies: `ingress` when a
`SUPERVISOR_TOKEN` is present, otherwise `dev`. The standalone image always sets
the mode explicitly, so it can never fall into `dev` by accident.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import HTTPException, Request

from . import db
from .config import settings

log = logging.getLogger(__name__)

_warned_dev_fallback = False

DEV_USER_ID = os.getenv("POKEMON_DEV_USER_ID", "dev-local")
DEV_USER_NAME = os.getenv("POKEMON_DEV_USER_NAME", "Dev")

AUTH_MODES = ("ingress", "ha_oauth", "trusted_header", "dev")

SESSION_COOKIE = "pokehoas_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 3600

# Prefix for users that only exist behind a forward-auth proxy (no matching HA
# account), so their IDs can never collide with real HA user IDs.
PROXY_USER_PREFIX = "proxy:"


# ------------------------------------------------------------------ Mode

def auth_mode() -> str:
    """Active auth mode. Read at call time (not import time) so tests can switch."""
    raw = (os.getenv("POKEMON_AUTH_MODE") or "").strip().lower()
    if raw:
        if raw not in AUTH_MODES:
            raise RuntimeError(
                f"Unknown POKEMON_AUTH_MODE {raw!r} - expected one of {', '.join(AUTH_MODES)}"
            )
        return raw
    return "ingress" if os.getenv("SUPERVISOR_TOKEN") else "dev"


def dev_mode() -> bool:
    return auth_mode() == "dev"


def _dev_fallback_allowed() -> bool:
    # Kept for backwards compatibility with older callers.
    return dev_mode()


# --------------------------------------------------------------- Sessions

def _secret_file() -> Path:
    return settings.data_dir / "session_secret"


_secret_cache: bytes | None = None


def session_secret() -> bytes:
    """HMAC key for session cookies: env var, else a random key kept in /data."""
    global _secret_cache
    env = os.getenv("POKEMON_SESSION_SECRET")
    if env:
        return env.encode("utf-8")
    if _secret_cache is not None:
        return _secret_cache
    path = _secret_file()
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = ""
    if len(value) < 32:
        value = secrets.token_urlsafe(48)
        path.write_text(value, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    _secret_cache = value.encode("utf-8")
    return _secret_cache


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload: dict, max_age: int = SESSION_MAX_AGE_SECONDS) -> str:
    """Signed, expiring token (`<b64 json>.<b64 hmac>`) - no extra dependency."""
    body = dict(payload, exp=int(time.time()) + max_age)
    data = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    mac = hmac.new(session_secret(), data.encode("ascii"), hashlib.sha256).digest()
    return f"{data}.{_b64(mac)}"


def unsign(token: str | None) -> dict | None:
    """Payload of a valid, unexpired token, else None."""
    if not token or "." not in token:
        return None
    data, mac = token.rsplit(".", 1)
    expected = _b64(hmac.new(session_secret(), data.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        payload = json.loads(_unb64(data))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or int(payload.get("exp", 0)) < time.time():
        return None
    return payload


# Admin flag HA reported for a user at login (`auth/current_user`). Fallback for
# admin.py when the configured token may not list HA users.
_login_admin_flags: dict[str, bool] = {}


def remember_login_admin(user_id: str, is_admin: bool) -> None:
    _login_admin_flags[user_id] = bool(is_admin)


def login_admin_flag(user_id: str) -> bool | None:
    return _login_admin_flags.get(user_id)


# ---------------------------------------------------------- Trusted header

def _trusted_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks = []
    raw = os.getenv("POKEMON_TRUSTED_PROXIES", "")
    for part in raw.replace(",", " ").split():
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("POKEMON_TRUSTED_PROXIES: ignoring invalid entry %r", part)
    return networks


def peer_is_trusted_proxy(request: Request) -> bool:
    host = request.client.host if request.client else None
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in _trusted_networks())


def _header_user(request: Request) -> tuple[str, str, str] | None:
    if not peer_is_trusted_proxy(request):
        log.warning(
            "trusted_header: request from %s is not in POKEMON_TRUSTED_PROXIES - rejected",
            request.client.host if request.client else "?",
        )
        return None
    user_header = os.getenv("POKEMON_TRUSTED_USER_HEADER", "Remote-User")
    name_header = os.getenv("POKEMON_TRUSTED_NAME_HEADER", "Remote-Name")
    username = (request.headers.get(user_header) or "").strip()
    if not username:
        return None
    display = (request.headers.get(name_header) or "").strip() or username
    return username, username, display


def resolve_proxy_user(username: str, ha_users: dict[str, dict] | None) -> str:
    """Map a proxy user name to the HA user with the same user name, if any.

    That keeps admin detection and data compatible with the add-on; unknown
    names get a `proxy:` ID that can never clash with a real HA user ID.
    """
    wanted = username.casefold()
    for user_id, user in (ha_users or {}).items():
        if str(user.get("username") or "").casefold() == wanted:
            return user_id
    return PROXY_USER_PREFIX + username


# -------------------------------------------------------------- Dependency

def _login_required() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "login_required", "login_url": "/auth/login"},
    )


async def get_current_user(request: Request) -> dict:
    """FastAPI dependency: returns the `app_user` record of the requester.

    Automatically creates the user on first call (no separate registration form
    needed - HA already authenticated).
    """
    mode = auth_mode()

    if mode == "ha_oauth":
        session = unsign(request.cookies.get(SESSION_COOKIE))
        if not session or not session.get("uid"):
            raise _login_required()
        uid = str(session["uid"])
        name = str(session.get("name") or uid)
        return db.upsert_user(uid, name, str(session.get("dn") or name))

    if mode == "trusted_header":
        found = _header_user(request)
        if not found:
            raise HTTPException(status_code=401, detail="No authenticated user from the reverse proxy.")
        username, name, display_name = found
        from . import admin  # local import: admin imports this module

        user_id = resolve_proxy_user(username, await admin.ha_users())
        return db.upsert_user(user_id, name, display_name)

    user_id = request.headers.get("x-remote-user-id")
    name = request.headers.get("x-remote-user-name")
    display_name = request.headers.get("x-remote-user-display-name")

    if not user_id:
        if mode == "dev":
            global _warned_dev_fallback
            if not _warned_dev_fallback:
                log.warning(
                    "Kein Ingress-User-Header gefunden - nutze Dev-Fallback-Nutzer "
                    "'%s'. Das ist normal bei lokalem `uvicorn --reload`, aber nicht "
                    "in einem echten Home-Assistant-Add-on.",
                    DEV_USER_ID,
                )
                _warned_dev_fallback = True
            user_id, name, display_name = DEV_USER_ID, DEV_USER_NAME, DEV_USER_NAME
        else:
            raise HTTPException(
                status_code=401,
                detail=(
                    "Kein Home-Assistant-Nutzer erkannt. Bitte die App ueber das "
                    "Pokémon-Panel in der Home-Assistant-Seitenleiste oeffnen."
                ),
            )

    return db.upsert_user(user_id, name or user_id, display_name or name or user_id)
