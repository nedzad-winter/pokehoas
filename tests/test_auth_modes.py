"""Tests for the auth modes (ingress / ha_oauth / trusted_header / dev).

Aufruf:  python3 tests/test_auth_modes.py
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-auth-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"
os.environ.pop("POKEMON_AUTH_MODE", None)
os.environ.pop("POKEMON_SESSION_SECRET", None)

from fastapi.testclient import TestClient  # noqa: E402

from app import admin, auth, db, seed  # noqa: E402
from app.api import login as login_api  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"[{status}] {label}{(' - ' + str(detail)) if detail != '' else ''}")
    if not condition:
        failures.append(label)


db.init_db()
seed.seed_from_fallback()
logging.getLogger("httpx").setLevel(logging.WARNING)

HA_USERS = [
    {"id": "alice-id", "name": "Alice", "username": "alice", "is_owner": False,
     "is_active": True, "group_ids": ["system-admin"]},
    {"id": "bob-id", "name": "Bob", "username": "bob", "is_owner": False,
     "is_active": True, "group_ids": ["system-users"]},
]
ha_list = {"fail": False}


async def fake_fetch() -> list[dict]:
    if ha_list["fail"]:
        raise RuntimeError("token may not list users")
    return [dict(u) for u in HA_USERS]


admin._fetch_ha_users = fake_fetch


def set_mode(mode: str | None) -> None:
    if mode is None:
        os.environ.pop("POKEMON_AUTH_MODE", None)
    else:
        os.environ["POKEMON_AUTH_MODE"] = mode
    admin.clear_cache()


# ------------------------------------------------------- Mode resolution
set_mode(None)
check("Without explicit mode + SUPERVISOR_TOKEN: ingress", auth.auth_mode() == "ingress")
os.environ.pop("SUPERVISOR_TOKEN")
check("Without explicit mode and token: dev", auth.auth_mode() == "dev")
os.environ["SUPERVISOR_TOKEN"] = "dummy"
set_mode("HA_OAUTH")
check("Mode is case-insensitive", auth.auth_mode() == "ha_oauth")
set_mode("nonsense")
try:
    auth.auth_mode()
    check("Unknown mode is rejected", False)
except RuntimeError:
    check("Unknown mode is rejected", True)
set_mode("ha_oauth")
check("Explicit mode wins over missing SUPERVISOR_TOKEN", auth.dev_mode() is False)

# ---------------------------------------------------------- Signed tokens
token = auth.sign({"uid": "x"})
check("Signed token round-trips", (auth.unsign(token) or {}).get("uid") == "x")
data, mac = token.rsplit(".", 1)
check("Tampered payload is rejected", auth.unsign(data[:-2] + "AA." + mac) is None)
check("Garbage is rejected", auth.unsign("abc") is None and auth.unsign(None) is None)
check("Expired token is rejected", auth.unsign(auth.sign({"uid": "x"}, max_age=-5)) is None)
check("Secret is persisted in /data", (Path(TMP) / "session_secret").exists())
os.environ["POKEMON_SESSION_SECRET"] = "another-secret-from-env-0123456789"
check("Token signed with old secret is invalid under new secret", auth.unsign(token) is None)
os.environ.pop("POKEMON_SESSION_SECRET")

# ------------------------------------------------------------- ha_oauth
set_mode("ha_oauth")
os.environ["POKEMON_PUBLIC_URL"] = "https://pokemon.example.com/"
os.environ["POKEMON_HA_EXTERNAL_URL"] = "https://ha.example.com"
client = TestClient(app, base_url="https://pokemon.example.com", follow_redirects=False)

r = client.get("/api/users/me")
check("ha_oauth: no session -> 401", r.status_code == 401, r.status_code)
check("ha_oauth: 401 tells the frontend to log in",
      (r.json().get("detail") or {}).get("code") == "login_required", r.json())
r = client.get("/api/users/me", headers={"X-Remote-User-Id": "alice-id"})
check("ha_oauth: Ingress headers are ignored", r.status_code == 401)
check("ha_oauth: status endpoint reports no session",
      client.get("/auth/status").json() == {"mode": "ha_oauth", "logged_in": False})

r = client.get("/auth/login")
location = r.headers.get("location", "")
query = parse_qs(urlparse(location).query)
check("Login redirects to HA authorize",
      r.status_code == 302 and location.startswith("https://ha.example.com/auth/authorize?"), location)
check("client_id is the public URL", query.get("client_id") == ["https://pokemon.example.com/"], query)
check("redirect_uri shares the client host",
      query.get("redirect_uri") == ["https://pokemon.example.com/auth/callback"])
state = query["state"][0]
check("State cookie is set", bool(client.cookies.get(login_api.STATE_COOKIE)))

calls: dict[str, list] = {"exchange": [], "revoke": []}


async def fake_exchange(code: str, client_id: str) -> dict:
    calls["exchange"].append((code, client_id))
    return {"access_token": "acc-" + code, "refresh_token": "ref-" + code}


async def fake_current_user(access_token: str) -> dict:
    who = access_token.removeprefix("acc-")
    return {"alice": {"id": "alice-id", "name": "Alice", "is_admin": True},
            "bob": {"id": "bob-id", "name": "Bob", "is_admin": False}}[who]


async def fake_revoke(refresh_token: str) -> None:
    calls["revoke"].append(refresh_token)


login_api.exchange_code = fake_exchange
login_api.fetch_current_user = fake_current_user
login_api.revoke_refresh_token = fake_revoke

r = client.get("/auth/callback", params={"code": "alice", "state": "forged"})
check("Forged state is rejected", r.status_code == 400 and not calls["exchange"])

r = client.get("/auth/callback", params={"code": "alice", "state": state})
check("Callback redirects home", r.status_code == 302
      and r.headers.get("location") == "https://pokemon.example.com/", r.status_code)
check("Code exchanged with the same client_id",
      calls["exchange"] == [("alice", "https://pokemon.example.com/")], calls["exchange"])
check("HA refresh token is revoked right away", calls["revoke"] == ["ref-alice"])
set_cookie = r.headers.get("set-cookie", "").lower()
check("Session cookie is HttpOnly + Secure (https public URL)",
      "httponly" in set_cookie and "secure" in set_cookie, set_cookie)

me = client.get("/api/users/me").json()
check("Session identifies the HA user", me.get("id") == "alice-id", me)
check("HA admin is admin (user list)", me.get("is_admin") is True)
check("Status endpoint reports a session", client.get("/auth/status").json()["logged_in"] is True)

ha_list["fail"] = True
admin.clear_cache()
check("Admin falls back to login flag when token may not list users",
      client.get("/api/users/me").json().get("is_admin") is True)
ha_list["fail"] = False
admin.clear_cache()

r = client.get("/auth/logout")
check("Logout redirects home", r.status_code == 302)
check("Logout removes the session", client.get("/api/users/me").status_code == 401)

# Popup flow (iframe embed): page instead of redirect, posts back to opener.
state = parse_qs(urlparse(client.get("/auth/login", params={"popup": "true"})
                          .headers["location"]).query)["state"][0]
r = client.get("/auth/callback", params={"code": "bob", "state": state})
check("Popup callback returns a closing page",
      r.status_code == 200 and "pokehoas-login" in r.text and "window.close" in r.text)
check("Bob is logged in and no admin",
      client.get("/api/users/me").json().get("is_admin") is False)

os.environ["POKEMON_COOKIE_SAMESITE"] = "none"
os.environ["POKEMON_PUBLIC_URL"] = "http://192.168.1.20:8099"
r = client.get("/auth/login")
cookie = r.headers.get("set-cookie", "").lower()
check("SameSite=None forces Secure", "samesite=none" in cookie and "secure" in cookie, cookie)
os.environ.pop("POKEMON_COOKIE_SAMESITE")
r = client.get("/auth/login")
check("Plain http public URL: no Secure flag", "secure" not in r.headers.get("set-cookie", "").lower())

set_mode("ingress")
check("Login routes are disabled outside ha_oauth", client.get("/auth/login").status_code == 404)

# -------------------------------------------------------- trusted_header
set_mode("trusted_header")
os.environ["POKEMON_TRUSTED_PROXIES"] = "10.0.0.0/24, fd00::1, not-an-ip"


def fake_request(host: str | None) -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=host) if host else None)


check("Proxy inside CIDR is trusted", auth.peer_is_trusted_proxy(fake_request("10.0.0.7")))
check("IPv6 proxy is trusted", auth.peer_is_trusted_proxy(fake_request("fd00::1")))
check("Other peer is not trusted", not auth.peer_is_trusted_proxy(fake_request("192.168.1.5")))
check("Non-IP peer is not trusted", not auth.peer_is_trusted_proxy(fake_request("testclient")))

plain = TestClient(app)
r = plain.get("/api/users/me", headers={"Remote-User": "alice"})
check("Header from untrusted peer is rejected", r.status_code == 401)

original_peer_check = auth.peer_is_trusted_proxy
auth.peer_is_trusted_proxy = lambda request: True
me = plain.get("/api/users/me", headers={"Remote-User": "Alice", "Remote-Name": "Alice A."}).json()
check("Proxy user maps to HA user with same username", me.get("id") == "alice-id", me)
check("Mapped proxy user keeps HA admin rights", me.get("is_admin") is True)
me = plain.get("/api/users/me", headers={"Remote-User": "carl"}).json()
check("Unknown proxy user gets a prefixed ID", me.get("id") == "proxy:carl", me)
check("Unknown proxy user is no admin", me.get("is_admin") is False)
r = plain.get("/api/users/me")
check("Missing user header -> 401", r.status_code == 401)
os.environ["POKEMON_TRUSTED_USER_HEADER"] = "X-Forwarded-User"
me = plain.get("/api/users/me", headers={"X-Forwarded-User": "bob"}).json()
check("User header name is configurable", me.get("id") == "bob-id", me)
os.environ.pop("POKEMON_TRUSTED_USER_HEADER")
auth.peer_is_trusted_proxy = original_peer_check

# ------------------------------------------------------- ingress / dev
set_mode("ingress")
check("ingress: header user works", plain.get(
    "/api/users/me", headers={"X-Remote-User-Id": "bob-id"}).json().get("id") == "bob-id")
check("ingress: no header -> 401", plain.get("/api/users/me").status_code == 401)
set_mode("dev")
check("dev: fallback user", plain.get("/api/users/me").json().get("id") == auth.DEV_USER_ID)
set_mode(None)

# --------------------------------------------------- entrypoint checks
sys.path.insert(0, str(ROOT / "pokemon_encounter"))
import entrypoint  # noqa: E402

env_ok = {"POKEMON_AUTH_MODE": "ha_oauth", "POKEMON_HA_URL": "http://ha:8123",
          "POKEMON_HA_TOKEN": "t"}
check("Entrypoint accepts a valid standalone config", entrypoint.config_problems(env_ok) == [])
check("Entrypoint requires a HA token outside the add-on",
      any("POKEMON_HA_TOKEN" in p for p in entrypoint.config_problems(
          {**env_ok, "POKEMON_HA_TOKEN": ""})))
check("Entrypoint rejects trusted_header without proxies",
      any("POKEMON_TRUSTED_PROXIES" in p for p in entrypoint.config_problems(
          {**env_ok, "POKEMON_AUTH_MODE": "trusted_header"})))
check("Entrypoint refuses implicit dev mode in the standalone image",
      any("dev" in p for p in entrypoint.config_problems(
          {**env_ok, "POKEMON_AUTH_MODE": "dev", "POKEMON_STANDALONE": "1"})))
check("Entrypoint allows dev mode when explicitly confirmed", entrypoint.config_problems(
    {**env_ok, "POKEMON_AUTH_MODE": "dev", "POKEMON_STANDALONE": "1",
     "POKEMON_ALLOW_DEV_MODE": "1"}) == [])
check("Entrypoint maps HA log levels for uvicorn",
      entrypoint.uvicorn_log_level("notice") == "info"
      and entrypoint.uvicorn_log_level("fatal") == "critical"
      and entrypoint.uvicorn_log_level("debug") == "debug")

print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("Alle Tests bestanden.")
