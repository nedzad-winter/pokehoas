"""Runtime configuration from environment variables (set by run.sh)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    www_dir: Path
    ha_url: str
    ha_token: str
    # Add-on option `admin_users`: HA usernames or IDs that independently of
    # their HA group have admin rights in the app (see app/admin.py).
    admin_users: tuple[str, ...]
    poll_interval_minutes: int
    log_level: str
    http_port: int

    @property
    def db_path(self) -> Path:
        # Lives in add-on data directory -> captured by Supervisor backup.
        return self.data_dir / "pokemon.sqlite3"


def _split_list(raw: str) -> tuple[str, ...]:
    # bashio delivers list options line by line; comma works for local tests too.
    items = (part.strip() for part in raw.replace(",", "\n").splitlines())
    return tuple(item for item in items if item and item != "null")


def load_settings() -> Settings:
    data_dir = Path(os.getenv("POKEMON_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    www_dir = Path(os.getenv("POKEMON_WWW_DIR", Path(__file__).parent.parent / "www")).resolve()

    return Settings(
        data_dir=data_dir,
        www_dir=www_dir,
        ha_url=os.getenv("POKEMON_HA_URL", "http://supervisor/core").rstrip("/"),
        ha_token=os.getenv("SUPERVISOR_TOKEN", os.getenv("POKEMON_HA_TOKEN", "")),
        admin_users=_split_list(os.getenv("POKEMON_ADMIN_USERS", "")),
        poll_interval_minutes=int(os.getenv("POKEMON_POLL_INTERVAL_MINUTES", "5")),
        log_level=os.getenv("POKEMON_LOG_LEVEL", "info"),
        http_port=int(os.getenv("POKEMON_HTTP_PORT", "8099")),
    )


settings = load_settings()
