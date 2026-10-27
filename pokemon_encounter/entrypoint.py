"""Container entrypoint - shared by the Supervisor add-on and the standalone image.

The add-on's `run.sh` translates the Supervisor options into environment
variables (via bashio) and then execs this script. The standalone Docker image
(`Dockerfile.standalone`, for Home Assistant Container installs) runs it
directly; there the user sets the variables in docker-compose.

This script only validates the configuration and starts uvicorn. Everything
else is read by the app itself (`app/config.py`, `app/auth.py`,
`app/trigger_sync.py`).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

AUTH_MODES = ("ingress", "ha_oauth", "trusted_header", "dev")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def resolve_auth_mode(env: Mapping[str, str]) -> str:
    raw = (env.get("POKEMON_AUTH_MODE") or "").strip().lower()
    if raw:
        return raw
    return "ingress" if env.get("SUPERVISOR_TOKEN") else "dev"


def config_problems(env: Mapping[str, str]) -> list[str]:
    """Fatal configuration errors (empty list = OK to start)."""
    problems: list[str] = []
    mode = resolve_auth_mode(env)
    supervisor = bool(env.get("SUPERVISOR_TOKEN"))

    if mode not in AUTH_MODES:
        problems.append(f"POKEMON_AUTH_MODE={mode!r} is unknown, use one of: {', '.join(AUTH_MODES)}")

    if not supervisor:
        if not (env.get("POKEMON_HA_URL") or "").strip():
            problems.append("POKEMON_HA_URL is missing (how the container reaches Home Assistant, "
                            "e.g. http://192.168.1.10:8123)")
        if not (env.get("POKEMON_HA_TOKEN") or "").strip():
            problems.append("POKEMON_HA_TOKEN is missing (long-lived access token of a Home "
                            "Assistant administrator, created in your HA profile -> Security)")
        if mode == "ingress":
            problems.append("POKEMON_AUTH_MODE=ingress only works inside the Supervisor add-on; "
                            "use ha_oauth or trusted_header")

    if mode == "trusted_header" and not (env.get("POKEMON_TRUSTED_PROXIES") or "").strip():
        problems.append("POKEMON_AUTH_MODE=trusted_header needs POKEMON_TRUSTED_PROXIES "
                        "(IP/CIDR of your reverse proxy), otherwise anyone could fake the header")

    if (mode == "dev" and _truthy(env.get("POKEMON_STANDALONE"))
            and not _truthy(env.get("POKEMON_ALLOW_DEV_MODE"))):
        problems.append("POKEMON_AUTH_MODE=dev makes every visitor the same admin user. "
                        "Set POKEMON_ALLOW_DEV_MODE=1 if that is really intended")

    raw_interval = (env.get("POKEMON_POLL_INTERVAL_MINUTES") or "5").strip()
    if not raw_interval.isdigit() or not 1 <= int(raw_interval) <= 60:
        problems.append("POKEMON_POLL_INTERVAL_MINUTES must be a whole number between 1 and 60")

    return problems


def config_warnings(env: Mapping[str, str]) -> list[str]:
    warnings: list[str] = []
    if resolve_auth_mode(env) == "ha_oauth" and not (env.get("POKEMON_PUBLIC_URL") or "").strip():
        warnings.append("POKEMON_PUBLIC_URL is not set - the login derives the app URL from each "
                        "request. Set it to the URL you open in the browser.")
    return warnings


def uvicorn_log_level(level: str) -> str:
    # uvicorn knows neither "notice" nor "fatal" (HA log levels).
    level = (level or "info").strip().lower()
    return {"notice": "info", "fatal": "critical"}.get(level, level)


def main() -> None:
    env = os.environ
    for warning in config_warnings(env):
        print(f"[pokehoas] WARNING: {warning}", file=sys.stderr, flush=True)
    problems = config_problems(env)
    if problems:
        for problem in problems:
            print(f"[pokehoas] ERROR: {problem}", file=sys.stderr, flush=True)
        sys.exit(1)

    port = env.get("POKEMON_HTTP_PORT", "8099")
    print(f"[pokehoas] Starting on port {port} (auth mode {resolve_auth_mode(env)})", flush=True)
    os.chdir(Path(__file__).resolve().parent)
    os.execvp(sys.executable, [
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--host", env.get("POKEMON_HTTP_HOST", "0.0.0.0"),
        "--port", port,
        "--log-level", uvicorn_log_level(env.get("POKEMON_LOG_LEVEL", "info")),
        # The client address must stay the direct peer: trusted_header checks it
        # against POKEMON_TRUSTED_PROXIES. The app never needs X-Forwarded-*.
        "--no-proxy-headers",
    ])


if __name__ == "__main__":
    main()
