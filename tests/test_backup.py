"""Tests for database snapshots (app/backup.py) and the admin download.

Aufruf:  python3 tests/test_backup.py
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pokemon_encounter"))

TMP = tempfile.mkdtemp(prefix="pokemon-backup-test-")
os.environ["POKEMON_DATA_DIR"] = TMP
os.environ["SUPERVISOR_TOKEN"] = "dummy"
os.environ.pop("POKEMON_AUTH_MODE", None)
os.environ.pop("POKEMON_BACKUP_INTERVAL_HOURS", None)

from fastapi.testclient import TestClient  # noqa: E402

from app import admin, backup, db, seed  # noqa: E402
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
db.upsert_user("alice", "alice", "Alice")

# -------------------------------------------------------------- Settings
check("Backups are off by default (add-on)", backup.interval_hours() == 0)
os.environ["POKEMON_BACKUP_INTERVAL_HOURS"] = "24"
os.environ["POKEMON_BACKUP_KEEP"] = "3"
check("Interval and keep from env", backup.interval_hours() == 24 and backup.keep_count() == 3)
os.environ["POKEMON_BACKUP_KEEP"] = "nonsense"
check("Invalid keep falls back to 7", backup.keep_count() == 7)
os.environ["POKEMON_BACKUP_KEEP"] = "0"
check("Keep is at least 1", backup.keep_count() == 1)

# -------------------------------------------------------------- Snapshot
path = backup.create_snapshot()
check("Snapshot lands in <data>/backups", path.parent == Path(TMP) / "backups" and path.exists())
with sqlite3.connect(path) as conn:
    users = conn.execute("SELECT id FROM app_user").fetchall()
    species = conn.execute("SELECT COUNT(*) FROM species").fetchone()[0]
check("Snapshot contains the live data", ("alice",) in users and species > 0, f"{users}, {species}")
check("No temp file left behind", not list(path.parent.glob("*.tmp")))

# Rotation: create a few with distinct names, keep the newest 2.
for i in range(4):
    p = backup.backup_dir() / f"{backup.PREFIX}2026010{i}-000000{backup.SUFFIX}"
    p.write_bytes(b"x")
(backup.backup_dir() / "unrelated.txt").write_text("keep me")
removed = backup.rotate(2)
remaining = [p.name for p in backup.snapshots()]
check("Rotation keeps the newest snapshots", len(remaining) == 2 and remaining[-1] == path.name, remaining)
check("Rotation deletes the oldest", len(removed) == 3, [p.name for p in removed])
check("Rotation leaves other files alone", (backup.backup_dir() / "unrelated.txt").exists())

check("Not due right after a snapshot", backup._due(24) is False)
old = time.time() - 25 * 3600
os.utime(backup.snapshots()[-1], (old, old))
check("Due once the newest snapshot is older than the interval", backup._due(24) is True)

# ------------------------------------------------------------- Download
async def fake_fetch() -> list[dict]:
    return [
        {"id": "alice", "name": "Alice", "is_active": True, "is_owner": False,
         "group_ids": ["system-admin"]},
        {"id": "bob", "name": "Bob", "is_active": True, "is_owner": False,
         "group_ids": ["system-users"]},
    ]


admin._fetch_ha_users = fake_fetch
client = TestClient(app)
r = client.get("/api/admin/backup", headers={"X-Remote-User-Id": "bob"})
check("Download is admin-only", r.status_code == 403, r.status_code)
r = client.get("/api/admin/backup", headers={"X-Remote-User-Id": "alice"})
check("Admin gets a database file", r.status_code == 200 and r.content.startswith(b"SQLite format 3"),
      r.status_code)
check("Download has a timestamped file name",
      "pokehoas-" in r.headers.get("content-disposition", ""), r.headers.get("content-disposition"))
check("Download copy is not counted as a rotating snapshot",
      all(p.parent == backup.backup_dir() for p in backup.snapshots()))

print()
if failures:
    print(f"{len(failures)} Test(s) fehlgeschlagen:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("Alle Tests bestanden.")
