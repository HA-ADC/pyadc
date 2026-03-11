"""Camera controller for pyadc.

Fetches camera devices from ``video/devices/cameras`` and, on demand,
resolves the ``video/videoSources/liveVideoSources/{id}`` resource for
WebRTC stream info and ``video/snapshots/{id}`` for still images.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pyadc.const import ResourceType
from pyadc.controllers.base import BaseController
from pyadc.models.camera import Camera, LiveVideoSource

if TYPE_CHECKING:
    from pyadc import AlarmBridge

log = logging.getLogger(__name__)


class CameraController(BaseController):
    """Controller for Alarm.com video camera devices."""

    resource_type = ResourceType.CAMERA
    model_class = Camera
    _event_state_map = {}  # Cameras don't have simple state transitions

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

    async def get_live_video_source(self, camera: Camera) -> LiveVideoSource | None:
        """Fetch a fresh LiveVideoSource (WebRTC info) for *camera*.

        Stream credentials expire in ~1 hour so this is always called fresh.
        Returns ``None`` if the camera has no videoSource relationship or the
        request fails.
        """
        source_id = camera.live_video_source_id or camera.resource_id
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
        log.debug(
            "LiveVideoSource for camera %s: isMjpeg=%s proxy_url_len=%s janus=%s",
            camera.resource_id,
            source.is_mjpeg,
            len(source.proxy_url or ""),
            source.janus_gateway_url,
        )
        return source
