"""Persistent authenticated Music Assistant WebSocket event connection."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time

from collections.abc import Callable
from typing import Any

from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType

from .const import MUSIC_ASSISTANT_SCHEMA_MIN, MUSIC_ASSISTANT_SCHEMA_VALIDATED

_LOGGER = logging.getLogger(__name__)


def _websocket_url(base_url: str) -> str:
    """Return the Music Assistant WebSocket endpoint for a HTTP base URL."""
    clean = str(base_url or "").strip().rstrip("/")
    if clean.startswith("https://"):
        clean = f"wss://{clean[8:]}"
    elif clean.startswith("http://"):
        clean = f"ws://{clean[7:]}"
    if not clean.endswith("/ws"):
        clean = f"{clean}/ws"
    return clean


class MusicAssistantEventClient:
    """Keep one MA event stream alive and expose only redacted status."""

    def __init__(
        self,
        session: ClientSession,
        on_message: Callable[[dict[str, Any]], None],
    ) -> None:
        self._session = session
        self._on_message = on_message
        self._base_urls: list[str] = []
        self._base_url = ""
        self._active_url_index = 0
        self._token = ""
        self._task: asyncio.Task[None] | None = None
        self._ws: ClientWebSocketResponse | None = None
        self._generation = 0
        self._connected = False
        self._authenticated = False
        self._server_version = ""
        self._schema_version: int | None = None
        self._server_id = ""
        self._last_connected_at = ""
        self._last_event_at = ""
        self._last_error = ""
        self._event_count = 0
        self._pending_commands: dict[str, asyncio.Future[Any]] = {}
        self._partial_results: dict[str, list[Any]] = {}
        self._send_lock = asyncio.Lock()
        self._ready_event = asyncio.Event()

    def configure(self, base_url: str | list[str], token: str) -> None:
        """Apply connection settings and restart only when they changed."""
        raw_urls = base_url if isinstance(base_url, list) else [base_url]
        clean_urls = [
            clean
            for index, value in enumerate(raw_urls)
            if (clean := str(value or "").strip().rstrip("/"))
            and clean not in [str(item or "").strip().rstrip("/") for item in raw_urls[:index]]
        ]
        clean_url = clean_urls[0] if clean_urls else ""
        clean_token = str(token or "").strip()
        if clean_urls == self._base_urls and clean_token == self._token and self._task:
            return
        self._base_urls = clean_urls
        self._base_url = clean_url
        self._active_url_index = 0
        self._token = clean_token
        self._generation += 1
        previous_task = self._task
        if previous_task:
            previous_task.cancel()
        # Keep the cancelled task referenced until a replacement can await its socket cleanup.
        # HA options reload calls configure("", "") and configure(url, token) back-to-back.
        self._task = previous_task
        self._connected = False
        self._authenticated = False
        self._ready_event.clear()
        if clean_url and clean_token:
            generation = self._generation
            try:
                if previous_task:
                    self._task = asyncio.create_task(
                        self._restart_after(previous_task, generation),
                        name="homeii_flow_music_assistant_events_restart",
                    )
                else:
                    self._task = asyncio.create_task(
                        self._run(generation),
                        name="homeii_flow_music_assistant_events",
                    )
            except RuntimeError as err:
                self._last_error = str(err)[:300]
                self._task = None
        self._publish_status()

    async def _restart_after(
        self,
        previous_task: asyncio.Task[None],
        generation: int,
    ) -> None:
        """Wait for the previous socket to close before starting its replacement."""
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await previous_task
        if generation == self._generation and self._base_urls and self._token:
            await self._run(generation)

    async def async_stop(self) -> None:
        """Stop the event stream."""
        self._generation += 1
        task = self._task
        self._task = None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._close_ws()
        self._connected = False
        self._authenticated = False
        self._ready_event.clear()
        self._fail_pending_commands("Music Assistant connection stopped")
        self._publish_status()

    async def async_command(
        self,
        command: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float = 30,
    ) -> Any:
        """Execute a command on the authenticated persistent MA connection."""
        deadline = asyncio.get_running_loop().time() + timeout
        if not self._authenticated:
            try:
                await asyncio.wait_for(self._ready_event.wait(), timeout=min(timeout, 12))
            except TimeoutError as err:
                raise RuntimeError("Music Assistant WebSocket authentication timed out") from err
        ws = self._ws
        if not self._authenticated or ws is None or ws.closed:
            raise RuntimeError("Music Assistant WebSocket is not authenticated")
        message_id = f"homeii_cmd_{int(time.time() * 1000)}_{secrets.token_urlsafe(6)}"
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending_commands[message_id] = future
        self._partial_results[message_id] = []
        try:
            # One deadline includes lock contention, sending and the MA response.
            async with asyncio.timeout_at(deadline):
                async with self._send_lock:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError
                    if self._ws is not ws or ws.closed or not self._authenticated:
                        raise RuntimeError("Music Assistant WebSocket connection changed")
                    await ws.send_json(
                        {
                            "message_id": message_id,
                            "command": str(command or "").strip(),
                            "args": args or {},
                        }
                    )
                return await asyncio.shield(future)
        except TimeoutError as err:
            raise RuntimeError(f"Music Assistant command timed out: {command}") from err
        finally:
            self._pending_commands.pop(message_id, None)
            self._partial_results.pop(message_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Consume a disconnect error even if sending failed first.

    def snapshot(self) -> dict[str, Any]:
        """Return status without exposing URL credentials."""
        return {
            "configured": bool(self._base_url and self._token),
            "url_configured": bool(self._base_url),
            "url_count": len(self._base_urls),
            "active_url_index": self._active_url_index,
            "token_configured": bool(self._token),
            "connected": self._connected,
            "authenticated": self._authenticated,
            "server_version": self._server_version,
            "schema_version": self._schema_version,
            "schema_supported": bool(
                self._schema_version is not None
                and self._schema_version >= MUSIC_ASSISTANT_SCHEMA_MIN
            ),
            "schema_validated": self._schema_version == MUSIC_ASSISTANT_SCHEMA_VALIDATED,
            "validated_schema_version": MUSIC_ASSISTANT_SCHEMA_VALIDATED,
            "server_id": self._server_id,
            "last_connected_at": self._last_connected_at,
            "last_event_at": self._last_event_at,
            "event_count": self._event_count,
            "last_error": self._last_error,
        }

    async def _run(self, generation: int) -> None:
        delay = 1
        while generation == self._generation and self._base_urls and self._token:
            self._active_url_index %= len(self._base_urls)
            self._base_url = self._base_urls[self._active_url_index]
            try:
                await self._connect_and_listen(generation)
                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - connection errors must never escape setup
                self._last_error = str(err)[:300]
                _LOGGER.warning("Music Assistant event connection failed: %s", self._last_error)
                self._active_url_index = (self._active_url_index + 1) % len(self._base_urls)
            finally:
                await self._close_ws()
                self._connected = False
                self._authenticated = False
                self._publish_status()
            if generation != self._generation:
                break
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)

    async def _connect_and_listen(self, generation: int) -> None:
        self._ws = await self._session.ws_connect(
            _websocket_url(self._base_url),
            heartbeat=55,
            compress=15,
            max_msg_size=0,
        )
        greeting = await self._receive_json()
        self._server_version = str(greeting.get("server_version") or "")
        schema = greeting.get("schema_version", greeting.get("api_schema_version"))
        try:
            self._schema_version = int(schema) if schema is not None else None
        except (TypeError, ValueError):
            self._schema_version = None
        if self._schema_version is None or self._schema_version < MUSIC_ASSISTANT_SCHEMA_MIN:
            raise RuntimeError(
                f"Music Assistant API schema {MUSIC_ASSISTANT_SCHEMA_MIN} or newer is required; "
                f"server reported {self._schema_version if self._schema_version is not None else 'unknown'}"
            )
        self._server_id = str(greeting.get("server_id") or "")
        self._connected = True
        self._last_error = ""
        self._publish_status()

        message_id = f"homeii_auth_{int(time.time() * 1000)}"
        await self._ws.send_json(
            {
                "message_id": message_id,
                "command": "auth",
                "args": {"token": self._token, "device_name": "HOMEii Flow Engine"},
            }
        )
        while generation == self._generation:
            message = await self._receive_json()
            if message.get("message_id") != message_id:
                if "event" in message:
                    self._publish_event(message)
                continue
            if message.get("error_code") or message.get("error"):
                raise RuntimeError(
                    str(message.get("details") or message.get("error") or message.get("error_code"))
                )
            self._authenticated = True
            self._ready_event.set()
            self._last_connected_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._publish_status()
            break

        while generation == self._generation:
            message = await self._receive_json()
            if self._dispatch_command_response(message):
                continue
            if "event" in message:
                self._publish_event(message)

    def _dispatch_command_response(self, message: dict[str, Any]) -> bool:
        """Resolve one pending command, including chunked MA list responses."""
        message_id = str(message.get("message_id") or "")
        future = self._pending_commands.get(message_id)
        if future is None:
            return False
        if message.get("error_code") or message.get("error"):
            if not future.done():
                details = str(
                    message.get("details")
                    or message.get("error")
                    or message.get("error_code")
                    or "Music Assistant command failed"
                )
                future.set_exception(RuntimeError(details))
            return True
        result = message.get("result")
        if message.get("partial"):
            partial = self._partial_results.setdefault(message_id, [])
            if isinstance(result, list):
                partial.extend(result)
            elif result is not None:
                partial.append(result)
            return True
        partial = self._partial_results.get(message_id) or []
        if partial:
            if isinstance(result, list):
                result = [*partial, *result]
            elif result is None:
                result = partial
            else:
                result = [*partial, result]
        if not future.done():
            future.set_result(result)
        return True

    async def _receive_json(self) -> dict[str, Any]:
        if not self._ws:
            raise RuntimeError("Music Assistant WebSocket is not connected")
        message = await self._ws.receive()
        if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
            raise RuntimeError("Music Assistant WebSocket closed")
        if message.type == WSMsgType.ERROR:
            raise RuntimeError("Music Assistant WebSocket failed")
        if message.type != WSMsgType.TEXT:
            raise ValueError("Music Assistant sent a non-text WebSocket message")
        data = message.json()
        if not isinstance(data, dict):
            raise ValueError("Music Assistant sent an invalid WebSocket message")
        return data

    async def _close_ws(self) -> None:
        ws = self._ws
        self._ws = None
        self._ready_event.clear()
        if ws and not ws.closed:
            with contextlib.suppress(Exception):
                await ws.close()
        self._fail_pending_commands("Music Assistant WebSocket disconnected")

    def _fail_pending_commands(self, reason: str) -> None:
        """Reject commands that cannot receive a response after disconnect."""
        for future in list(self._pending_commands.values()):
            if not future.done():
                future.set_exception(RuntimeError(reason))
        self._pending_commands.clear()
        self._partial_results.clear()

    def _publish_status(self) -> None:
        try:
            self._on_message({"kind": "connection", "connection": self.snapshot()})
        except Exception:  # noqa: BLE001 - status reporting must not break the connection task
            _LOGGER.exception("Music Assistant connection status callback failed")

    def _publish_event(self, message: dict[str, Any]) -> None:
        self._event_count += 1
        self._last_event_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            self._on_message(
                {
                    "kind": "event",
                    "event": str(message.get("event") or ""),
                    "object_id": str(message.get("object_id") or ""),
                    "sequence": self._event_count,
                    "occurred_at": self._last_event_at,
                }
            )
        except Exception:  # noqa: BLE001 - one HA bus failure must not drop the MA socket
            _LOGGER.exception("Music Assistant event callback failed")
