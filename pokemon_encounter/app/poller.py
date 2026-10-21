"""Poller: fetches state changes of trigger entities from HA every N minutes
and creates encounters from them.

Design decisions (from project planning):
* Pull instead of push. No webhook, no automation per sensor - the user only
  selects entities in the web app. Survives HA restarts because history holds
  events anyway.
* The marker "up to here I've processed" is intentionally in-memory only. After
  an add-on restart `poll_lookback_minutes` is looked back - at worst a few
  minutes are missed, which is acceptable.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from . import auto_rules, buddy, db, spawn, trigger_sync
from .config import settings
from .ha_client import HomeAssistantClient, HomeAssistantError, minutes_ago, utcnow

log = logging.getLogger(__name__)


class EncounterPoller:
    def __init__(self, client: HomeAssistantClient) -> None:
        self.client = client
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        # In-memory marker: history has been processed up to here.
        self._cursor: datetime | None = None
        self.last_run_at: datetime | None = None
        self.last_error: str | None = None
        self.last_created: int = 0

    # ------------------------------------------------------------ Lifecycle

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._loop(), name="encounter-poller")
            log.info("Poller started (interval: %s min)", settings.poll_interval_minutes)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        interval = max(1, settings.poll_interval_minutes) * 60
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - loop must never die
                self.last_error = str(exc)
                log.exception("Poll-Durchlauf fehlgeschlagen: %s", exc)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    # ----------------------------------------------------------- One poll run

    async def run_once(self) -> int:
        """Poll once. Return the number of newly created encounters."""
        now = utcnow()

        # Let stale encounters flee before adding new ones.
        fled = spawn.expire_stale_encounters(now)
        if fled:
            log.info("%s encounters fled after retention expired", fled)

        # Sync automatic triggers from add-on config (at startup and each run).
        # An error here must not prevent polling existing triggers.
        try:
            await trigger_sync.sync(self.client)
        except Exception as exc:  # noqa: BLE001
            log.warning("Trigger sync with add-on config failed: %s", exc)

        # Buddy: Schritte gutschreiben und Sensor nach HA pushen - unabhaengig
        # davon, ob es Trigger gibt; ein Fehler hier stoppt den Poll nicht.
        try:
            await buddy.sync(self.client)
        except Exception as exc:  # noqa: BLE001
            log.warning("Buddy sync failed: %s", exc)

        # Auto-rule notifications queued since the last run (e.g. by the
        # queue's "Run rules" button) - spawns of this run follow below.
        await self._push_rule_notifications()

        triggers = db.query(
            "SELECT * FROM trigger_entity WHERE enabled = 1"
        )
        if not triggers:
            self.last_run_at = now
            self.last_created = 0
            return 0

        since = self._cursor or minutes_ago(
            int(db.get_setting("poll_lookback_minutes", 15))
        )
        entity_ids = [trigger["entity_id"] for trigger in triggers]

        try:
            events = await self.client.fetch_state_changes(entity_ids, since=since, until=now)
        except HomeAssistantError as exc:
            # Don't advance cursor - try again on next attempt.
            self.last_error = str(exc)
            log.warning("History fetch failed, retrying: %s", exc)
            return 0

        created = spawn.process_events(events, triggers, now=now)
        await self._push_rule_notifications()

        self._cursor = now
        self.last_run_at = now
        self.last_error = None
        self.last_created = created
        if created:
            log.info("%s new encounters created from %s events", created, len(events))
        return created

    async def _push_rule_notifications(self) -> None:
        """Send the "notify me" messages of the auto rules as HA persistent
        notifications. A failure drops them (best effort) - never the poll."""
        for note in auto_rules.drain_notifications():
            try:
                await self.client.call_service("persistent_notification", "create", note)
            except Exception as exc:  # noqa: BLE001
                log.warning("Auto-rule notification failed: %s", exc)

    # -------------------------------------------------------------- Status

    def status(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "interval_minutes": settings.poll_interval_minutes,
            "cursor": self._cursor.isoformat() if self._cursor else None,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_created": self.last_created,
            "last_error": self.last_error,
        }
