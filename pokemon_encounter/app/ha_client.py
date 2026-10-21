"""Client fuer die Home-Assistant-Core-API.

Als Supervisor-Add-on laeuft der Zugriff ueber http://supervisor/core/api mit
dem SUPERVISOR_TOKEN - der Nutzer muss also keinen Long-Lived Token anlegen.

Zum "Logbook-Polling": Die Logbook-API (/api/logbook/<ts>) filtert nur nach
einer einzelnen Entity. Fuer mehrere Trigger-Entities auf einmal ist
/api/history/period der passendere Endpunkt - inhaltlich dieselben
Zustandswechsel, aber mit `filter_entity_id` als Komma-Liste und deutlich
kleinerer Antwort dank `minimal_response`. Deshalb ist History der Default;
`fetch_logbook()` bleibt fuer Debugging/Einzel-Entities erhalten.

Bei hunderten Trigger-Entities (automatische Kategorien aus der
Add-on-Konfiguration) wird die Komma-Liste in der URL zu lang fuer HA
(aiohttp begrenzt die Request-Zeile auf ~8 KB). `fetch_state_changes()`
teilt die Liste deshalb in mehrere Abrufe auf.

Die Entity-Registry (entity_category, hidden_by, disabled_by) gibt es nur
ueber die WebSocket-API - siehe `list_entity_registry()`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)

# Obergrenze fuer die URL-kodierte `filter_entity_id`-Liste pro History-Abruf.
# HA/aiohttp lehnt Request-Zeilen ueber 8190 Bytes ab; Kommas werden als %2C
# kodiert. 3500 Zeichen lassen reichlich Luft fuer Pfad, Zeitstempel und
# die uebrigen Parameter.
HISTORY_FILTER_MAX_CHARS = 3500


class HomeAssistantError(RuntimeError):
    pass


class HomeAssistantClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or settings.ha_url).rstrip("/")
        self.token = token or settings.ha_token
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"{self.base_url}/api",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        client = await self._get_client()
        try:
            response = await client.get(path, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HomeAssistantError(f"HA-Abruf {path} fehlgeschlagen: {exc}") from exc
        return response.json()

    async def _send(self, method: str, path: str, payload: Any = None) -> Any:
        client = await self._get_client()
        try:
            response = await client.request(method, path, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HomeAssistantError(f"HA-{method} {path} fehlgeschlagen: {exc}") from exc
        return response.json() if response.content else None

    # ------------------------------------------------------------- Entities

    async def set_state(self, entity_id: str, state: str, attributes: dict[str, Any]) -> Any:
        """Zustand einer Entity setzen (POST /api/states/<entity_id>).

        Legt die Entity an, falls es sie noch nicht gibt - ohne Registry-Eintrag
        (keine unique_id), und nach einem HA-Neustart ist sie weg, bis der
        naechste Push kommt. Fuer den Buddy-Sensor (buddy.py) reicht das.
        """
        return await self._send(
            "POST", f"/states/{entity_id}", {"state": state, "attributes": attributes}
        )

    async def delete_state(self, entity_id: str) -> None:
        """Per set_state angelegte Entity wieder entfernen (404 = schon weg)."""
        try:
            await self._send("DELETE", f"/states/{entity_id}")
        except HomeAssistantError as exc:
            if "404" not in str(exc):
                raise

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        """Call a HA service (POST /api/services/<domain>/<service>)."""
        return await self._send("POST", f"/services/{domain}/{service}", data)

    async def list_states(self) -> list[dict[str, Any]]:
        """Alle Entities mit aktuellem Zustand - Grundlage fuer die Auswahl in der App."""
        return await self._get("/states")

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        try:
            return await self._get(f"/states/{entity_id}")
        except HomeAssistantError:
            return None

    async def get_states(self, entity_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not entity_ids:
            return {}
        wanted = set(entity_ids)
        return {
            state["entity_id"]: state
            for state in await self.list_states()
            if state["entity_id"] in wanted
        }

    async def list_entity_registry(self, timeout: float = 30) -> list[dict[str, Any]]:
        """Entity-Registry ueber die WebSocket-API (`config/entity_registry/list`).

        Liefert u.a. `entity_category`, `hidden_by`, `disabled_by` und
        `platform` - damit filtert der Trigger-Abgleich Konfigurations-/
        Diagnose-Schalter und ausgeblendete Entities heraus. Die REST-API
        kennt diese Felder nicht.
        """
        try:
            return await asyncio.wait_for(self._ws_command("config/entity_registry/list"), timeout)
        except HomeAssistantError:
            raise
        except Exception as exc:  # noqa: BLE001 - Netzwerk, Timeout, Protokoll
            raise HomeAssistantError(f"Entity-Registry-Abruf fehlgeschlagen: {exc!r}") from exc

    def _ws_url(self) -> str:
        if self.base_url.startswith("https://"):
            return "wss://" + self.base_url[len("https://"):] + "/websocket"
        if self.base_url.startswith("http://"):
            return "ws://" + self.base_url[len("http://"):] + "/websocket"
        return self.base_url + "/websocket"

    async def _ws_command(self, command: str) -> Any:
        # `websockets` kommt mit uvicorn[standard] - keine Zusatz-Abhaengigkeit.
        try:
            from websockets.asyncio.client import connect
        except ImportError:  # aeltere websockets-Versionen
            from websockets import connect  # type: ignore[no-redef]

        # max_size=None: die Registry-Liste ist in groesseren Haushalten
        # schnell mehrere MB gross (Default-Limit waere 1 MiB).
        async with connect(self._ws_url(), max_size=None) as ws:
            json.loads(await ws.recv())  # auth_required
            await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            auth = json.loads(await ws.recv())
            if auth.get("type") != "auth_ok":
                raise HomeAssistantError(f"WebSocket-Anmeldung fehlgeschlagen: {auth.get('type')}")
            await ws.send(json.dumps({"id": 1, "type": command}))
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == 1 and message.get("type") == "result":
                    break
        if not message.get("success"):
            raise HomeAssistantError(f"WebSocket-Befehl {command} fehlgeschlagen: {message.get('error')}")
        return message.get("result")

    # -------------------------------------------------------------- History

    async def fetch_state_changes(
        self,
        entity_ids: list[str],
        since: datetime,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Zustandswechsel der angegebenen Entities seit `since`.

        Rueckgabe: flache, nach Zeit sortierte Liste von
        {entity_id, state, last_changed (datetime, tz-aware)}.
        """
        if not entity_ids:
            return []

        base_params: dict[str, Any] = {
            "minimal_response": "",
            "significant_changes_only": "0",
        }
        if until is not None:
            base_params["end_time"] = until.astimezone(timezone.utc).isoformat()
        timestamp = since.astimezone(timezone.utc).isoformat()

        # Mehrere Abrufe statt einer ueberlangen URL. Schlaegt einer fehl,
        # scheitert der ganze Durchlauf (HomeAssistantError) - der Poller
        # rueckt seinen Cursor dann nicht vor und versucht es spaeter erneut.
        events: list[dict[str, Any]] = []
        for chunk in chunk_entity_ids(entity_ids):
            params = {**base_params, "filter_entity_id": ",".join(chunk)}
            raw = await self._get(f"/history/period/{timestamp}", params=params)
            events.extend(_history_to_events(raw, since))

        events.sort(key=lambda event: event["last_changed"])
        return events

    async def fetch_logbook(
        self, entity_id: str, since: datetime
    ) -> list[dict[str, Any]]:
        """Logbook einer einzelnen Entity - vor allem fuer Debugging."""
        timestamp = since.astimezone(timezone.utc).isoformat()
        return await self._get(
            f"/logbook/{timestamp}", params={"entity": entity_id}
        )

    # ----------------------------------------------------------- Geolocation

    async def current_zone(self, person_entity_id: str) -> str | None:
        """Zone, in der sich eine Person gerade befindet ('home', 'not_home', Zonenname)."""
        state = await self.get_state(person_entity_id)
        return state.get("state") if state else None

    async def list_zones(self) -> list[dict[str, Any]]:
        return [s for s in await self.list_states() if s["entity_id"].startswith("zone.")]


def chunk_entity_ids(
    entity_ids: list[str], max_chars: int = HISTORY_FILTER_MAX_CHARS
) -> list[list[str]]:
    """Teilt Entity-IDs so auf, dass jede kodierte Komma-Liste <= max_chars bleibt.

    Doppelte IDs werden entfernt, die Reihenfolge bleibt erhalten.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    length = 0
    for entity_id in dict.fromkeys(entity_ids):
        # +3 fuer das kodierte Komma (%2C) davor.
        cost = len(entity_id) + (3 if current else 0)
        if current and length + cost > max_chars:
            chunks.append(current)
            current, cost, length = [], len(entity_id), 0
        current.append(entity_id)
        length += cost
    if current:
        chunks.append(current)
    return chunks


def _history_to_events(raw: Any, since: datetime) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for series in raw or []:
        if not series:
            continue
        # Bei minimal_response traegt nur der erste Eintrag die entity_id.
        entity_id = series[0].get("entity_id")
        for item in series:
            entity_id = item.get("entity_id", entity_id)
            changed = _parse_ts(item.get("last_changed") or item.get("last_updated"))
            if changed is None or changed <= since:
                continue
            events.append(
                {
                    "entity_id": entity_id,
                    "state": item.get("state"),
                    "last_changed": changed,
                }
            )
    return events


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def minutes_ago(minutes: int) -> datetime:
    return utcnow() - timedelta(minutes=minutes)
