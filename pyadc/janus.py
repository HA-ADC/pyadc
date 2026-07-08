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

__all__ = [
    "JanusSession",
]

import asyncio
import json
import logging
import threading
import uuid
from typing import Any, Callable

import aiohttp

try:
    from aiortc.mediastreams import MediaStreamTrack as _MediaStreamTrack

    HAS_AIORTC = True
except ImportError:
    _MediaStreamTrack = object  # type: ignore[assignment,misc]

    #: True when the optional ``aiortc`` extra is installed. WebRTC camera
    #: streaming requires it; ``aiortc`` pins ``av<17.0`` and cannot be a hard
    #: dependency (Home Assistant ships ``av==17.x``), so it is opt-in via the
    #: ``pyadc[webrtc]`` extra. All non-streaming functionality works without it.
    HAS_AIORTC = False

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


class _ReconnectableTrack(_MediaStreamTrack):
    """Persistent bridge track that survives Janus RTSP reconnects.

    aiortc's ``RTCRtpSender._run_rtp`` reads frames via ``recv()``.  If the
    track's ``recv()`` raises ``MediaStreamError`` (which happens when Janus
    closes the WebRTC connection on RTSP timeout), ``_run_rtp`` **exits
    permanently** — no further calls to ``replaceTrack`` can restart it.

    This class uses a queue instead of forwarding directly from the Janus
    receiver.  ``recv()`` simply blocks on ``asyncio.Queue.get()`` — it never
    raises ``MediaStreamError``, so ``_run_rtp`` keeps looping.  A separate
    "feeder" coroutine reads from the Janus relay track and pushes frames in.

    On RTSP restart the old feeder is cancelled and a new one is started for
    the new Janus receiver.  ``_browser_pc`` is never touched — no
    ``replaceTrack``, no renegotiation with the browser.  Video resumes as
    soon as the new feeder pushes the first frame (~1 s reconnect window).

    Must inherit from ``MediaStreamTrack`` so that ``RTCRtpSender.__init__``
    recognises it as a track (isinstance check) and sets ``self.__track``
    correctly.  Without this, ``_run_rtp`` loops forever on ``sleep(0.02)``
    waiting for a non-None track and never sends a single packet.
    """

    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind  # overrides MediaStreamTrack.kind class variable
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=5)
        # PTS normalization state — ensures monotonically increasing PTS across
        # Janus stream restarts.  When the RTSP source reconnects it resets its
        # PTS near zero; without correction libvpx raises "pts is smaller than
        # initial pts" and _run_rtp exits permanently.
        self._pts_watermark: int = -1
        self._pts_offset: int = 0
        # Count of frames pushed into the queue.  Only successfully *decoded*
        # frames reach feed_from(), so a stalled counter means the Janus-side
        # decoder is producing nothing — used by the keyframe watchdog.
        self.frames_fed: int = 0

    async def recv(self) -> Any:
        """Return next frame; blocks when empty. Never raises MediaStreamError."""
        return await self._queue.get()

    async def feed_from(self, source_track: Any) -> None:
        """Read frames from *source_track*, normalize PTS, and push into queue.

        Adjusts ``frame.pts`` to be monotonically non-decreasing across stream
        restarts so that aiortc's libvpx encoder never sees a backwards PTS.
        Exits cleanly when the source track ends (``MediaStreamError``) or when
        the task is cancelled (intentional restart / close).  If the queue is
        full, the oldest frame is dropped to prevent memory growth.
        """
        try:
            while True:
                frame = await source_track.recv()
                if frame.pts is not None:
                    adjusted = frame.pts + self._pts_offset
                    if adjusted < self._pts_watermark:
                        # PTS jumped backwards (stream restart) — shift offset
                        # forward so the adjusted value just exceeds the watermark.
                        self._pts_offset += self._pts_watermark - frame.pts + 1
                        adjusted = frame.pts + self._pts_offset
                    if adjusted > self._pts_watermark:
                        self._pts_watermark = adjusted
                    frame.pts = adjusted
                if self._queue.full():
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                self._queue.put_nowait(frame)
                self.frames_fed += 1
        except (asyncio.CancelledError, Exception):
            pass


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
        *,
        add_sps_pps: bool = False,
        name: str | None = None,
    ) -> None:
        self._url = gateway_url
        self._token = token
        self._proxy_url = proxy_url
        self._ice_servers_raw = ice_servers or []
        # Mirror the official ADC player: add_sps_pps comes from the
        # liveVideoSource's spsAndPpsRequired attribute, name is the camera MAC.
        self._add_sps_pps = add_sps_pps
        self._mountpoint_name = name
        self._http_session: aiohttp.ClientSession | None = None  # set in start()

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._janus_session_id: int | None = None
        self._handle_id: int | None = None
        self._stream_id: int | None = None  # dynamic mountpoint ID, stored for explicit destroy on close
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._recv_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._ha_loop: asyncio.AbstractEventLoop | None = None  # set in start()

        # Guard flags
        self._closing: bool = False       # True while close() is running — prevents double-close
        self._stop_handled: bool = False  # True once a stopped event has been acted on

        # Temporary buffers for trickle ICE candidates that arrive before the
        # peer connections are ready.  Drained and cleared as soon as the PC is
        # created.  Caps are intentionally generous — a normal WebRTC session
        # produces only a handful of candidates during startup; the limit only
        # protects against a pathological / malicious Janus gateway.
        _MAX_TRICKLE_QUEUE = 100
        _MAX_PENDING = 50
        self._max_trickle_queue: int = _MAX_TRICKLE_QUEUE
        self._max_pending: int = _MAX_PENDING
        self._janus_trickle_queue: list[dict[str, Any]] = []
        # Buffer for browser trickle ICE candidates that arrive before _browser_pc exists
        self._browser_trickle_queue: list[tuple[str, str | None, int | None]] = []

        # aiortc peer connections — live in the _AiortcWorker's event loop
        self._janus_pc = None   # RTCPeerConnection: aiortc ↔ Janus
        self._browser_pc = None  # RTCPeerConnection: aiortc ↔ Browser
        self._worker = _AiortcWorker.get()

        # Persistent bridge tracks and their feeder tasks (replaced on restart)
        self._reconnectable_tracks: dict[str, "_ReconnectableTrack"] = {}
        self._feeder_tasks: list[asyncio.Task] = []
        # Keyframe watchdog (worker loop) — survives Janus restarts, cancelled
        # only in close().  See _aiortc_keyframe_watchdog.
        self._keyframe_task: asyncio.Task | None = None

        # Restart-storm guards.  A source that immediately and repeatedly emits
        # Janus "stopped" would otherwise trigger a restart every ~1 s, and two
        # overlapping restarts corrupt the WebSocket ("Cannot write to closing
        # transport") and kill the browser PC.  The lock serializes restarts;
        # the counter gives up after several restarts that never delivered a
        # single frame (a genuinely dead source, e.g. an HD relay this camera
        # doesn't support).
        self._restart_lock = asyncio.Lock()
        self._frameless_restarts: int = 0
        self._max_frameless_restarts: int = 4

        # Optional callback invoked (on the HA event loop) when the stream
        # has permanently stopped and the session is being torn down.
        self._on_stopped: "Callable[[], None] | None" = None

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

        Raises:
            JanusError: if the optional ``aiortc`` dependency is not installed.
        """
        if not HAS_AIORTC:
            raise JanusError(
                "WebRTC camera streaming requires the 'aiortc' package, which "
                "is not installed. Install it with 'pip install pyadc[webrtc]'. "
                "Note: aiortc requires av<17.0, which may conflict with the "
                "Home Assistant core environment."
            )

        self._ha_loop = asyncio.get_running_loop()
        self._http_session = http_session

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
            "body": self._mountpoint_create_body(),
        })
        plugin_data = resp.get("plugindata", {}).get("data", {})
        if plugin_data.get("error"):
            raise JanusError(f"Janus create error: {plugin_data['error']}")
        stream_id = plugin_data.get("stream", {}).get("id")
        if not stream_id:
            raise JanusError(f"Janus create: no stream.id in response: {plugin_data}")
        self._stream_id = stream_id
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

    async def wait_first_frame(self, timeout: float) -> bool:
        """HA loop: wait until at least one decoded video frame has bridged.

        Returns True as soon as the video bridge has fed a frame toward the
        browser, False if *timeout* elapses (or the session is closing) first.
        Used by the camera layer to detect a dead source and switch streams.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self._closing:
                return False
            bridge = self._reconnectable_tracks.get("video")
            if bridge is not None and bridge.frames_fed > 0:
                return True
            await asyncio.sleep(0.5)
        bridge = self._reconnectable_tracks.get("video")
        return bridge is not None and bridge.frames_fed > 0

    async def switch_source(
        self,
        proxy_url: str,
        *,
        gateway_url: str | None = None,
        token: str | None = None,
        add_sps_pps: bool | None = None,
    ) -> None:
        """HA loop: swap to a different media source without dropping the browser.

        Updates the Janus connection parameters and replays the whole
        connect/create/watch/start sequence via ``_restart_janus_stream`` —
        ``_browser_pc`` stays alive, so the viewer just sees video start once
        the new mountpoint delivers.  Used to fall back between the HD and SD
        ADC relay endpoints when one of them never produces decodable video.
        """
        self._proxy_url = proxy_url
        if gateway_url:
            self._url = gateway_url
        if token:
            self._token = token
        if add_sps_pps is not None:
            self._add_sps_pps = add_sps_pps
        self._stop_handled = True  # suppress stop-event handling during teardown
        # Deliberate endpoint change — give the new source a fresh restart
        # budget rather than inheriting the failed endpoint's frameless count.
        self._frameless_restarts = 0
        async with self._restart_lock:
            await self._do_restart_janus_stream()

    def _mountpoint_create_body(self) -> dict[str, Any]:
        """Build the streaming-plugin ``create`` request body.

        Mirrors the official ADC web player (adc_rtcplayer.js) exactly:
        ``add_sps_pps`` comes from the liveVideoSource's ``spsAndPpsRequired``
        attribute (hardcoding True stalls the RTSP ingest on cameras that
        don't need injection), ``name`` is the camera MAC, and no extra keys
        (``streaming_type``, ``media_uri_query``, ``timeout_seconds``) are
        sent — the ADC player omits them and the server defaults are correct.
        """
        body: dict[str, Any] = {
            "request": "create",
            "is_private": True,
            "type": "rtp",
            "media_uri": self._proxy_url,
            "add_sps_pps": self._add_sps_pps,
            "is_virtual": False,
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
        }
        if self._mountpoint_name:
            body["name"] = self._mountpoint_name
        return body

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
            if len(self._browser_trickle_queue) >= self._max_trickle_queue:
                _LOGGER.warning("Browser trickle queue full (%d), dropping candidate", self._max_trickle_queue)
                return
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
        """Shut down all connections and tasks.

        Teardown order matters for clean Janus RTSP re-ingest on subsequent
        sessions with the same proxy_url:
        1. Stop keepalive so no further keepalives are sent.
        2. Explicitly destroy the streaming mountpoint (plugin-level destroy)
           so Janus releases the RTSP connection before the session is gone.
           This prevents a re-ingest stall when a new session immediately
           re-uses the same proxy_url.
        3. Destroy the Janus session and close the WebSocket.
        4. Cancel and await recv task (WS is closed so it exits cleanly).
        5. Close aiortc PeerConnections in the worker loop — awaited, not
           fire-and-forget, so old PC objects are fully torn down before a
           new session can reuse the singleton _AiortcWorker.
        """
        if self._closing:
            return
        self._closing = True

        # 1. Stop keepalive
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._keepalive_task = None

        # 2. Destroy streaming mountpoint (recv_task still running to receive response)
        if (
            self._ws
            and not self._ws.closed
            and self._janus_session_id
            and self._stream_id
            and self._handle_id
        ):
            try:
                await asyncio.wait_for(
                    self._tx({
                        "janus": "message",
                        "session_id": self._janus_session_id,
                        "handle_id": self._handle_id,
                        "body": {"request": "destroy", "id": self._stream_id},
                    }),
                    timeout=5.0,
                )
                _LOGGER.debug("Janus streaming mountpoint %s destroyed", self._stream_id)
            except Exception as exc:
                _LOGGER.debug("Janus mountpoint destroy error (non-fatal): %s", exc)

        # 3. Destroy session and close WebSocket
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

        # 4. Cancel and await recv task (WS closure causes it to exit naturally)
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
            self._recv_task = None

        # 5. Close aiortc PCs — awaited so old objects are fully gone before
        #    the next session's setup runs in the same _AiortcWorker loop.
        janus_pc, browser_pc = self._janus_pc, self._browser_pc
        self._janus_pc = None
        self._browser_pc = None

        # Cancel feeder tasks before closing PCs so they don't race with teardown.
        feeder_tasks, self._feeder_tasks = self._feeder_tasks, []
        keyframe_task, self._keyframe_task = self._keyframe_task, None
        if keyframe_task:
            feeder_tasks.append(keyframe_task)
        if feeder_tasks:
            async def _cancel_feeders() -> None:
                for task in feeder_tasks:
                    task.cancel()
            try:
                await self._worker.run(_cancel_feeders(), timeout=5.0)
            except Exception:
                pass

        if janus_pc or browser_pc:
            async def _close_pcs() -> None:
                if browser_pc:
                    await browser_pc.close()
                if janus_pc:
                    await janus_pc.close()
            try:
                await self._worker.run(_close_pcs(), timeout=10.0)
            except Exception as exc:
                _LOGGER.debug("Error closing aiortc PCs: %s", exc)

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
        """Worker-loop: create _janus_pc, answer Janus's offer, return local SDP.

        IMPORTANT: self._janus_pc stays None until AFTER both remote and local
        descriptions are set.  _dispatch (HA loop) checks self._janus_pc to decide
        whether to queue trickle candidates or route them to the worker.  Setting
        it before setRemoteDescription completes causes "addIceCandidate without
        remote description" warnings that silently drop ICE candidates.
        """
        from aiortc import RTCPeerConnection, RTCSessionDescription

        # Use local var so _dispatch keeps queuing candidates in
        # _janus_trickle_queue during the entire negotiation window.
        new_pc = RTCPeerConnection()

        @new_pc.on("track")
        def on_track(track):
            _LOGGER.debug("aiortc received track from Janus: %s", track.kind)

        # Send aiortc's ICE candidates back to Janus (via HA loop's WebSocket)
        ha_loop = self._ha_loop

        @new_pc.on("icecandidate")
        def on_icecandidate(candidate):
            if candidate is None or ha_loop is None:
                return
            asyncio.run_coroutine_threadsafe(
                self._send_trickle(candidate), ha_loop
            )

        offer = RTCSessionDescription(sdp=janus_offer_sdp, type="offer")
        await new_pc.setRemoteDescription(offer)
        answer = await new_pc.createAnswer()
        await new_pc.setLocalDescription(answer)

        # Both descriptions are set — publish the PC so _dispatch routes
        # subsequent trickle candidates directly instead of queuing them.
        self._janus_pc = new_pc

        # Drain trickle candidates queued while the PC was being negotiated.
        for cand in self._janus_trickle_queue:
            await self._aiortc_apply_janus_candidate(cand)
        self._janus_trickle_queue.clear()

        return new_pc.localDescription.sdp

    async def _aiortc_keyframe_watchdog(self) -> None:
        """Worker-loop: send RTCP PLI to Janus whenever decoded video stalls.

        Some ADC cameras use very long GOPs and only emit an H264 IDR (plus
        SPS/PPS) when a viewer asks for one.  A real browser does this via
        RTCP PLI as soon as its decoder can't produce a picture, but aiortc
        only sends PLI on jitter-buffer overflow — RTP that arrives intact yet
        references a keyframe we never received is silently discarded by the
        decoder ("no frame!") forever.  Without this watchdog those cameras
        deliver P-slices only and the browser never gets a single frame.

        Every tick, compare the video bridge's decoded-frame counter with the
        previous tick; if it hasn't advanced, fire a PLI at every active video
        SSRC on the current ``_janus_pc``.  ADC's Janus proxy forwards the PLI
        upstream and the camera responds with a fresh IDR.  Reads
        ``self._janus_pc`` each tick so it keeps working across Janus stream
        restarts; cancelled only in close().
        """
        from struct import pack

        from aiortc.rtp import RTCP_PSFB_FIR, RtcpPsfbPacket

        last_fed = 0
        fir_seq = 0
        while True:
            await asyncio.sleep(2.0)
            bridge = self._reconnectable_tracks.get("video")
            pc = self._janus_pc
            if bridge is None or pc is None:
                continue
            fed = bridge.frames_fed
            if fed != last_fed:
                last_fed = fed
                continue
            try:
                for receiver in pc.getReceivers():
                    track = receiver.track
                    if track is None or track.kind != "video":
                        continue
                    # aiortc has no public keyframe-request API; use the same
                    # internals its own jitter buffer path uses.
                    ssrcs = list(
                        getattr(receiver, "_RTCRtpReceiver__active_ssrc", {})
                    )
                    rtcp_ssrc = getattr(
                        receiver, "_RTCRtpReceiver__rtcp_ssrc", None
                    )
                    for ssrc in ssrcs:
                        await receiver._send_rtcp_pli(ssrc)
                        # Some sources ignore PLI but honor FIR (RFC 5104
                        # Full Intra Request) — send both while stalled.
                        if rtcp_ssrc is not None:
                            fir_seq = (fir_seq + 1) % 256
                            await receiver._send_rtcp(
                                RtcpPsfbPacket(
                                    fmt=RTCP_PSFB_FIR,
                                    ssrc=rtcp_ssrc,
                                    media_ssrc=ssrc,
                                    fci=pack("!LB3x", ssrc, fir_seq),
                                )
                            )
                    if ssrcs:
                        _LOGGER.debug(
                            "Video stalled (%d frames) — sent PLI+FIR keyframe "
                            "request to Janus for SSRC(s) %s",
                            fed, ssrcs,
                        )
            except Exception as exc:
                _LOGGER.debug("Keyframe request failed: %s", exc)

    async def _apply_janus_candidate(self, cand_data: dict[str, Any]) -> None:
        """HA loop: forward a Janus trickle candidate to the worker loop."""
        if self._janus_pc is None:
            if len(self._janus_trickle_queue) >= self._max_trickle_queue:
                _LOGGER.warning("Janus trickle queue full (%d), dropping candidate", self._max_trickle_queue)
                return
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

        # Add _ReconnectableTrack objects to _browser_pc.
        # These persistent bridge tracks survive Janus RTSP reconnects:
        # their recv() blocks on a queue rather than calling into a Janus
        # receiver directly, so aiortc's _run_rtp sender loop never exits
        # due to MediaStreamError when Janus closes the connection.
        track_count = 0
        if self._janus_pc:
            relay = MediaRelay()
            for receiver in self._janus_pc.getReceivers():
                track = receiver.track
                if track:
                    bridge = _ReconnectableTrack(track.kind)
                    self._reconnectable_tracks[track.kind] = bridge
                    self._browser_pc.addTrack(bridge)
                    feeder = asyncio.create_task(
                        bridge.feed_from(relay.subscribe(track)),
                        name=f"janus-feeder-{track.kind}",
                    )
                    self._feeder_tasks.append(feeder)
                    track_count += 1
        _LOGGER.debug("Browser PC: relaying %d track(s) from Janus", track_count)

        if self._keyframe_task is None:
            self._keyframe_task = asyncio.create_task(
                self._aiortc_keyframe_watchdog(), name="janus-keyframe-watchdog"
            )

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
        if len(self._pending) >= self._max_pending:
            raise JanusError(f"Too many in-flight Janus transactions ({self._max_pending})")
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
        ws_died_unexpectedly = False
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
                    ws_died_unexpectedly = True
                    break
        except asyncio.CancelledError:
            pass  # intentionally cancelled by close() or _restart_janus_stream()
        except Exception as exc:
            _LOGGER.debug("Janus recv exception: %s", exc)
            ws_died_unexpectedly = True
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(JanusError("WebSocket closed"))
            self._pending.clear()
            # If the WS died on its own (network drop, server reset, etc.) while
            # we were streaming — not cancelled by close() or _restart_janus_stream()
            # — trigger a transparent reconnect.  Without this, the feeder tasks
            # exit silently when the Janus PC's receiver tracks end, leaving
            # packet counts frozen indefinitely with no recovery path.
            if ws_died_unexpectedly and not self._closing and not self._stop_handled and self._ha_loop:
                _LOGGER.warning("Janus WS closed unexpectedly — attempting reconnect")
                self._stop_handled = True
                asyncio.run_coroutine_threadsafe(
                    self._restart_janus_stream(), self._ha_loop
                )

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

            plugin_data = data.get("plugindata", {}).get("data", {})
            # Check for stream-stopped events from the ADC custom plugin or the
            # standard streaming plugin.  Both fire when the RTSP ingest dies
            # (e.g. camera goes offline, network interruption, or server-side
            # 3-minute RTSP limit reached).  We attempt a transparent Janus
            # reconnect to keep the browser's WebRTC session alive, avoiding a
            # frozen last frame without requiring browser-side reconnect logic.
            status = (
                plugin_data.get("result", {}).get("status")  # streaming plugin
                or plugin_data.get("status")                  # adc_streaming plugin
            )
            if status == "stopped" and not self._closing and not self._stop_handled:
                self._stop_handled = True
                reason = plugin_data.get("message", "stream stopped")
                _LOGGER.warning("Janus stream stopped: %s — will attempt reconnect", reason)
                if self._ha_loop:
                    asyncio.run_coroutine_threadsafe(
                        self._restart_janus_stream(), self._ha_loop
                    )
                return
            _LOGGER.debug("Janus handle event: %s", plugin_data)
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


    async def _restart_janus_stream(self) -> None:
        """HA loop: transparently reconnect to Janus after an RTSP stop event.

        Tears down only the Janus WebSocket/session (Janus-side), then opens a
        fresh session with a new dynamic mountpoint and re-negotiates only
        ``_janus_pc``.  ``_browser_pc`` is kept alive so the browser's WebRTC
        connection is uninterrupted — no frozen frame, no need for browser-side
        reconnect logic.

        Falls back to a full ``close()`` if the reconnect fails.
        """
        if self._closing:
            return

        # Give up on a source that keeps dropping without ever delivering a
        # frame — restarting it forever just storms the gateway.  A video
        # bridge that has fed frames resets the counter (a legitimate mid-
        # stream drop should reconnect indefinitely).
        bridge = self._reconnectable_tracks.get("video")
        if bridge is not None and bridge.frames_fed > 0:
            self._frameless_restarts = 0
        else:
            self._frameless_restarts += 1
        if self._frameless_restarts > self._max_frameless_restarts:
            _LOGGER.warning(
                "Janus stream stopped %d times without delivering video — "
                "giving up on this source", self._frameless_restarts,
            )
            if self._on_stopped:
                try:
                    self._on_stopped()
                except Exception:
                    pass
            await self.close()
            return

        # Serialize restarts: overlapping teardowns corrupt the shared
        # WebSocket.  If one is already running, this stop event is redundant.
        if self._restart_lock.locked():
            return
        async with self._restart_lock:
            await self._do_restart_janus_stream()

    async def _do_restart_janus_stream(self) -> None:
        if self._closing:
            return
        _LOGGER.info("Restarting Janus RTSP stream (keeping browser connection alive)")

        # ── 1. Tear down Janus-side only (WS + session state) ─────────────
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._keepalive_task = None

        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass

        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
            self._recv_task = None

        # Clear Janus protocol state; _pending futures already drained by _recv_loop.finally
        self._janus_session_id = None
        self._handle_id = None
        self._stream_id = None
        self._pending.clear()
        self._janus_trickle_queue.clear()
        self._stop_handled = False  # reset so next stop triggers another restart

        # Null out _janus_pc NOW (in the HA loop) so that trickle candidates from
        # the new Janus session queue up in _janus_trickle_queue rather than being
        # dispatched to the old closed PC and silently dropped.
        self._janus_pc = None

        # ── 2. Reconnect to Janus and replay the create/watch/start sequence ──
        try:
            if not self._http_session:
                raise JanusError("No HTTP session available for restart")

            self._ws = await self._http_session.ws_connect(
                self._url,
                protocols=("janus-protocol",),
                timeout=aiohttp.ClientTimeout(total=15),
            )
            self._recv_task = asyncio.create_task(self._recv_loop(), name="janus_recv")

            resp = await self._tx({"janus": "create"})
            self._janus_session_id = resp["data"]["id"]
            _LOGGER.debug("Janus restart: session %d", self._janus_session_id)

            resp = await self._tx({
                "janus": "attach",
                "session_id": self._janus_session_id,
                "plugin": "janus.plugin.streaming",
            })
            self._handle_id = resp["data"]["id"]
            _LOGGER.debug("Janus restart: handle %d", self._handle_id)

            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(), name="janus_keepalive"
            )

            resp = await self._tx({
                "janus": "message",
                "session_id": self._janus_session_id,
                "handle_id": self._handle_id,
                "body": self._mountpoint_create_body(),
            })
            plugin_data = resp.get("plugindata", {}).get("data", {})
            if plugin_data.get("error"):
                raise JanusError(f"Janus restart create error: {plugin_data['error']}")
            stream_id = plugin_data.get("stream", {}).get("id")
            if not stream_id:
                raise JanusError(f"Janus restart: no stream.id: {plugin_data}")
            self._stream_id = stream_id
            _LOGGER.debug("Janus restart: stream %s", stream_id)

            resp = await self._tx(
                {
                    "janus": "message",
                    "session_id": self._janus_session_id,
                    "handle_id": self._handle_id,
                    "body": {"request": "watch", "id": stream_id},
                },
                wait_for_jsep=True,
            )
            janus_sdp = resp.get("jsep", {}).get("sdp")
            if not janus_sdp:
                raise JanusError("Janus restart: no SDP from watch")

            # Re-negotiate _janus_pc with the new offer (keeps _browser_pc untouched)
            answer_sdp = await self._worker.run(
                self._aiortc_renegotiate_janus(janus_sdp), timeout=30
            )

            await self._tx(
                {
                    "janus": "message",
                    "session_id": self._janus_session_id,
                    "handle_id": self._handle_id,
                    "body": {"request": "start"},
                    "jsep": {"type": "answer", "sdp": answer_sdp},
                },
                wait_for_jsep=False,
            )
            _LOGGER.info("Janus stream restarted successfully (stream_id=%s)", stream_id)

        except Exception as exc:
            _LOGGER.error("Janus restart failed: %s — falling back to full close", exc)
            # Notify camera layer and do a full teardown
            if self._on_stopped:
                try:
                    self._on_stopped()
                except Exception:
                    pass
            await self.close()

    async def _aiortc_renegotiate_janus(self, janus_offer_sdp: str) -> str:
        """Worker-loop: create a new _janus_pc and reconnect bridge tracks.

        Called after a Janus RTSP stop — the old _janus_pc was closed by Janus.
        We create a fresh RTCPeerConnection, answer the new Janus offer, then
        cancel the old feeder tasks and start new ones that read from the new
        Janus receivers into the persistent _ReconnectableTrack bridge objects.

        The _browser_pc is untouched — _run_rtp is still looping, blocked on
        the empty queue inside each _ReconnectableTrack.  As soon as the new
        feeders start pushing frames, video resumes with no browser reconnect.

        IMPORTANT: self._janus_pc stays None until AFTER both remote and local
        descriptions are set.  _dispatch (HA loop) checks self._janus_pc to decide
        whether to queue trickle candidates or route them to the worker.  Setting
        it before setRemoteDescription completes causes "addIceCandidate without
        remote description" warnings that silently drop ICE candidates → ICE fails
        ~60 s after restart.
        """
        from aiortc import RTCPeerConnection, RTCSessionDescription
        from aiortc.contrib.media import MediaRelay

        # self._janus_pc is already None (nulled in HA loop before calling us).
        # Use a local var so _dispatch keeps routing trickle candidates to
        # _janus_trickle_queue for the entire negotiation window.
        new_pc = RTCPeerConnection()
        ha_loop = self._ha_loop

        @new_pc.on("icecandidate")
        def on_icecandidate(candidate):
            if candidate is None or ha_loop is None:
                return
            asyncio.run_coroutine_threadsafe(
                self._send_trickle(candidate), ha_loop
            )

        offer = RTCSessionDescription(sdp=janus_offer_sdp, type="offer")
        await new_pc.setRemoteDescription(offer)
        answer = await new_pc.createAnswer()
        await new_pc.setLocalDescription(answer)

        # Both descriptions are set — publish the new PC so _dispatch routes
        # subsequent trickle candidates directly here instead of queuing them.
        self._janus_pc = new_pc

        # Drain trickle candidates queued while the new PC was being negotiated.
        for cand in self._janus_trickle_queue:
            await self._aiortc_apply_janus_candidate(cand)
        self._janus_trickle_queue.clear()

        # Cancel old feeder tasks (they were blocked waiting on the dead Janus PC's
        # receiver tracks — cancelling them is safe since _run_rtp reads from the
        # queue, not from the feeders directly).
        for task in self._feeder_tasks:
            task.cancel()
        self._feeder_tasks.clear()

        # Start new feeder tasks that push frames from the new Janus PC into
        # the persistent _ReconnectableTrack bridge objects.
        relay = MediaRelay()
        new_feeder_count = 0
        for receiver in new_pc.getReceivers():
            if receiver.track and receiver.track.kind in self._reconnectable_tracks:
                bridge = self._reconnectable_tracks[receiver.track.kind]
                feeder = asyncio.create_task(
                    bridge.feed_from(relay.subscribe(receiver.track)),
                    name=f"janus-feeder-{receiver.track.kind}",
                )
                self._feeder_tasks.append(feeder)
                new_feeder_count += 1

        _LOGGER.debug(
            "Janus restart: rewired %d bridge feeder(s) to new Janus PC",
            new_feeder_count,
        )

        return new_pc.localDescription.sdp

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

