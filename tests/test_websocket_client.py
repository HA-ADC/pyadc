"""Tests for WebSocketClient token rotation, dedupe, and gap-resync signalling.

The ADC backend force-closes any socket whose JWT ValidTo (300 s) has passed,
so the client rotates sockets proactively (make-before-break).  These tests
verify:

* a rotation opens a second socket, closes the first, and publishes no
  connection events (state stays CONNECTED throughout);
* an unexpected socket drop leads to a reconnect that publishes RECONNECTED
  so consumers can resync missed events;
* identical frames delivered on both sockets during the rotation overlap are
  deduplicated;
* AlarmBridge reacts to RECONNECTED with exactly one refresh_all().
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

import pyadc.websocket.client as ws_client_module
from pyadc.events import EventBroker, EventBrokerTopic
from pyadc.websocket.client import ConnectionEvent, WebSocketClient, WebSocketState


class FakeWS:
    """Minimal stand-in for aiohttp.ClientWebSocketResponse.

    Frames are fed via :meth:`feed`; iteration blocks until a frame arrives
    or the socket is closed (locally via close() or remotely via
    server_close()).
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def feed(self, text: str) -> None:
        self._queue.put_nowait(
            aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, text, None)
        )

    def server_close(self) -> None:
        """Simulate the server closing the connection."""
        self.closed = True
        self._queue.put_nowait(None)

    async def close(self) -> None:
        self.closed = True
        self._queue.put_nowait(None)

    def __aiter__(self) -> "FakeWS":
        return self

    async def __anext__(self) -> aiohttp.WSMessage:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    def exception(self) -> Exception | None:
        return None


def make_bridge(sockets: list[FakeWS]) -> MagicMock:
    """Bridge mock whose ws_connect returns the given sockets in order."""
    bridge = MagicMock()
    bridge.event_broker = EventBroker()
    bridge.auth.get_websocket_token = AsyncMock(
        return_value=("wss://example.test/ws", "jwt-token")
    )
    bridge._session.ws_connect = AsyncMock(side_effect=sockets)
    return bridge


def collect_connection_events(broker: EventBroker) -> list[ConnectionEvent]:
    events: list[ConnectionEvent] = []
    broker.subscribe([EventBrokerTopic.CONNECTION_EVENT], events.append)
    return events


async def wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


# ---------------------------------------------------------------------------
# Token rotation (make-before-break)
# ---------------------------------------------------------------------------

async def test_rotation_swaps_sockets_without_events(monkeypatch):
    monkeypatch.setattr(ws_client_module, "WS_TOKEN_ROTATE_AFTER_S", 0.15)
    monkeypatch.setattr(ws_client_module, "WS_ROTATION_OVERLAP_S", 0.05)

    ws1, ws2, ws3 = FakeWS(), FakeWS(), FakeWS()
    bridge = make_bridge([ws1, ws2, ws3])
    client = WebSocketClient(bridge)
    events = collect_connection_events(bridge.event_broker)

    await client.start()
    try:
        # First rotation: second socket connected, first closed gracefully.
        await wait_for(lambda: bridge._session.ws_connect.call_count >= 2)
        await wait_for(lambda: ws1.closed)
        assert not ws2.closed
        assert client.state is WebSocketState.CONNECTED
        assert client._ws is ws2

        # The handover is seamless: one CONNECTING + one CONNECTED at startup,
        # nothing (especially no RECONNECTED / WAITING) for the rotation.
        states = [e.current_state for e in events]
        assert states == [WebSocketState.CONNECTING, WebSocketState.CONNECTED]
    finally:
        await client.stop()


async def test_rotation_failure_keeps_old_socket(monkeypatch):
    monkeypatch.setattr(ws_client_module, "WS_TOKEN_ROTATE_AFTER_S", 0.15)
    monkeypatch.setattr(ws_client_module, "WS_TOKEN_ROTATE_RETRY_S", 0.1)
    monkeypatch.setattr(ws_client_module, "WS_ROTATION_OVERLAP_S", 0.05)

    ws1, ws2 = FakeWS(), FakeWS()
    bridge = make_bridge([ws1, ConnectionError("boom"), ws2])
    client = WebSocketClient(bridge)

    await client.start()
    try:
        # Rotation attempt fails → old socket must stay open until the retry
        # succeeds, then the swap completes.
        await wait_for(lambda: bridge._session.ws_connect.call_count >= 3)
        await wait_for(lambda: ws1.closed)
        assert client._ws is ws2
        assert client.state is WebSocketState.CONNECTED
    finally:
        await client.stop()


async def test_connect_times_out_on_hung_token_fetch(monkeypatch):
    monkeypatch.setattr(ws_client_module, "WS_CONNECT_TIMEOUT_S", 0.05)

    bridge = make_bridge([])

    async def hung_token_fetch():
        await asyncio.sleep(10)

    bridge.auth.get_websocket_token = hung_token_fetch
    client = WebSocketClient(bridge)

    with pytest.raises(TimeoutError):
        await client._connect()


# ---------------------------------------------------------------------------
# Coverage gap → RECONNECTED
# ---------------------------------------------------------------------------

async def test_unexpected_close_publishes_reconnected(monkeypatch):
    monkeypatch.setattr(ws_client_module, "WS_TOKEN_ROTATE_AFTER_S", 60)
    # Collapse the back-off sleep to ~0 so the test runs fast.
    monkeypatch.setattr(
        ws_client_module.random, "uniform", lambda a, b: -1.99
    )

    ws1, ws2 = FakeWS(), FakeWS()
    bridge = make_bridge([ws1, ws2])
    client = WebSocketClient(bridge)
    events = collect_connection_events(bridge.event_broker)

    await client.start()
    try:
        await wait_for(lambda: client.state is WebSocketState.CONNECTED)
        ws1.server_close()  # unexpected drop → coverage gap

        await wait_for(
            lambda: any(
                e.current_state is WebSocketState.RECONNECTED for e in events
            )
        )
        states = [e.current_state for e in events]
        # CONNECTED (recovery) must precede RECONNECTED so consumers see a
        # live socket when they resync.
        assert states.index(WebSocketState.RECONNECTED) > states.index(
            WebSocketState.WAITING
        )
        assert client._ws is ws2
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# Overlap dedupe
# ---------------------------------------------------------------------------

async def test_duplicate_frames_are_dropped(monkeypatch):
    ws1 = FakeWS()
    bridge = make_bridge([ws1])
    client = WebSocketClient(bridge)

    frame = '{"UnitId": 1, "DeviceId": 2, "NewState": 1, "FlagMask": 1}'
    assert client._is_duplicate_frame(frame) is False
    assert client._is_duplicate_frame(frame) is True

    # After the TTL the same text is accepted again.
    monkeypatch.setattr(ws_client_module, "WS_DEDUP_TTL_S", 0.05)
    await asyncio.sleep(0.06)
    assert client._is_duplicate_frame(frame) is False


async def test_overlap_duplicate_enqueued_once(monkeypatch):
    monkeypatch.setattr(ws_client_module, "WS_TOKEN_ROTATE_AFTER_S", 60)

    ws1 = FakeWS()
    bridge = make_bridge([ws1])
    client = WebSocketClient(bridge)

    published = []
    bridge.event_broker.subscribe(
        [EventBrokerTopic.RAW_RESOURCE_EVENT], published.append
    )

    await client.start()
    try:
        await wait_for(lambda: client.state is WebSocketState.CONNECTED)
        frame = '{"UnitId": 1, "DeviceId": 2, "NewState": 1, "FlagMask": 1}'
        # Same frame arriving twice, as it would on two overlapping sockets.
        ws1.feed(frame)
        ws1.feed(frame)
        ws1.feed('{"UnitId": 1, "DeviceId": 3, "NewState": 2, "FlagMask": 2}')

        await wait_for(lambda: len(published) >= 2)
        await asyncio.sleep(0.05)
        assert len(published) == 2
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# AlarmBridge gap-resync
# ---------------------------------------------------------------------------

async def test_bridge_refreshes_after_gap(mock_session):
    from pyadc import AlarmBridge

    bridge = AlarmBridge(mock_session, "user@example.com", "hunter2")
    bridge._initialized = True
    bridge.refresh_all = AsyncMock()

    bridge.event_broker.publish(
        ConnectionEvent(current_state=WebSocketState.RECONNECTED)
    )
    await asyncio.sleep(0.01)
    bridge.refresh_all.assert_awaited_once()

    # CONNECTED (normal transition) must NOT trigger a refresh.
    bridge.refresh_all.reset_mock()
    bridge.event_broker.publish(
        ConnectionEvent(current_state=WebSocketState.CONNECTED)
    )
    await asyncio.sleep(0.01)
    bridge.refresh_all.assert_not_awaited()


async def test_bridge_ignores_reconnected_before_initialize(mock_session):
    from pyadc import AlarmBridge

    bridge = AlarmBridge(mock_session, "user@example.com", "hunter2")
    bridge.refresh_all = AsyncMock()

    bridge.event_broker.publish(
        ConnectionEvent(current_state=WebSocketState.RECONNECTED)
    )
    await asyncio.sleep(0.01)
    bridge.refresh_all.assert_not_awaited()
