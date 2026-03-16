"""Janus WebRTC Gateway WebSocket client with aiortc bridge.

The ADC Janus proxy uses ``janus.plugin.streaming``.  In this plugin, **Janus
is the offerer** — it sends an SDP offer to the client; the client creates an
SDP answer.  This is the opposite of HA's WebRTC model where the browser sends
an offer and the integration returns an answer.

To bridge the role mismatch we use ``aiortc``:

1. Browser → HA: SDP offer (via HA WebSocket)
2. HA → Janus: ``create`` (streaming plugin body with proxy_url as media_uri)
3. Janus → HA: ``stream.id``
4. HA → Janus: ``watch`` with that stream_id (no JSEP)
5. Janus → HA: SDP offer
6. aiortc answers Janus's offer and starts receiving media
7. aiortc creates an SDP answer for the browser
8. HA → Browser: aiortc's SDP answer (in response to the browser's offer)
9. Browser ↔ aiortc: WebRTC media
10. Janus → aiortc: WebRTC media

ADC signaling flow (confirmed from proxy-rtc-player.js):
    create {media_uri, type:"rtp", video/audio codec params}
        → ack → event {stream: {id: <N>}}
    watch {id: <N>} (no JSEP)
        → ack → event {jsep: {type: "offer", sdp: "..."}}
    start {jsep: {type: "answer", sdp: "..."}}
        → ack → event {result: "ok"} + webrtcup
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Janus sessions expire after 60 s of inactivity; keepalive every 25 s.
_KEEPALIVE_INTERVAL = 25


class _AiortcWorker:
    """Singleton background thread with its own asyncio event loop for aiortc.

    aiortc performs synchronous OpenSSL operations (DTLS handshake, SRTP
    encrypt/decrypt) that would block HA's main event loop if run there.
    All ``RTCPeerConnection`` operations run exclusively in this loop.
    """

    _instance: "_AiortcWorker | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        # Increase H264 encoder quality before any PeerConnections are created.
        # aiortc defaults to 1 Mbps with a 3 Mbps cap — far below what modern
        # cameras stream at (typically 2-8 Mbps). The encoder respects REMB
        # feedback from the browser so the browser can negotiate upward, but
        # it starts at DEFAULT_BITRATE and won't exceed MAX_BITRATE.
        try:
            import aiortc.codecs.h264 as _h264
            _h264.DEFAULT_BITRATE = 4_000_000   # 4 Mbps start
            _h264.MAX_BITRATE = 8_000_000       # 8 Mbps ceiling
            _LOGGER.debug("aiortc H264 bitrate: default=4Mbps max=8Mbps")
        except Exception as exc:
            _LOGGER.debug("Could not patch aiortc H264 bitrate: %s", exc)

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="alarmdotcom-aiortc",
        )
        self._thread.start()

    @classmethod
    def get(cls) -> "_AiortcWorker":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
        return cls._instance

    async def run(self, coro, timeout: float = 30.0):
        """Run *coro* in the aiortc loop; await result in the calling loop."""
        ha_loop = asyncio.get_running_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return await ha_loop.run_in_executor(None, fut.result, timeout)

    def schedule(self, coro) -> None:
        """Fire-and-forget: schedule *coro* in the aiortc loop."""
        asyncio.run_coroutine_threadsafe(coro, self._loop)


class JanusError(Exception):
    """Raised on Janus protocol errors."""


class JanusSession:
    """One viewer session: Janus signaling + aiortc bridge to HA browser.

    Lifecycle::

        session = JanusSession(gateway_url, token, proxy_url, ice_servers)
        browser_answer_sdp = await session.start(browser_offer_sdp, http_session)
        # later:
        await session.add_ice_candidate(candidate, sdp_mid, sdp_m_line_index)
        await session.close()
    """

    def __init__(
        self,
        gateway_url: str,
        token: str,
        proxy_url: str,
        ice_servers: list[dict[str, Any]] | None = None,
    ) -> None:
        self._url = gateway_url
        self._token = token
        self._proxy_url = proxy_url
        self._ice_servers_raw = ice_servers or []

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._janus_session_id: int | None = None
        self._handle_id: int | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._recv_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._ha_loop: asyncio.AbstractEventLoop | None = None  # set in start()

        # Buffer for Janus trickle ICE candidates that arrive before _janus_pc exists
        self._janus_trickle_queue: list[dict[str, Any]] = []
        # Buffer for browser trickle ICE candidates that arrive before _browser_pc exists
        self._browser_trickle_queue: list[tuple[str, str | None, int | None]] = []

        # aiortc peer connections — live in the _AiortcWorker's event loop
        self._janus_pc = None   # RTCPeerConnection: aiortc ↔ Janus
        self._browser_pc = None  # RTCPeerConnection: aiortc ↔ Browser
        self._worker = _AiortcWorker.get()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def start(
        self,
        browser_offer_sdp: str,
        http_session: aiohttp.ClientSession,
    ) -> str:
        """Bridge browser's WebRTC offer to Janus and return SDP answer.

        Args:
            browser_offer_sdp: The raw SDP offer string from the HA browser.
            http_session: Shared aiohttp session (reuses existing ADC session).

        Returns:
            SDP answer string to send back to the browser via HA's
            ``send_message(WebRTCAnswer(answer=...))``.
        """
        self._ha_loop = asyncio.get_running_loop()

        # 1. Connect to Janus WebSocket and set up the streaming handle
        self._ws = await http_session.ws_connect(
            self._url,
            protocols=("janus-protocol",),
            timeout=aiohttp.ClientTimeout(total=15),
        )
        self._recv_task = asyncio.create_task(self._recv_loop(), name="janus_recv")

        resp = await self._tx({"janus": "create"})
        self._janus_session_id = resp["data"]["id"]
        _LOGGER.debug("Janus session: %d", self._janus_session_id)

        resp = await self._tx({
            "janus": "attach",
            "session_id": self._janus_session_id,
            "plugin": "janus.plugin.streaming",
        })
        self._handle_id = resp["data"]["id"]
        _LOGGER.debug("Janus handle: %d", self._handle_id)

        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(), name="janus_keepalive"
        )

        # 2. Create a dynamic stream from the proxy RTSP URL.
        #    ADC's Janus streaming plugin creates a mountpoint on demand,
        #    returning a stream ID that we use for `watch`.
        resp = await self._tx({
            "janus": "message",
            "session_id": self._janus_session_id,
            "handle_id": self._handle_id,
            "body": {
                "request": "create",
                "is_private": True,
                "type": "rtp",
                "media_uri": self._proxy_url,
                "media_uri_query": "",
                "add_sps_pps": True,
                "is_virtual": False,
                "streaming_type": "proxy",
                "timeout_seconds": 180,
                "max_timeout_seconds": 900,
                "video": True,
                "videoport": 0,
                "videopt": 126,
                "videortpmap": "H264/90000",
                # profile-level-id: Baseline 3.1 (42e01f) is the most universally
                # compatible H.264 profile. aiortc 1.9+ enforces strict profile
                # matching between the relay track and the browser's offered codecs;
                # High 4.0 (640028) causes "Failed to set remote video description
                # send parameters" because most browsers only offer Baseline.
                "videofmtp": "profile-level-id=42e01f;packetization-mode=1",
            },
        })
        plugin_data = resp.get("plugindata", {}).get("data", {})
        if plugin_data.get("error"):
            raise JanusError(f"Janus create error: {plugin_data['error']}")
        stream_id = plugin_data.get("stream", {}).get("id")
        if not stream_id:
            raise JanusError(f"Janus create: no stream.id in response: {plugin_data}")
        _LOGGER.debug("Janus dynamic stream ID: %s", stream_id)

        # 3. Send watch (no JSEP) — Janus will respond with its SDP offer
        resp = await self._tx(
            {
                "janus": "message",
                "session_id": self._janus_session_id,
                "handle_id": self._handle_id,
                "body": {"request": "watch", "id": stream_id},
            },
            wait_for_jsep=True,
        )
        janus_offer = resp.get("jsep", {})
        if not janus_offer or janus_offer.get("type") != "offer":
            raise JanusError(f"Expected Janus SDP offer, got: {janus_offer!r}")
        janus_sdp = janus_offer["sdp"]
        _LOGGER.debug("Received Janus SDP offer (%d bytes)", len(janus_sdp))

        # 4. aiortc answers Janus's offer; starts receiving Janus's media
        janus_answer_sdp = await self._answer_janus(janus_sdp)

        # 5. Send our answer to Janus
        await self._tx(
            {
                "janus": "message",
                "session_id": self._janus_session_id,
                "handle_id": self._handle_id,
                "body": {"request": "start"},
                "jsep": {"type": "answer", "sdp": janus_answer_sdp},
            },
            wait_for_jsep=False,
        )
        _LOGGER.debug("Janus answered; media flowing Janus → aiortc")

        # 6. aiortc creates its own connection toward the browser,
        #    bridging the tracks it's receiving from Janus.
        browser_answer_sdp = await self._bridge_to_browser(browser_offer_sdp)
        _LOGGER.debug("Browser connection established via aiortc bridge")

        return browser_answer_sdp

    async def add_ice_candidate(
        self,
        candidate: str | None,
        sdp_mid: str | None = None,
        sdp_m_line_index: int | None = None,
    ) -> None:
        """Add a trickle ICE candidate from the browser to the browser-side PC."""
        _LOGGER.debug("Browser trickle candidate received: %s (mid=%s)", candidate, sdp_mid)
        if not candidate:
            return
        if self._browser_pc is None:
            _LOGGER.debug("Browser trickle candidate arrived before _browser_pc ready, queuing")
            self._browser_trickle_queue.append((candidate, sdp_mid, sdp_m_line_index))
            return
        self._worker.schedule(
            self._aiortc_add_browser_candidate(candidate, sdp_mid, sdp_m_line_index)
        )

    async def _aiortc_add_browser_candidate(
        self,
        candidate: str,
        sdp_mid: str | None,
        sdp_m_line_index: int | None,
    ) -> None:
        """Worker-loop: add a browser ICE candidate to _browser_pc."""
        from aiortc.sdp import candidate_from_sdp
        try:
            raw = candidate.removeprefix("candidate:")
            cand = candidate_from_sdp(raw)
            cand.sdpMid = sdp_mid
            cand.sdpMLineIndex = sdp_m_line_index or 0
            await self._browser_pc.addIceCandidate(cand)
            _LOGGER.debug("Added browser ICE candidate to _browser_pc: %s", candidate[:60])
        except Exception as exc:
            _LOGGER.debug("Browser ICE candidate error: %s", exc)

    async def close(self) -> None:
        """Shut down all connections and tasks."""
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self._recv_task:
            self._recv_task.cancel()

        # Close aiortc PCs in the worker loop
        janus_pc, browser_pc = self._janus_pc, self._browser_pc
        self._janus_pc = None
        self._browser_pc = None
        if janus_pc or browser_pc:
            async def _close_pcs():
                if browser_pc:
                    await browser_pc.close()
                if janus_pc:
                    await janus_pc.close()
            self._worker.schedule(_close_pcs())

        if self._ws and not self._ws.closed:
            if self._janus_session_id:
                try:
                    await self._fire({
                        "janus": "destroy",
                        "session_id": self._janus_session_id,
                    })
                except Exception:
                    pass
            try:
                await self._ws.close()
            except Exception:
                pass

        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(JanusError("Session closed"))
        self._pending.clear()

    # ------------------------------------------------------------------ #
    # Internal: aiortc bridging (all run in _AiortcWorker's event loop)
    # ------------------------------------------------------------------ #

    async def _answer_janus(self, janus_offer_sdp: str) -> str:
        """Schedule Janus-side aiortc setup in the worker loop, await SDP answer."""
        return await self._worker.run(
            self._aiortc_answer_janus(janus_offer_sdp), timeout=30
        )

    async def _aiortc_answer_janus(self, janus_offer_sdp: str) -> str:
        """Worker-loop: create _janus_pc, answer Janus's offer, return local SDP."""
        from aiortc import RTCPeerConnection, RTCSessionDescription
        self._janus_pc = RTCPeerConnection()

        @self._janus_pc.on("track")
        def on_track(track):
            _LOGGER.debug("aiortc received track from Janus: %s", track.kind)

        # Send aiortc's ICE candidates back to Janus (via HA loop's WebSocket)
        ha_loop = self._ha_loop

        @self._janus_pc.on("icecandidate")
        def on_icecandidate(candidate):
            if candidate is None or ha_loop is None:
                return
            asyncio.run_coroutine_threadsafe(
                self._send_trickle(candidate), ha_loop
            )

        offer = RTCSessionDescription(sdp=janus_offer_sdp, type="offer")
        await self._janus_pc.setRemoteDescription(offer)

        # Drain Janus trickle candidates that arrived before PC was ready
        for cand in self._janus_trickle_queue:
            await self._aiortc_apply_janus_candidate(cand)
        self._janus_trickle_queue.clear()

        answer = await self._janus_pc.createAnswer()
        await self._janus_pc.setLocalDescription(answer)
        return self._janus_pc.localDescription.sdp

    async def _apply_janus_candidate(self, cand_data: dict[str, Any]) -> None:
        """HA loop: forward a Janus trickle candidate to the worker loop."""
        if self._janus_pc is None:
            self._janus_trickle_queue.append(cand_data)
            return
        self._worker.schedule(self._aiortc_apply_janus_candidate(cand_data))

    async def _aiortc_apply_janus_candidate(self, cand_data: dict[str, Any]) -> None:
        """Worker-loop: add a Janus ICE candidate to _janus_pc."""
        if self._janus_pc is None:
            return
        from aiortc.sdp import candidate_from_sdp
        try:
            candidate_str = cand_data.get("candidate", "")
            completed = cand_data.get("completed", False)
            if completed or not candidate_str:
                await self._janus_pc.addIceCandidate(None)
                return
            raw = candidate_str.removeprefix("candidate:")
            cand = candidate_from_sdp(raw)
            cand.sdpMid = cand_data.get("sdpMid")
            cand.sdpMLineIndex = cand_data.get("sdpMLineIndex") or 0
            await self._janus_pc.addIceCandidate(cand)
            _LOGGER.debug("Added Janus ICE candidate: %s", candidate_str[:60])
        except Exception as exc:
            _LOGGER.debug("Failed to add Janus trickle candidate: %s", exc)

    async def _send_trickle(self, candidate) -> None:
        """HA loop: send an aiortc ICE candidate to Janus via trickle."""
        if not self._ws or self._ws.closed or not self._janus_session_id or not self._handle_id:
            return
        try:
            await self._fire({
                "janus": "trickle",
                "session_id": self._janus_session_id,
                "handle_id": self._handle_id,
                "candidate": {
                    "candidate": (
                        f"candidate:{candidate.foundation} {candidate.component} "
                        f"{candidate.protocol} {candidate.priority} "
                        f"{candidate.ip} {candidate.port} typ {candidate.type}"
                    ),
                    "sdpMid": candidate.sdpMid or "0",
                    "sdpMLineIndex": candidate.sdpMLineIndex or 0,
                },
            })
        except Exception as exc:
            _LOGGER.debug("Trickle send error: %s", exc)

    async def _bridge_to_browser(self, browser_offer_sdp: str) -> str:
        """Schedule browser-side aiortc setup in the worker loop, return SDP answer."""
        return await self._worker.run(
            self._aiortc_bridge_to_browser(browser_offer_sdp), timeout=60
        )

    async def _aiortc_bridge_to_browser(self, browser_offer_sdp: str) -> str:
        """Worker-loop: create _browser_pc, relay Janus tracks, return SDP answer.

        The SDP answer is returned only after ICE gathering completes so that all
        reachable candidates (including TURN relay addresses) are included inline.
        This avoids trickle ICE toward the browser and ensures connectivity even
        from behind Docker network isolation.
        """
        from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
        from aiortc.contrib.media import MediaRelay

        # Build ICE server config from the ADC-provided STUN/TURN list so
        # aiortc uses TURN relay addresses that the macOS browser can reach.
        ice_server_objects = []
        for s in self._ice_servers_raw:
            urls = s.get("urls") or s.get("url") or []
            if isinstance(urls, str):
                urls = [urls]
            username = s.get("username") or s.get("userName")
            credential = s.get("credential")
            try:
                ice_server_objects.append(
                    RTCIceServer(urls=urls, username=username, credential=credential)
                )
            except Exception:
                pass  # skip malformed entries

        _LOGGER.debug(
            "Browser PC ICE config: %d server(s): %s",
            len(ice_server_objects),
            [str(s.urls) for s in ice_server_objects],
        )
        config = RTCConfiguration(iceServers=ice_server_objects) if ice_server_objects else None
        self._browser_pc = RTCPeerConnection(configuration=config)

        # Register state-change watchers before any SDP operations.
        loop = asyncio.get_running_loop()
        gather_event = asyncio.Event()

        @self._browser_pc.on("icegatheringstatechange")
        def on_gathering_state():
            state = self._browser_pc.iceGatheringState if self._browser_pc else "closed"
            _LOGGER.debug("Browser PC ICE gathering state: %s", state)
            if state == "complete":
                loop.call_soon_threadsafe(gather_event.set)

        @self._browser_pc.on("iceconnectionstatechange")
        def on_ice_connection_state():
            state = self._browser_pc.iceConnectionState if self._browser_pc else "closed"
            _LOGGER.debug("Browser PC ICE connection state: %s", state)

        @self._browser_pc.on("connectionstatechange")
        def on_connection_state():
            state = self._browser_pc.connectionState if self._browser_pc else "closed"
            _LOGGER.debug("Browser PC connection state: %s", state)

        # Relay tracks from _janus_pc into _browser_pc
        track_count = 0
        if self._janus_pc:
            relay = MediaRelay()
            for receiver in self._janus_pc.getReceivers():
                track = receiver.track
                if track:
                    self._browser_pc.addTrack(relay.subscribe(track))
                    track_count += 1
        _LOGGER.debug("Browser PC: relaying %d track(s) from Janus", track_count)

        offer = RTCSessionDescription(sdp=browser_offer_sdp, type="offer")

        # Log what ICE candidates the browser included in its offer
        offer_cands = [ln for ln in browser_offer_sdp.splitlines() if ln.startswith("a=candidate:")]
        _LOGGER.debug("Browser offer has %d inline ICE candidates: %s", len(offer_cands), offer_cands[:3])

        await self._browser_pc.setRemoteDescription(offer)
        answer = await self._browser_pc.createAnswer()

        # setLocalDescription starts ICE gathering; we wait for it to complete
        # so TURN relay candidates are included inline (browser can reach them
        # even though it can't reach the Docker host 172.17.0.2 directly).
        await self._browser_pc.setLocalDescription(answer)

        if self._browser_pc.iceGatheringState == "complete":
            gather_event.set()

        try:
            await asyncio.wait_for(gather_event.wait(), timeout=20)
        except asyncio.TimeoutError:
            _LOGGER.warning("Browser PC ICE gathering timed out; returning partial SDP")

        sdp = self._browser_pc.localDescription.sdp
        cand_lines = [ln for ln in sdp.splitlines() if ln.startswith("a=candidate:")]
        _LOGGER.debug(
            "Browser SDP answer ready: %d ICE candidates: %s",
            len(cand_lines),
            cand_lines[:3],
        )

        # Drain any browser trickle candidates that arrived during ICE gathering
        for cand, mid, idx in self._browser_trickle_queue:
            await self._aiortc_add_browser_candidate(cand, mid, idx)
        self._browser_trickle_queue.clear()

        return sdp

    # ------------------------------------------------------------------ #
    # Internal: Janus WebSocket protocol
    # ------------------------------------------------------------------ #

    async def _tx(
        self,
        msg: dict[str, Any],
        wait_for_jsep: bool = False,
    ) -> dict[str, Any]:
        """Send a Janus request and wait for a response.

        Args:
            msg: Janus message dict (without ``transaction`` or ``token``).
            wait_for_jsep: If True, keep waiting past the first event until
                           one with a ``jsep`` field arrives.
        """
        tx = uuid.uuid4().hex[:8]
        msg = {**msg, "transaction": tx, "token": self._token}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[tx] = fut

        assert self._ws is not None
        await self._ws.send_str(json.dumps(msg))

        try:
            resp = await asyncio.wait_for(fut, timeout=20)
        except asyncio.TimeoutError:
            self._pending.pop(tx, None)
            raise JanusError(f"Timeout waiting for Janus ({msg.get('janus')})")

        if resp.get("janus") == "error":
            err = resp.get("error", {})
            raise JanusError(f"Janus error {err.get('code')}: {err.get('reason')}")

        # If caller wants a JSEP but this event doesn't have one, keep waiting
        if wait_for_jsep and not resp.get("jsep"):
            _LOGGER.debug("Event without JSEP (status=%s), waiting for offer...",
                          resp.get("plugindata", {}).get("data", {}).get("result", {}).get("status"))
            # Re-register future for the same transaction to catch next event
            loop = asyncio.get_running_loop()
            fut2: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._pending[tx] = fut2
            try:
                resp = await asyncio.wait_for(fut2, timeout=20)
            except asyncio.TimeoutError:
                self._pending.pop(tx, None)
                raise JanusError("Timeout waiting for Janus JSEP offer")
            if resp.get("janus") == "error":
                err = resp.get("error", {})
                raise JanusError(f"Janus error {err.get('code')}: {err.get('reason')}")

        return resp

    async def _fire(self, msg: dict[str, Any]) -> None:
        """Send a Janus message with no response expected."""
        if self._ws is None or self._ws.closed:
            return
        msg = {**msg, "transaction": uuid.uuid4().hex[:8], "token": self._token}
        try:
            await self._ws.send_str(json.dumps(msg))
        except Exception as exc:
            _LOGGER.debug("Janus fire error: %s", exc)

    async def _recv_loop(self) -> None:
        """Read messages from the Janus WebSocket."""
        try:
            async for ws_msg in self._ws:  # type: ignore[union-attr]
                if ws_msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(ws_msg.data)
                    # Log everything except keepalive acks to diagnose flow
                    if data.get("janus") not in ("ack",) or data.get("jsep"):
                        if data.get("janus") == "trickle":
                            _LOGGER.debug("Janus RAW <<< TRICKLE: %s", data)
                        else:
                            _LOGGER.debug("Janus RAW <<< janus=%s tx=%s jsep=%s plugindata=%s",
                                          data.get("janus"),
                                          data.get("transaction"),
                                          bool(data.get("jsep")),
                                          data.get("plugindata", {}).get("data", {}))
                    self._dispatch(data)
                elif ws_msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    _LOGGER.debug("Janus WS closed/error: type=%s", ws_msg.type)
                    break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _LOGGER.debug("Janus recv exception: %s", exc)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(JanusError("WebSocket closed"))
            self._pending.clear()

    def _dispatch(self, data: dict[str, Any]) -> None:
        """Route a Janus message to a pending future."""
        tx = data.get("transaction")
        janus_type = data.get("janus", "")

        if tx and tx in self._pending:
            if janus_type == "ack":
                _LOGGER.debug("Janus ack for tx=%s (pending=%d)", tx, len(self._pending))
                return  # Real result comes in a subsequent event
            fut = self._pending.pop(tx)
            if not fut.done():
                fut.set_result(data)
        elif janus_type == "event":
            # Janus streaming plugin may send the SDP offer as a handle-level
            # event without a transaction ID (async notification after "watch").
            jsep = data.get("jsep")
            if jsep:
                _LOGGER.debug("Janus handle event with JSEP type=%s", jsep.get("type"))
                # Deliver to the oldest pending future (the one waiting for JSEP)
                for pending_tx, fut in list(self._pending.items()):
                    if not fut.done():
                        self._pending.pop(pending_tx, None)
                        fut.set_result(data)
                        return
            _LOGGER.debug(
                "Janus handle event: %s",
                data.get("plugindata", {}).get("data", {}),
            )
        elif janus_type == "hangup":
            _LOGGER.info("Janus hangup: %s", data.get("reason"))
        elif janus_type == "trickle":
            # Janus sends its ICE candidates as unsolicited trickle messages
            cand = data.get("candidate") or data.get("candidates") or {}
            _LOGGER.debug("Janus trickle candidate: %s", cand)
            if cand:
                if self._janus_pc is not None:
                    # PC exists in worker loop — schedule directly there
                    self._worker.schedule(self._aiortc_apply_janus_candidate(cand))
                else:
                    self._janus_trickle_queue.append(cand)
        elif janus_type in ("webrtcup", "media", "slowlink"):
            _LOGGER.debug("Janus %s for handle %s", janus_type, data.get("sender"))


    async def _keepalive_loop(self) -> None:
        """Send Janus keepalives every 25 seconds."""
        try:
            while True:
                await asyncio.sleep(_KEEPALIVE_INTERVAL)
                if self._janus_session_id and self._ws and not self._ws.closed:
                    await self._fire({
                        "janus": "keepalive",
                        "session_id": self._janus_session_id,
                    })
        except asyncio.CancelledError:
            pass

