"""Database snapshots.

Inside the Supervisor add-on, `/data` is part of every HA backup, so nothing is
needed here by default. Without Supervisor (standalone Docker image) nobody
backs the volume up, so the image enables periodic snapshots:

- `POKEMON_BACKUP_INTERVAL_HOURS` (0 = off, add-on default; image default 24)
- `POKEMON_BACKUP_KEEP` how many snapshots to keep (default 7)

Snapshots go to `<data>/backups/` and use SQLite's online backup API, so they
are consistent even while the app writes. Admins can also download a fresh
snapshot (`GET /api/admin/backup`). Restoring = stop the container, replace
`pokemon.sqlite3` with a snapshot, start again.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import settings

log = logging.getLogger(__name__)

PREFIX = "pokemon-"
SUFFIX = ".sqlite3"

_task: asyncio.Task | None = None


def backup_dir() -> Path:
    return settings.data_dir / "backups"


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        log.warning("%s is not a number - using %d", name, default)
        return default


def interval_hours() -> int:
    return _int_env("POKEMON_BACKUP_INTERVAL_HOURS", 0)


def keep_count() -> int:
    return max(1, _int_env("POKEMON_BACKUP_KEEP", 7))


def create_snapshot(target_dir: Path | None = None, name: str | None = None) -> Path:
    """Consistent copy of the live database; returns the new file."""
    target_dir = target_dir or backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = target_dir / (name or f"{PREFIX}{stamp}{SUFFIX}")
    tmp = target.with_suffix(target.suffix + ".tmp")
    source = db.connect()
    dest = sqlite3.connect(tmp)
    try:
        # Hold the app's DB lock so no transaction is half-written mid-copy.
        with db._lock:
            source.backup(dest)
    finally:
        dest.close()
    tmp.replace(target)
    return target


def snapshots(target_dir: Path | None = None) -> list[Path]:
    """Existing automatic snapshots, oldest first."""
    target_dir = target_dir or backup_dir()
    if not target_dir.exists():
        return []
    return sorted(p for p in target_dir.iterdir()
                  if p.name.startswith(PREFIX) and p.name.endswith(SUFFIX))


def rotate(keep: int, target_dir: Path | None = None) -> list[Path]:
    """Delete all but the newest `keep` snapshots; returns the deleted files."""
    existing = snapshots(target_dir)
    removed = existing[:-keep] if keep > 0 else existing
    for path in removed:
        try:
            path.unlink()
        except OSError as exc:
            log.warning("Could not delete old backup %s: %s", path, exc)
    return removed


def _due(hours: int) -> bool:
    existing = snapshots()
    if not existing:
        return True
    age = datetime.now(timezone.utc).timestamp() - existing[-1].stat().st_mtime
    return age >= hours * 3600


async def _loop(hours: int) -> None:
    while True:
        try:
            if _due(hours):
                path = await asyncio.to_thread(create_snapshot)
                rotate(keep_count())
                log.info("Database backup written: %s", path.name)
        except Exception as exc:  # noqa: BLE001 - a failed backup must not stop the app
            log.warning("Database backup failed: %s", exc)
        # Check every 10 minutes; restarts don't reset the schedule because
        # the age of the newest snapshot decides.
        await asyncio.sleep(600)


def start() -> None:
    global _task
    hours = interval_hours()
    if hours <= 0 or _task is not None:
        return
    log.info("Automatic database backups every %dh (keeping %d)", hours, keep_count())
    _task = asyncio.create_task(_loop(hours))


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
