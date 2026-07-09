"""WebSocket client for real-time Alarm.com device updates.

Implements a 2-task architecture:

* **Reader task** — owns the WebSocket connection(s), handles proactive token
  rotation and reconnection with exponential back-off, and enqueues parsed
  messages.
* **Processor task** — dequeues messages and dispatches them to the
  :class:`~pyadc.events.EventBroker`.

The server (ADC backend) sends its own keepalive pings every 15 seconds via
.NET's ``WebSocket.DefaultKeepAliveInterval``; aiohttp's ``heartbeat`` parameter
handles the client side of ping/pong automatically.  A separate manual keepalive
task is therefore unnecessary.

Connection URL format: ``<endpoint>?f=1&auth=<token>``

The ``<token>`` value returned by the ``api/websockets/token`` endpoint is
produced by ``WebsocketAuthUtils.IssueToken()``, which appends ``&ver=<N>``
to the JWT automatically — so the client does **not** add a separate
``ver=`` parameter.  JWT lifetime is **300 seconds** (configured server-side
via ``WebsocketAuthTokenTimeout``).

**Token rotation (make-before-break).**  The JWT is validated only at the
handshake, but the backend's purge loop force-closes any socket whose
``ValidTo`` has passed (``WebSocketDispatcher.PurgeDisconnectedClients``,
close code 1008).  A close-then-reconnect cycle therefore used to occur every
~5 minutes, and any event dispatched during the reconnect window was lost —
e.g. a garage door completing its ``opening → open`` transition would leave
HA stuck on "opening" forever (GitHub issue alarmdotcom-ha#2).  To eliminate
the gap, the reader opens a **replacement socket with a fresh JWT before the
old one expires** (at ``WS_TOKEN_ROTATE_AFTER_S``).  The backend registers
multiple sockets per unit and fans every message out to all of them
(``WebSocketDispatcher.AddClient`` / ``SendQueuedMessagesAsync``), so both
sockets receive all traffic during a brief overlap and no event can fall
between them.  Identical raw frames received on both sockets during the
overlap are dropped by a short-TTL dedupe cache.

**Coverage gaps.**  If the connection ever drops *before* a replacement is
connected (network blip, backend restart, failed rotation), events may have
been missed.  After the subsequent successful reconnect the client publishes
a :class:`ConnectionEvent` with :attr:`WebSocketState.RECONNECTED` (in
addition to the regular ``CONNECTED`` state change) so consumers — e.g.
:class:`~pyadc.AlarmBridge` — can run a one-shot REST resync.  Rotation
handovers are seamless and publish nothing.

.. note:: **Security constraint** — the ADC backend reads the JWT
    exclusively from ``Request.QueryString["auth"]``
    (``AlarmClientWebSocketService.cs:173``).  There is no
    ``Authorization`` header alternative, so the token will appear in
    server access logs and intermediate proxy logs.  Mitigations: the
    JWT is short-lived (300 s), is both signed and encrypted, and is
    scoped to a specific ``customerId``.  Header-based auth would
    require a backend change on the Alarm.com side.

The ``?f=1`` flag instructs the ADC server to send an immediate close-frame
(code 1008) when the JWT is invalid or expired, rather than silently
hanging.  With rotation in place a 1008 close should only be seen if a
rotation failed repeatedly; the reader then reconnects with a fresh JWT.
A full re-login is only triggered if the JWT fetch itself fails (i.e. the
HTTP session has also expired).
"""

from __future__ import annotations

__all__ = [
    "WebSocketClient",
    "WebSocketState",
    "ConnectionEvent",
]

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, NoReturn

import aiohttp

from pyadc.const import (
    MAX_CONNECTION_ATTEMPTS,
    MAX_RECONNECT_WAIT_S,
    WS_CONNECT_TIMEOUT_S,
    WS_DEDUP_TTL_S,
    WS_KEEP_ALIVE_INTERVAL_S,
    WS_RECEIVE_TIMEOUT_S,
    WS_ROTATION_OVERLAP_S,
    WS_TOKEN_ROTATE_AFTER_S,
    WS_TOKEN_ROTATE_RETRY_S,
)
from pyadc.exceptions import AuthenticationFailed, NotAuthorized
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

    ``RECONNECTED`` is not a resting state: it is published as an *extra*
    :class:`ConnectionEvent` immediately after a ``CONNECTED`` transition
    that followed a coverage gap (the socket was down for some interval, so
    events may have been missed).  Consumers should treat it as a signal to
    resync state via REST.  Seamless token-rotation handovers never publish
    it.
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
    """2-task async WebSocket client for Alarm.com real-time events.

    **Task architecture:**

    1. ``ws_reader`` (:meth:`_reader_task`) — establishes the WebSocket
       connection, spawns a frame-reading subtask per socket
       (:meth:`_read_frames`), and manages the connection lifecycle:

       * **Token rotation** — ``WS_TOKEN_ROTATE_AFTER_S`` after each JWT
         fetch, a replacement socket is opened with a fresh token while the
         old socket is still live.  The two overlap briefly (the backend
         fans messages out to both), then the old socket is closed.  No gap,
         no events published, state stays ``CONNECTED``.
       * **Reconnection** — if a socket closes before a replacement is up,
         the reader reconnects with exponential back-off and, on success,
         publishes ``RECONNECTED`` so consumers can resync missed state.

    2. ``ws_processor`` (:meth:`_processor_task`) — dequeues messages and
       publishes them through the :class:`~pyadc.events.EventBroker` so that
       device controllers and HA entities receive state updates.

    Keepalive is handled automatically: the ADC server sends pings every 15 s
    via .NET ``WebSocket.DefaultKeepAliveInterval``, and aiohttp's ``heartbeat``
    parameter manages the client-side ping/pong response.

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
        self._old_ws: aiohttp.ClientWebSocketResponse | None = None
        self._last_message_at: float | None = None
        self._token_fetched_at: float = 0.0
        self._had_gap = False
        # raw frame text → monotonic time first seen; drops handover duplicates
        self._recent_frames: dict[str, float] = {}

    @property
    def connected(self) -> bool:
        """Return ``True`` when the WebSocket is in the CONNECTED state."""
        return self._state == WebSocketState.CONNECTED

    @property
    def seconds_since_last_message(self) -> float | None:
        """Seconds since the last inbound frame or successful (re)connect.

        Lets callers distinguish an active connection (recent traffic) from one
        that is silent — used as a cheap, network-free health signal so a REST
        reconcile only runs when the socket looks stalled.  A successful token
        rotation also refreshes this timestamp, since completing a fresh
        token fetch + handshake proves the path is alive end-to-end.
        """
        if self._last_message_at is None:
            return None
        return time.monotonic() - self._last_message_at

    @property
    def state(self) -> WebSocketState:
        """Return the current :class:`WebSocketState`."""
        return self._state

    async def start(self) -> None:
        """Spawn the two background tasks (idempotent).

        If the tasks are already running this method returns immediately.
        Dead/cancelled tasks are filtered out first so a restart after an
        unexpected task exit correctly spawns fresh tasks rather than
        returning early with a list of zombie handles.
        """
        self._tasks = [t for t in self._tasks if not t.done()]
        if self._tasks:
            return

        def _on_task_done(task: asyncio.Task) -> None:
            if not task.cancelled() and task.exception() is not None:
                log.error("WebSocket task %s died: %s", task.get_name(), task.exception())

        self._tasks = [
            asyncio.create_task(self._reader_task(), name="ws_reader"),
            asyncio.create_task(self._processor_task(), name="ws_processor"),
        ]
        for t in self._tasks:
            t.add_done_callback(_on_task_done)

    async def stop(self) -> None:
        """Cancel all tasks and close the WebSocket connection(s)."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for ws in (self._ws, self._old_ws):
            if ws and not ws.closed:
                await ws.close()
        self._old_ws = None
        self._set_state(WebSocketState.DISCONNECTED)

    async def _connect(self) -> aiohttp.ClientWebSocketResponse:
        """Acquire a fresh WS JWT and open the WebSocket connection.

        Calls ``websockets/token`` to get a short-lived JWT (300 s lifetime).
        If that request fails due to an expired HTTP session
        (:exc:`~pyadc.exceptions.AuthenticationFailed` /
        :exc:`~pyadc.exceptions.NotAuthorized`), falls back to a full
        re-login before retrying the token fetch.

        The ``?f=1`` flag tells the server to send close code 1008
        immediately on JWT expiry rather than silently hanging.
        ``heartbeat`` delegates ping/pong to aiohttp; ``receive_timeout``
        is set to match the JWT lifetime so a fully silent connection is
        detected within one token window.

        The whole attempt (token fetch, optional re-login, handshake) is
        capped at ``WS_CONNECT_TIMEOUT_S``: a stalled REST call here would
        otherwise wedge the reader task indefinitely — during a rotation the
        old socket would silently die underneath it, and during a reconnect
        recovery would be delayed by however long the stall lasts.
        """
        async with asyncio.timeout(WS_CONNECT_TIMEOUT_S):
            return await self._connect_inner()

    async def _connect_inner(self) -> aiohttp.ClientWebSocketResponse:
        log.debug("WS connect: fetching token...")
        try:
            endpoint, token = await self._bridge.auth.get_websocket_token()
        except (AuthenticationFailed, NotAuthorized):
            log.info("WS token fetch failed — HTTP session expired, running full re-auth...")
            await self._bridge.auth.login()
            await self._bridge.auth.start_keep_alive()
            endpoint, token = await self._bridge.auth.get_websocket_token()

        # The rotation deadline is measured from token *issuance*, not from
        # when the handshake completes, so a slow connect can't eat into the
        # safety margin before the server-side ValidTo purge.
        self._token_fetched_at = time.monotonic()

        # SECURITY NOTE: The ADC backend reads the JWT exclusively from the URL
        # query string (AlarmClientWebSocketService.cs, line 173:
        # `Request.QueryString.Get("auth")`).  There is no header-based
        # alternative.  The token therefore appears in server access logs and
        # any intermediate proxies.  Mitigations: the JWT is short-lived
        # (300 s), encrypted+signed (not just signed), and tied to a specific
        # customerId — replay value is low but the exposure is unavoidable
        # until the ADC backend adds header-based auth.
        url = f"{endpoint}?f=1&auth={token}"
        try:
            ws = await self._bridge._session.ws_connect(
                url,
                heartbeat=WS_KEEP_ALIVE_INTERVAL_S,
                receive_timeout=WS_RECEIVE_TIMEOUT_S,
            )
            log.debug("WebSocket connected")
            return ws
        except Exception as err:
            raise ConnectionError(f"WebSocket connection failed: {err}") from err

    async def _reader_task(self) -> NoReturn:
        """Maintain WS coverage: rotate tokens seamlessly, reconnect on drops."""
        read_task: asyncio.Task | None = None
        old_read_task: asyncio.Task | None = None
        try:
            while True:
                # ---- (re)connect with exponential back-off -----------------
                try:
                    self._set_state(WebSocketState.CONNECTING)
                    self._ws = await self._connect()
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    log.warning("WS connection error: %s", err)
                    if not await self._backoff():
                        return
                    continue

                self._connection_attempts = 0
                self._last_message_at = time.monotonic()
                self._set_state(WebSocketState.CONNECTED)
                if self._had_gap:
                    # The previous socket died before a replacement was up —
                    # events may have been missed. Signal consumers to resync.
                    self._had_gap = False
                    self._bridge.event_broker.publish(
                        ConnectionEvent(current_state=WebSocketState.RECONNECTED)
                    )

                read_task = asyncio.create_task(
                    self._read_frames(self._ws), name="ws_read_frames"
                )
                rotate_deadline = self._token_fetched_at + WS_TOKEN_ROTATE_AFTER_S

                # ---- rotation loop: swap sockets before the JWT purge ------
                while True:
                    timeout = max(rotate_deadline - time.monotonic(), 0.0)
                    done, _ = await asyncio.wait({read_task}, timeout=timeout)
                    if read_task in done:
                        break  # socket closed → coverage gap begins

                    # Rotation due: open the replacement while the old socket
                    # is still registered server-side (the dispatcher fans
                    # messages out to every open socket for this unit).
                    log.debug("WS token rotation due — opening replacement socket")
                    try:
                        new_ws = await self._connect()
                    except asyncio.CancelledError:
                        raise
                    except Exception as err:
                        log.debug(
                            "WS rotation connect failed (%s) — retrying in %ss; "
                            "old socket stays up meanwhile",
                            err,
                            WS_TOKEN_ROTATE_RETRY_S,
                        )
                        rotate_deadline = time.monotonic() + WS_TOKEN_ROTATE_RETRY_S
                        continue

                    self._old_ws, old_read_task = self._ws, read_task
                    self._ws = new_ws
                    # A completed token fetch + handshake proves end-to-end
                    # liveness; refresh the staleness signal accordingly.
                    self._last_message_at = time.monotonic()
                    read_task = asyncio.create_task(
                        self._read_frames(new_ws), name="ws_read_frames"
                    )
                    rotate_deadline = self._token_fetched_at + WS_TOKEN_ROTATE_AFTER_S
                    log.debug(
                        "WS token rotation: replacement connected, closing old "
                        "socket after %ss overlap",
                        WS_ROTATION_OVERLAP_S,
                    )

                    # Brief overlap so a message in flight to the old socket
                    # is still read, then a graceful close.
                    await asyncio.wait({old_read_task}, timeout=WS_ROTATION_OVERLAP_S)
                    if not self._old_ws.closed:
                        await self._old_ws.close()
                    await asyncio.wait({old_read_task}, timeout=5)
                    if not old_read_task.done():
                        old_read_task.cancel()
                    old_read_task = None
                    self._old_ws = None

                # Unexpected close (JWT purge after failed rotations, network
                # drop, backend restart, …) — anything until reconnect is a gap.
                self._had_gap = True
                read_task = None
                if not await self._backoff():
                    return
        finally:
            for t in (read_task, old_read_task):
                if t is not None and not t.done():
                    t.cancel()

    async def _backoff(self) -> bool:
        """Sleep with exponential back-off; ``False`` once the DEAD cap is hit."""
        self._connection_attempts += 1
        if self._connection_attempts >= MAX_CONNECTION_ATTEMPTS:
            self._set_state(WebSocketState.DEAD)
            log.error("WebSocket DEAD after %d attempts", self._connection_attempts)
            return False

        wait_s = min(
            (2**self._connection_attempts) + random.uniform(0, min(2**self._connection_attempts, 30)),
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
        return True

    async def _read_frames(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Read frames from *ws* until it closes; enqueue parsed messages.

        Runs as one task per socket so that two sockets can be drained
        concurrently during a rotation handover.
        """
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._last_message_at = time.monotonic()
                    if self._is_duplicate_frame(msg.data):
                        continue
                    try:
                        data = json.loads(msg.data)
                        parsed = WebSocketMessageParser.parse(data)
                        await self._queue.put(parsed)
                    except Exception as err:
                        log.debug("Failed to parse WS message: %s | raw: %s", err, msg.data)

                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    close_code = msg.data
                    if close_code == WS_CLOSE_POLICY_VIOLATION:
                        log.info(
                            "WS JWT expired (1008) before rotation completed — "
                            "reconnecting with fresh token"
                        )
                    else:
                        log.debug("WS closed with code %s", close_code)
                    break

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log.error("WS error: %s", ws.exception())
                    break

        except asyncio.CancelledError:
            raise
        except Exception as err:
            log.warning("WS read error: %s", err)

    def _is_duplicate_frame(self, raw: str) -> bool:
        """True if an identical frame was already seen within ``WS_DEDUP_TTL_S``.

        During a rotation handover both sockets are registered server-side and
        the dispatcher sends every message to each of them; an identical raw
        frame inside the TTL window is that handover duplicate, not a new
        event (real ADC frames carry event dates/correlation data, so distinct
        events never serialize to identical text).
        """
        now = time.monotonic()
        self._recent_frames = {
            frame: seen_at
            for frame, seen_at in self._recent_frames.items()
            if now - seen_at < WS_DEDUP_TTL_S
        }
        if raw in self._recent_frames:
            return True
        self._recent_frames[raw] = now
        return False

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

    def _set_state(self, state: WebSocketState) -> None:
        """Update state and publish ConnectionEvent if it changed."""
        if self._state == state:
            return
        self._state = state
        log.debug("WS state → %s", state)
        self._bridge.event_broker.publish(ConnectionEvent(current_state=state))
