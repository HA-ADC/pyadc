"""Camera controller for pyadc.

Fetches camera devices from ``video/devices/cameras`` and, on demand,
resolves the ``video/videoSources/liveVideoSources/{id}`` resource for
WebRTC stream info and ``video/snapshots/{id}`` for still images.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from pyadc.const import ResourceEventType, ResourceType
from pyadc.controllers.base import BaseController
from pyadc.events import ResourceEventMessage
from pyadc.models.camera import Camera, LiveVideoSource
from pyadc.websocket.messages import MonitorEventWSMessage

if TYPE_CHECKING:
    from pyadc import AlarmBridge

log = logging.getLogger(__name__)

# Object-detection flags are momentary: a camera reports a single "detected"
# event with no matching "cleared" follow-up, so we drive the flag True then
# auto-restore it to False after this delay (matches the 30s motion window used
# by SensorController for MotionDetected).
_DETECTION_CLEAR_DELAY_S = 30

# Camera model attribute set by each detected object class.
_ATTR_PERSON = "person_detected"
_ATTR_VEHICLE = "vehicle_detected"
_ATTR_ANIMAL = "animal_detected"
_ATTR_PACKAGE = "package_detected"

# VideoCameraTriggered (71) carries `cnff` — a comma-separated list of detected
# ClassificationCategoryTypeEnum *names* (e.g. "Human,Animal,Vehicle").  Map the
# person/vehicle/animal/parcel-family names (lower-cased) to the model attribute.
# Source: ClassificationCategoryTypeEnum.cs +
#   UnitEventVideoKeys.CustomerNotificationFiltersCategories = "cnff".
_CNFF_NAME_TO_ATTR: dict[str, str] = {
    "human": _ATTR_PERSON,
    "familiarface": _ATTR_PERSON,
    "unknownface": _ATTR_PERSON,
    "vehicle": _ATTR_VEHICLE,
    "familiarvehicle": _ATTR_VEHICLE,
    "deliveryvehicle": _ATTR_VEHICLE,
    "animal": _ATTR_ANIMAL,
    "parcel": _ATTR_PACKAGE,
}

# VideoAnalyticsDetection (210) carries `category=<int>` — a single
# ClassificationCategoryTypeEnum value.  Map the person/vehicle/animal/parcel
# integer values to the model attribute.  Source: ClassificationCategoryTypeEnum.cs
#   Human=0, Vehicle=1, Parcel=4, Animal=101, DeliveryVehicle=102,
#   FamiliarVehicle=103, FamiliarFace=104, UnknownFace=105.
_CATEGORY_INT_TO_ATTR: dict[int, str] = {
    0: _ATTR_PERSON,    # Human
    104: _ATTR_PERSON,  # FamiliarFace
    105: _ATTR_PERSON,  # UnknownFace
    1: _ATTR_VEHICLE,   # Vehicle
    102: _ATTR_VEHICLE,  # DeliveryVehicle
    103: _ATTR_VEHICLE,  # FamiliarVehicle
    101: _ATTR_ANIMAL,  # Animal
    4: _ATTR_PACKAGE,   # Parcel
}


class CameraController(BaseController):
    """Controller for Alarm.com video camera devices."""

    resource_type = ResourceType.CAMERA
    model_class = Camera
    _event_state_map = {}  # Cameras don't have simple state transitions

    def __init__(self, bridge: "AlarmBridge") -> None:
        super().__init__(bridge)
        # Pending auto-clear timers for momentary detection flags,
        # keyed by "{resource_id}:{attr}".
        self._clear_handles: dict[str, asyncio.TimerHandle] = {}

    def _handle_monitor_event(
        self,
        msg: MonitorEventWSMessage,
        event_type: ResourceEventType | str | int,
    ) -> None:
        """Route camera video-analytics events to object-detection flags.

        Handles the two confirmed detection events:

        * ``VideoCameraTriggered`` (71) — reads the ``cnff`` classified-object
          list from the extra-data query string.
        * ``VideoAnalyticsDetection`` (210) — reads the single ``category``
          integer from the extra-data query string.

        All other camera events (including ``VideoAnalyticsRuleTurnedOff`` /
        ``VideoAnalyticsRuleResumedAutomatically``, which are rule-config state
        changes, not detections) fall through to the base handler.
        """
        camera: Camera | None = self._get_device_by_ws_id(msg.device_id)
        if camera is None:
            super()._handle_monitor_event(msg, event_type)
            return

        attrs: set[str] = set()
        if event_type == ResourceEventType.VIDEO_CAMERA_TRIGGERED:
            attrs = self._attrs_from_cnff(msg.qstring)
        elif event_type == ResourceEventType.VIDEO_ANALYTICS_DETECTION:
            attrs = self._attrs_from_category(msg.qstring)

        if not attrs:
            super()._handle_monitor_event(msg, event_type)
            return

        for attr in attrs:
            self._trigger_detection(camera, attr)

    @staticmethod
    def _attrs_from_cnff(qstring: str) -> set[str]:
        """Parse the ``cnff`` classified-object list into model attribute names."""
        if not qstring:
            return set()
        values = parse_qs(qstring).get("cnff", [])
        attrs: set[str] = set()
        for value in values:
            for name in value.split(","):
                attr = _CNFF_NAME_TO_ATTR.get(name.strip().lower())
                if attr is not None:
                    attrs.add(attr)
        return attrs

    @staticmethod
    def _attrs_from_category(qstring: str) -> set[str]:
        """Parse the ``category`` integer into a model attribute name."""
        if not qstring:
            return set()
        values = parse_qs(qstring).get("category", [])
        attrs: set[str] = set()
        for value in values:
            try:
                category = int(float(value))
            except (TypeError, ValueError):
                continue
            attr = _CATEGORY_INT_TO_ATTR.get(category)
            if attr is not None:
                attrs.add(attr)
        return attrs

    def _trigger_detection(self, camera: Camera, attr: str) -> None:
        """Drive a detection flag True, notify subscribers, and (re)arm auto-clear."""
        key = f"{camera.resource_id}:{attr}"
        # A repeat detection supersedes the pending clear — reset the timer.
        handle = self._clear_handles.pop(key, None)
        if handle is not None:
            handle.cancel()

        already_on = getattr(camera, attr, False)
        setattr(camera, attr, True)
        if not already_on:
            self._bridge.event_broker.publish(
                ResourceEventMessage(
                    device_id=camera.resource_id,
                    device_type=self.resource_type,
                )
            )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (e.g. unit test) — leave the flag set
        self._clear_handles[key] = loop.call_later(
            _DETECTION_CLEAR_DELAY_S, self._clear_detection, camera.resource_id, attr
        )

    def _clear_detection(self, resource_id: str, attr: str) -> None:
        """Restore a detection flag to False after the momentary window elapses."""
        self._clear_handles.pop(f"{resource_id}:{attr}", None)
        camera: Camera | None = self._get_device_by_ws_id(resource_id)
        if camera is None or not getattr(camera, attr, False):
            return
        setattr(camera, attr, False)
        self._bridge.event_broker.publish(
            ResourceEventMessage(
                device_id=camera.resource_id,
                device_type=self.resource_type,
            )
        )

    async def get_snapshot_url(self, camera: Camera) -> str | None:
        """Fetch a signed snapshot URL for *camera*.

        Returns a short-lived HTTPS URL pointing to a JPEG still frame served
        by the ADC relay, or ``None`` on failure.
        """
        device_id = camera.resource_id
        try:
            resp = await self._get(f"video/snapshots/{device_id}")
        except Exception as exc:
            log.debug("Failed to fetch snapshot for camera %s: %s", device_id, exc)
            return None

        data = resp.get("data", {})
        attrs = data.get("attributes", {}) if data else {}
        url = attrs.get("url")
        if not url:
            log.debug("No snapshot URL in response for camera %s: %s", device_id, attrs)
        return url

    async def get_live_video_source(
        self, camera: Camera, *, hd: bool = True
    ) -> LiveVideoSource | None:
        """Fetch a fresh LiveVideoSource (WebRTC info) for *camera*.

        Stream credentials expire in ~1 hour so this is always called fresh.
        Returns ``None`` if the camera has no videoSource relationship or the
        request fails.

        Args:
            camera: The camera device to fetch stream info for.
            hd: If ``True`` (default), request the highest-resolution stream
                via ``liveVideoHighestResSources``.  Falls back to the standard
                ``liveVideoSources`` endpoint on failure.
        """
        source_id = camera.live_video_source_id or camera.resource_id

        if hd:
            try:
                resp = await self._get(
                    f"video/videoSources/liveVideoHighestResSources/{source_id}"
                )
                log.debug("Using HD stream for camera %s", camera.resource_id)
            except Exception as exc:
                log.debug(
                    "HD stream unavailable for camera %s, falling back to standard: %s",
                    camera.resource_id, exc,
                )
                hd = False  # fall through to standard endpoint

        if not hd:
            try:
                resp = await self._get(
                    f"video/videoSources/liveVideoSources/{source_id}"
                )
            except Exception as exc:
                log.debug("Failed to fetch liveVideoSource for camera %s: %s", camera.resource_id, exc)
                return None

        data = resp.get("data", {})
        if not data:
            return None

        source = LiveVideoSource.from_json_api(data)
        camera.live_video_source = source
        # Redacted diagnostic: log only the scheme + host of proxy_url so we can
        # tell whether it is a directly-reachable RTSP source (→ go2rtc can pull
        # it and bypass Janus/aiortc) or an ADC-internal address. The path/query
        # is NOT logged because it can carry short-lived auth tokens.
        proxy_scheme = proxy_host = None
        if source.proxy_url:
            parsed = urlparse(source.proxy_url)
            proxy_scheme, proxy_host = parsed.scheme, parsed.hostname
        log.debug(
            "LiveVideoSource for camera %s: isMjpeg=%s proxy=%s://%s (len=%s) janus=%s",
            camera.resource_id,
            source.is_mjpeg,
            proxy_scheme,
            proxy_host,
            len(source.proxy_url or ""),
            source.janus_gateway_url,
        )
        return source
