"""WebSocket client for real-time Alarm.com device updates.

Implements a 3-task architecture:

* **Reader task** — owns the WebSocket connection, handles reconnection with
  exponential back-off, and enqueues parsed messages.
* **Processor task** — dequeues messages and dispatches them to the
  :class:`~pyadc.events.EventBroker`.
* **Keep-alive task** — sends periodic WebSocket pings to prevent idle
  timeouts.

Connection URL format: ``<endpoint>?f=1&auth=<token>``

The ``<token>`` value returned by the ``api/websockets/token`` endpoint is
produced by ``WebsocketAuthUtils.IssueToken()``, which appends ``&ver=<N>``
to the JWT automatically — so the client does **not** add a separate
``ver=`` parameter.

The ``?f=1`` flag instructs the ADC server to send an immediate close-frame
(code 1008) when the JWT is invalid or expired, rather than silently
hanging.  On close code 1008 the reader task automatically re-authenticates
via :meth:`~pyadc.auth.AuthController.login` before reconnecting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import urlencode

import aiohttp

from pyadc.const import (
    MAX_CONNECTION_ATTEMPTS,
    MAX_RECONNECT_WAIT_S,
    WS_KEEP_ALIVE_INTERVAL_S,
)
from pyadc.events import EventBrokerMessage, EventBrokerTopic
from pyadc.websocket.messages import (
    BaseWSMessage,
    RawResourceEventMessage,
    WebSocketMessageParser,
)

if TYPE_CHECKING:
    from pyadc import AlarmBridge

log = logging.getLogger(__name__)

WS_CLOSE_POLICY_VIOLATION = 1008  # JWT expired / policy violation


class WebSocketState(Enum):
    """Connection state machine for the WebSocket client.

    State transitions:
    ``DISCONNECTED`` → ``CONNECTING`` → ``CONNECTED`` → ``WAITING`` → …
    After ``MAX_CONNECTION_ATTEMPTS`` failures: → ``DEAD`` (terminal).
    A ``RECONNECTED`` state may be published after a successful
    reconnect following a ``WAITING`` state.
    """

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    DEAD = "dead"
    WAITING = "waiting"
    RECONNECTED = "reconnected"


@dataclass(kw_only=True)
class ConnectionEvent(EventBrokerMessage):
    """Published whenever the WebSocket connection state changes."""

    topic: EventBrokerTopic = EventBrokerTopic.CONNECTION_EVENT
    current_state: WebSocketState
    next_attempt_s: int | None = None


class WebSocketClient:
    """3-task async WebSocket client for Alarm.com real-time events.

    **Task architecture:**

    1. ``ws_reader`` (:meth:`_reader_task`) — establishes the WebSocket
       connection, reads raw text frames, parses them with
       :class:`~pyadc.websocket.messages.WebSocketMessageParser`, and puts
       the resulting :class:`~pyadc.websocket.messages.BaseWSMessage` objects
       onto the internal queue.  Handles reconnection with exponential
       back-off and re-auth on close code 1008.
    2. ``ws_processor`` (:meth:`_processor_task`) — dequeues messages and
       publishes them through the :class:`~pyadc.events.EventBroker` so that
       device controllers and HA entities receive state updates.
    3. ``ws_keepalive`` (:meth:`_keepalive_task`) — sends a WebSocket ping
       every :data:`~pyadc.const.WS_KEEP_ALIVE_INTERVAL_S` seconds.

    **DEAD state:**  After ``MAX_CONNECTION_ATTEMPTS`` consecutive failures
    the state transitions to :attr:`WebSocketState.DEAD` and a
    :class:`ConnectionEvent` is published.  Callers (e.g. the HA
    ``AlarmHub``) should treat DEAD as a signal to reload / re-authenticate.
    """

    def __init__(self, bridge: "AlarmBridge") -> None:
        self._bridge = bridge
        self._state = WebSocketState.DISCONNECTED
        self._queue: asyncio.Queue[BaseWSMessage] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._connection_attempts = 0
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._consecutive_ping_failures = 0

    @property
    def connected(self) -> bool:
        """Return ``True`` when the WebSocket is in the CONNECTED state."""
        return self._state == WebSocketState.CONNECTED

    @property
    def state(self) -> WebSocketState:
        """Return the current :class:`WebSocketState`."""
        return self._state

    async def start(self) -> None:
        """Spawn the three background tasks (idempotent).

        If the tasks are already running this method returns immediately.
        """
        if self._tasks:
            return

        def _on_task_done(task: asyncio.Task) -> None:
            if not task.cancelled() and task.exception() is not None:
                log.error("WebSocket task %s died: %s", task.get_name(), task.exception())

        self._tasks = [
            asyncio.create_task(self._reader_task(), name="ws_reader"),
            asyncio.create_task(self._processor_task(), name="ws_processor"),
            asyncio.create_task(self._keepalive_task(), name="ws_keepalive"),
        ]
        for t in self._tasks:
            t.add_done_callback(_on_task_done)

    async def stop(self) -> None:
        """Cancel all tasks and close the WebSocket connection."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._set_state(WebSocketState.DISCONNECTED)

    async def _connect(self) -> aiohttp.ClientWebSocketResponse:
        """
        Acquire a WS token and connect.

        ``IssueToken()`` on the backend returns ``<jwt>&ver=<version>``, so the
        ``ver`` parameter is already embedded in the token string itself.  The
        ``?f=1`` flag instructs the server to send an immediate close-frame
        (code 1008) when the JWT is invalid or expired instead of silently hanging.
        """
        endpoint, token = await self._bridge.auth.get_websocket_token()

        # token already contains "&ver=X" appended by WebsocketAuthUtils.IssueToken()
        url = f"{endpoint}?{urlencode({'f': '1', 'auth': token})}"
        try:
            ws = await self._bridge._session.ws_connect(
                url,
                heartbeat=WS_KEEP_ALIVE_INTERVAL_S,
                receive_timeout=120,
            )
            log.debug("WebSocket connected")
            return ws
        except Exception as err:
            raise ConnectionError(f"WebSocket connection failed: {err}") from err

    async def _reader_task(self) -> NoReturn:
        """Maintain WS connection and enqueue parsed messages."""
        while True:
            try:
                self._set_state(WebSocketState.CONNECTING)
                self._ws = await self._connect()
                self._connection_attempts = 0
                self._set_state(WebSocketState.CONNECTED)

                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                            parsed = WebSocketMessageParser.parse(data)
                            await self._queue.put(parsed)
                        except Exception as err:
                            log.debug("Failed to parse WS message: %s | raw: %s", err, msg.data)

                    elif msg.type == aiohttp.WSMsgType.CLOSE:
                        close_code = msg.data
                        log.debug("WS closed with code %s", close_code)
                        if close_code == WS_CLOSE_POLICY_VIOLATION:
                            # JWT expired — re-auth before reconnecting
                            log.info("WS JWT expired (1008), re-authenticating...")
                            try:
                                await self._bridge.auth.login()
                            except Exception as auth_err:
                                log.error("Re-auth failed: %s", auth_err)
                        break

                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        log.error("WS error: %s", self._ws.exception())
                        break

            except asyncio.CancelledError:
                raise
            except Exception as err:
                log.warning("WS connection error: %s", err)

            # Backoff before reconnecting
            self._connection_attempts += 1
            if self._connection_attempts >= MAX_CONNECTION_ATTEMPTS:
                self._set_state(WebSocketState.DEAD)
                log.error("WebSocket DEAD after %d attempts", self._connection_attempts)
                return

            wait_s = min(
                (2**self._connection_attempts) + random.uniform(0, 1),
                MAX_RECONNECT_WAIT_S,
            )
            log.info(
                "WS reconnecting in %.1fs (attempt %d/%d)",
                wait_s,
                self._connection_attempts,
                MAX_CONNECTION_ATTEMPTS,
            )
            self._set_state(WebSocketState.WAITING)
            self._bridge.event_broker.publish(
                ConnectionEvent(
                    current_state=WebSocketState.WAITING,
                    next_attempt_s=int(wait_s),
                )
            )
            await asyncio.sleep(wait_s)

    async def _processor_task(self) -> NoReturn:
        """Dequeue parsed WS messages and dispatch to EventBroker."""
        while True:
            msg = await self._queue.get()
            try:
                self._bridge.event_broker.publish(RawResourceEventMessage(ws_message=msg))
                log.debug("WS dispatched: type=%s", type(msg).__name__)
            except Exception as err:
                log.error("Error dispatching WS message: %s", err)
            finally:
                self._queue.task_done()

    async def _keepalive_task(self) -> NoReturn:
        """Send periodic pings to keep the WebSocket alive."""
        while True:
            await asyncio.sleep(WS_KEEP_ALIVE_INTERVAL_S)
            if self._ws and not self._ws.closed:
                try:
                    await self._ws.ping()
                    self._consecutive_ping_failures = 0
                    log.debug("WS keepalive ping sent")
                except Exception as err:
                    self._consecutive_ping_failures += 1
                    log.debug("WS ping failed: %s", err)
                    if self._consecutive_ping_failures >= 3:
                        log.error(
                            "WS ping failed %d consecutive times; closing connection to trigger reconnect",
                            self._consecutive_ping_failures,
                        )
                        if self._ws and not self._ws.closed:
                            await self._ws.close()

    def _set_state(self, state: WebSocketState) -> None:
        """Update state and publish ConnectionEvent if it changed."""
        if self._state == state:
            return
        self._state = state
        log.debug("WS state → %s", state)
        self._bridge.event_broker.publish(ConnectionEvent(current_state=state))
