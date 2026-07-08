"""Camera model for pyadc."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Self

from pyadc.const import ResourceType
from pyadc.models.base import AdcDeviceResource, _camel_to_snake, _extract_attrs


@dataclass
class LiveVideoSource:
    """Resolved stream info from video/liveVideoSource.

    Fetched separately from the camera device resource.  The ``proxy_url``
    field contains the URL used by HA to display the stream:

    * ``is_mjpeg=True``  → HTTP VideoRelay MJPEG proxy (requires ADC session
      cookies); each call to the ADC REST endpoint returns a fresh, short-lived
      URL.
    * ``is_mjpeg=False`` → Janus WebRTC proxy flow; ``janus_gateway_url``,
      ``janus_token``, and ``ice_servers`` are populated for the WebRTC path.

    Credentials expire after ~1 hour; the camera controller re-fetches on
    demand.
    """

    proxy_url: str | None = None
    is_mjpeg: bool = False

    # WebRTC proxy fields (Janus Gateway)
    janus_gateway_url: str | None = None
    janus_token: str | None = None
    ice_servers: str | None = None  # JSON-encoded RFC 5766 ICE server list
    # Whether the camera's stream needs SPS/PPS injected by Janus to stream
    # over WebRTC.  Passed through as ``add_sps_pps`` on mountpoint create —
    # the official ADC player forwards this verbatim, never hardcodes it.
    sps_and_pps_required: bool = False

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        """Parse a video/liveVideoSource JSON:API response object."""
        attrs = data.get("attributes", {})
        snake = {_camel_to_snake(k): v for k, v in attrs.items()}

        return cls(
            proxy_url=snake.get("proxy_url"),
            is_mjpeg=bool(snake.get("is_mjpeg", False)),
            janus_gateway_url=snake.get("janus_gateway_url"),
            janus_token=snake.get("janus_token"),
            ice_servers=snake.get("ice_servers"),
            sps_and_pps_required=bool(snake.get("sps_and_pps_required", False)),
        )


@dataclass
class Camera(AdcDeviceResource):
    """An Alarm.com video camera device."""

    resource_type: ClassVar[str] = ResourceType.CAMERA

    # Network / connection
    private_ip: str | None = None
    public_ip: str | None = None
    port: int | None = None

    # Credentials — username is exposed by the REST API; the password is
    # encrypted server-side and is NEVER returned in plain text.
    username: str | None = None

    # Hardware info
    device_model: str | None = None
    firmware_version: str | None = None
    mac_address: str | None = None

    # Capability flags
    can_take_snapshot: bool = False
    supports_live_view: bool = False

    # Relationship ID of the associated video/liveVideoSource resource.
    # Resolved into ``live_video_source`` by the camera controller.
    live_video_source_id: str | None = None

    # Resolved stream info (populated after fetching liveVideoSource)
    live_video_source: LiveVideoSource | None = None

    # Transient object-detection state, driven by video-analytics WebSocket
    # events (VideoCameraTriggered / VideoAnalyticsDetection).  These are
    # momentary: the camera controller sets a flag True when an object of that
    # class is detected and auto-clears it back to False after a short delay.
    # Not populated from the REST API.
    person_detected: bool = False
    vehicle_detected: bool = False
    animal_detected: bool = False
    package_detected: bool = False

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        """Parse from a JSON:API resource object."""
        resource_id, name, snake_attrs = _extract_attrs(data)

        # Extract liveVideoSource relationship ID
        rels = data.get("relationships", {})
        video_source_id: str | None = None
        for key in ("videoSource", "video_source"):
            rel = rels.get(key, {}).get("data")
            if rel and rel.get("type") in (
                "video/liveVideoSource",
                "video/videoSources/liveVideoSource",
            ):
                video_source_id = rel.get("id")
                break

        port_raw = snake_attrs.get("port")
        port: int | None = None
        if port_raw is not None:
            try:
                port = int(port_raw)
            except (TypeError, ValueError):
                port = None

        return cls(
            resource_id=resource_id,
            name=name,
            private_ip=snake_attrs.get("private_ip"),
            public_ip=snake_attrs.get("public_ip"),
            port=port,
            username=snake_attrs.get("username"),
            device_model=snake_attrs.get("device_model"),
            firmware_version=snake_attrs.get("firmware_version"),
            mac_address=snake_attrs.get("mac_address"),
            can_take_snapshot=bool(snake_attrs.get("can_take_snapshot", False)),
            supports_live_view=bool(snake_attrs.get("supports_live_view", False)),
            live_video_source_id=video_source_id,
            battery_level_pct=snake_attrs.get("battery_level_null"),
        )

    @property
    def model_label(self) -> str | None:
        return self.device_model
