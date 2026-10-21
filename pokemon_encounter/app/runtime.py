"""Gemeinsame Laufzeit-Objekte (HA-Client, Poller), damit die Router sie teilen."""

from __future__ import annotations

from .ha_client import HomeAssistantClient
from .poller import EncounterPoller

ha_client = HomeAssistantClient()
poller = EncounterPoller(ha_client)
