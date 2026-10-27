"""Entry point of the add-on."""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, backup, db, gifts, seed
from .api import admin as admin_api
from .api import auto_rules as auto_rules_api
from .api import login as login_api
from .api import abilitydex, battles, breeding as breeding_api, buddy as buddy_api, challenges as challenges_api, config_routes, encounters, i18n as i18n_api, league, movedex, pvp as pvp_api, shop, team, trading as trading_api, users
from . import abilities, breeding, evolution, tms
from .config import settings
from .runtime import ha_client, poller

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("pokemon")


def _optional_extension(name: str):
    """Loads the optional dev-only module `app/api/<name>.py`, or returns None
    when it isn't installed (it is not part of the public repository). A module
    that exists but fails to import still raises."""
    module = f"{__package__}.api.{name}"
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name != module:
            raise
        return None


# Extensions expose `router`, `init_db()` and `FRONTEND_SCRIPT` (a file in www/
# that index.html loads, see GET /api/extensions).
EXTENSIONS = [ext for ext in (_optional_extension("agent_tasks"),) if ext is not None]


async def _heartbeat(start: float) -> None:
    """Output a heartbeat log every 60 seconds."""
    while True:
        await asyncio.sleep(60)
        uptime = int(time.monotonic() - start)
        with db.cursor() as cur:
            open_enc = cur.execute(
                "SELECT COUNT(*) FROM encounter WHERE status = 'waiting'"
            ).fetchone()[0]
        log.info("PokeHOAS running for %ds | open encounters: %d", uptime, open_enc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("PokeHOAS %s starting", __version__)
    db.init_db()
    for extension in EXTENSIONS:
        extension.init_db()
    # One-off gifts to named players (granted once, see gifts.py).
    gifts.grant_pending()
    # Starting inventory is no longer distributed globally, but per user when
    # choosing the starter (see app/api/users.py) - there are now potentially
    # multiple players.

    # Seed Pokédex in the background so startup doesn't block.
    heartbeat_task = asyncio.create_task(_heartbeat(time.monotonic()))
    species_seed_task = asyncio.create_task(seed.ensure_seeded())
    # Evolution chains in the background too - also on existing databases.
    evolution.start_background_seed()
    # Ability assignments (species_ability) as well - independent of
    # Pokédex seeding on an existing database (see abilities.py), but on
    # a FRESH database it waits for the Pokédex seed task: otherwise it checks
    # "which species are still missing?" against an empty `species` table
    # and incorrectly considers everything done.
    abilities.start_background_seed(after=species_seed_task)
    # name_en backfill (English display names) for databases that were already
    # completely seeded before the i18n migration - see seed.py.
    seed.start_name_en_backfill(after=species_seed_task)
    # Egg groups / egg cycles for breeding (see breeding.py), same pattern.
    breeding.start_background_backfill(after=species_seed_task)
    # TM learnsets (species.tm_moves, see tms.py), same pattern.
    tms.start_background_backfill(after=species_seed_task)

    poller.start()
    # Periodic DB snapshots (off in the add-on, on in the standalone image).
    backup.start()
    try:
        yield
    finally:
        heartbeat_task.cancel()
        await backup.stop()
        await poller.stop()
        await ha_client.close()
        log.info("PokeHOAS stopped")


app = FastAPI(
    title="PokeHOAS",
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

# HA account login for auth mode ha_oauth (Docker without Supervisor).
app.include_router(login_api.router)
# Admin panel (all endpoints behind require_admin, see app/admin.py).
app.include_router(admin_api.router)
app.include_router(config_routes.router)
app.include_router(encounters.router)
app.include_router(auto_rules_api.router)
app.include_router(users.router)
app.include_router(shop.router)
app.include_router(battles.router)
app.include_router(team.router)
app.include_router(movedex.router)
app.include_router(abilitydex.router)
app.include_router(league.router)
app.include_router(pvp_api.router)
app.include_router(challenges_api.router)
app.include_router(trading_api.router)
app.include_router(i18n_api.router)
app.include_router(buddy_api.router)
app.include_router(breeding_api.router)
for _extension in EXTENSIONS:
    app.include_router(_extension.router)


@app.get("/api/extensions", include_in_schema=False)
def extensions() -> dict[str, list[str]]:
    """Frontend scripts of the installed optional extensions (empty in the public build)."""
    return {"scripts": [ext.FRONTEND_SCRIPT for ext in EXTENSIONS]}


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


if settings.www_dir.exists():
    app.mount(
        "/static", StaticFiles(directory=settings.www_dir), name="static"
    )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(settings.www_dir / "index.html")
