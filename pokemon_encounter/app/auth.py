"""User detection via Home Assistant.

The web app runs via Ingress *within* the authenticated HA interface. HA passes
the identity of the logged-in user as a header to the add-on (see HA docs on
Ingress): `X-Remote-User-Id`, `X-Remote-User-Name`, `X-Remote-User-Display-Name`.
The add-on thus needs no separate login management - whoever is logged in to HA
is automatically logged in here too, with their own team/inventory/Pokedex.

For local development (README: "run locally" via uvicorn outside HA) these
headers naturally don't exist. So `--reload` still works, the service falls
back to a fixed dev user (configurable via env vars). With real Ingress access
without headers - e.g. incorrectly configured reverse proxy - a clear error is
returned instead of silently mixing users.
"""

from __future__ import annotations

import logging
import os

from fastapi import HTTPException, Request

from . import db

log = logging.getLogger(__name__)

_warned_dev_fallback = False

DEV_USER_ID = os.getenv("POKEMON_DEV_USER_ID", "dev-local")
DEV_USER_NAME = os.getenv("POKEMON_DEV_USER_NAME", "Dev")


def _dev_fallback_allowed() -> bool:
    # Only outside a real add-on start (no SUPERVISOR_TOKEN from HA).
    return not bool(os.getenv("SUPERVISOR_TOKEN"))


def get_current_user(request: Request) -> dict:
    """FastAPI dependency: returns the `app_user` record of the requester.

    Automatically creates the user on first call (no separate registration form
    needed - HA already authenticated).
    """
    user_id = request.headers.get("x-remote-user-id")
    name = request.headers.get("x-remote-user-name")
    display_name = request.headers.get("x-remote-user-display-name")

    if not user_id:
        if _dev_fallback_allowed():
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
