"""Login with a Home Assistant account (auth mode `ha_oauth`).

For installs without Supervisor (Home Assistant Container, Docker) there is no
Ingress that tells us who the user is. Instead the app uses HA's built-in
OAuth2 / IndieAuth flow - the same one the HA mobile apps use:

1. `/auth/login` redirects the browser to `<HA>/auth/authorize` with our public
   URL as `client_id` and `/auth/callback` (same host) as `redirect_uri`. HA
   accepts any client whose redirect URI shares the client ID's host, so no
   registration in HA is needed.
2. HA shows its normal login page and redirects back with a one-time `code`.
3. `/auth/callback` exchanges the code for tokens (`POST <HA>/auth/token`), asks
   HA who that is (WebSocket `auth/current_user`) and immediately revokes the
   refresh token again - the app only needs the identity, not API access.
4. A signed session cookie (see `auth.sign`) keeps the user logged in.

Two HA URLs can differ: `POKEMON_HA_URL` is how the *container* reaches HA,
`POKEMON_HA_EXTERNAL_URL` how the *browser* reaches it (defaults to the first).
"""

from __future__ import annotations

import logging
import os
import secrets
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import auth, i18n
from ..config import settings
from ..ha_client import HomeAssistantClient, HomeAssistantError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

STATE_COOKIE = "pokehoas_oauth_state"
STATE_MAX_AGE_SECONDS = 600


# --------------------------------------------------------------- Helpers

def public_url(request: Request) -> str:
    """Base URL of this app as the browser sees it, without trailing slash."""
    configured = os.getenv("POKEMON_PUBLIC_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def ha_external_url() -> str:
    return (os.getenv("POKEMON_HA_EXTERNAL_URL", "").strip() or settings.ha_url).rstrip("/")


def _cookie_options(request: Request) -> dict:
    base = public_url(request)
    secure = base.startswith("https://")
    samesite = (os.getenv("POKEMON_COOKIE_SAMESITE", "lax").strip().lower() or "lax")
    if samesite not in ("lax", "strict", "none"):
        samesite = "lax"
    if samesite == "none":
        # Browsers drop SameSite=None cookies without Secure.
        secure = True
    return {
        "path": urlparse(base).path or "/",
        "httponly": True,
        "secure": secure,
        "samesite": samesite,
    }


def _require_oauth_mode() -> None:
    if auth.auth_mode() != "ha_oauth":
        raise HTTPException(status_code=404, detail="Login is only used in auth mode ha_oauth.")


def _language(request: Request) -> str:
    header = request.headers.get("accept-language", "")
    for part in header.split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in i18n.available_languages():
            return code
    return i18n.DEFAULT_LANGUAGE


def _message_page(request: Request, key: str, *, popup: bool, ok: bool) -> HTMLResponse:
    """Tiny page after login: closes the popup (iframe embed) or shows an error."""
    lang = _language(request)
    text = i18n.t(lang, key)
    back = i18n.t(lang, "login.back")
    home = public_url(request) + "/"
    script = ""
    if popup and ok:
        script = (
            "<script>try{if(window.opener){window.opener.postMessage('pokehoas-login','*');"
            "window.close();}}catch(e){}</script>"
        )
    html = (
        f"<!doctype html><html lang=\"{lang}\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>PokeHOAS</title><style>body{font-family:system-ui,sans-serif;"
        "display:grid;place-items:center;min-height:90vh;margin:0 16px;text-align:center}"
        "</style></head><body><div><p>"
        f"{_escape(text)}</p><p><a href=\"{_escape(home)}\">{_escape(back)}</a></p></div>"
        f"{script}</body></html>"
    )
    return HTMLResponse(html, status_code=200 if ok else 400)


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


# ------------------------------------------------------------- HA calls
# Module-level so tests can replace them.

async def exchange_code(code: str, client_id: str) -> dict:
    """POST <HA>/auth/token (grant_type=authorization_code)."""
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{settings.ha_url}/auth/token",
            data={"grant_type": "authorization_code", "code": code, "client_id": client_id},
        )
    if response.status_code != 200:
        raise HomeAssistantError(f"Token exchange failed: HTTP {response.status_code}")
    return response.json()


async def fetch_current_user(access_token: str) -> dict:
    """WebSocket `auth/current_user` with the user's own access token."""
    client = HomeAssistantClient(token=access_token)
    try:
        return await client._ws_command("auth/current_user")
    finally:
        await client.close()


async def revoke_refresh_token(refresh_token: str) -> None:
    """Remove the refresh token from the user's HA profile again (best effort)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{settings.ha_url}/auth/revoke", data={"token": refresh_token})
    except httpx.HTTPError as exc:
        log.info("Could not revoke HA refresh token: %s", exc)


# ---------------------------------------------------------------- Routes

@router.get("/login", include_in_schema=False)
def login(request: Request, popup: bool = False) -> Response:
    _require_oauth_mode()
    base = public_url(request)
    nonce = secrets.token_urlsafe(24)
    state = auth.sign({"n": nonce, "p": 1 if popup else 0}, max_age=STATE_MAX_AGE_SECONDS)
    query = urlencode({
        "response_type": "code",
        "client_id": base + "/",
        "redirect_uri": base + "/auth/callback",
        "state": state,
    })
    response = RedirectResponse(f"{ha_external_url()}/auth/authorize?{query}", status_code=302)
    response.set_cookie(STATE_COOKIE, nonce, max_age=STATE_MAX_AGE_SECONDS, **_cookie_options(request))
    return response


@router.get("/callback", include_in_schema=False)
async def callback(request: Request, code: str = "", state: str = "") -> Response:
    _require_oauth_mode()
    payload = auth.unsign(state)
    popup = bool(payload and payload.get("p"))
    nonce_cookie = request.cookies.get(STATE_COOKIE) or ""
    if not payload or not code or not secrets.compare_digest(str(payload.get("n", "")), nonce_cookie):
        return _message_page(request, "login.failed", popup=popup, ok=False)

    try:
        tokens = await exchange_code(code, public_url(request) + "/")
        user = await fetch_current_user(tokens["access_token"])
    except (HomeAssistantError, KeyError, ValueError, OSError) as exc:
        log.warning("HA login failed: %s", exc)
        return _message_page(request, "login.failed", popup=popup, ok=False)

    if tokens.get("refresh_token"):
        await revoke_refresh_token(tokens["refresh_token"])

    user_id = str(user.get("id") or "")
    if not user_id:
        return _message_page(request, "login.failed", popup=popup, ok=False)
    name = str(user.get("name") or user_id)
    is_admin = bool(user.get("is_admin") or user.get("is_owner"))
    auth.remember_login_admin(user_id, is_admin)
    log.info("HA user %s logged in (admin=%s)", name, is_admin)

    if popup:
        response: Response = _message_page(request, "login.done", popup=True, ok=True)
    else:
        response = RedirectResponse(public_url(request) + "/", status_code=302)
    options = _cookie_options(request)
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.sign({"uid": user_id, "name": name, "dn": name}),
        max_age=auth.SESSION_MAX_AGE_SECONDS,
        **options,
    )
    response.delete_cookie(STATE_COOKIE, path=options["path"])
    return response


@router.get("/logout", include_in_schema=False)
def logout(request: Request) -> Response:
    response = RedirectResponse(public_url(request) + "/", status_code=302)
    options = _cookie_options(request)
    response.delete_cookie(auth.SESSION_COOKIE, path=options["path"],
                           secure=options["secure"], samesite=options["samesite"])
    return response


@router.get("/status", include_in_schema=False)
def status(request: Request) -> dict:
    """Which auth mode is active and whether this browser has a session."""
    mode = auth.auth_mode()
    logged_in = mode != "ha_oauth" or auth.unsign(request.cookies.get(auth.SESSION_COOKIE)) is not None
    return {"mode": mode, "logged_in": logged_in}
