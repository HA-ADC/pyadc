from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pyadc.const import ResourceEventType, ResourceType
from pyadc.controllers.base import BaseController
from pyadc.models.base import _extract_attrs
from pyadc.models.image_sensor import ImageSensor

if TYPE_CHECKING:
    from pyadc import AlarmBridge

log = logging.getLogger(__name__)

# Peek-in is issued against the image-sensor *device* endpoint, which is a
# different resource from ``video/smrfImages`` (the images). Confirmed against
# the backend ImageSensorsController: POST imageSensor/imageSensors/{id}/doPeekInNow
# where the route requires an INTEGER device id.
_IMAGE_SENSOR_DEVICE_RESOURCE = "imageSensor/imageSensors"

# Latest uploaded image frames across all image-sensor devices, newest first.
# Confirmed against the backend ImageSensorImagesController:
# GET imageSensor/imageSensorImages/getRecentImages returns ImageSensorImage
# resources, each with an ``imageSrc`` URL and an ``imageSensor`` relationship
# whose id is the numeric image-sensor device id. Panel cameras and PIR image
# cameras are image-sensor devices (backend classifies them as ImageSensors),
# so their captures appear here too.
#
# NOTE: not added to ``pyadc.const.ResourceType`` on purpose — this is a
# read-only helper endpoint local to this controller, not a device collection.
_RECENT_IMAGES_RESOURCE = "imageSensor/imageSensorImages/getRecentImages"

# JSON:API relationship names the recent-images endpoint may use to point at
# the owning image-sensor device. Checked in order.
_IMAGE_SENSOR_RELATIONSHIP_NAMES = ("imageSensor", "image_sensor", "imagesensor")


def _relationship_id(item: dict[str, Any], *names: str) -> str | None:
    """Return the id of the first present JSON:API relationship in ``names``."""
    rels = item.get("relationships")
    if not isinstance(rels, dict):
        return None
    for name in names:
        rel = rels.get(name)
        if isinstance(rel, dict):
            data = rel.get("data")
            if isinstance(data, dict) and data.get("id") is not None:
                return str(data["id"])
    return None


def _parse_iso_timestamp(raw: Any) -> datetime | None:
    """Best-effort parse of an ISO-8601 timestamp (tolerating a trailing 'Z')."""
    if not raw:
        return None
    text = str(raw)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None


class ImageSensorController(BaseController):
    """Controller for image sensor devices."""

    resource_type = ResourceType.IMAGE_SENSOR
    model_class = ImageSensor
    _event_state_map = {
        ResourceEventType.IMAGE_SENSOR_UPLOAD: None,  # triggers event but no simple state change
    }

    def __init__(self, bridge: "AlarmBridge") -> None:
        super().__init__(bridge)
        # {image_sensor_device_short_id: (image_src_url, timestamp)} — latest
        # uploaded/peek-in frame per image-sensor device, populated by
        # :meth:`fetch_recent_images`. Keyed by the numeric device id (the
        # short suffix of a device resource id, e.g. "3" from "104878280-3").
        self._latest_by_device: dict[str, tuple[str, datetime | None]] = {}

    async def peek_in_now(self, sensor_id: str) -> None:
        """Request an immediate image capture (peek-in) from an image sensor.

        The peek-in endpoint lives on the image-sensor device resource and its
        route only accepts an integer device id, so the numeric suffix of the
        resource id is used (e.g. ``"104878280-3"`` → ``3``).
        """
        device_id = sensor_id.rsplit("-", 1)[-1]
        await self._post(
            f"{_IMAGE_SENSOR_DEVICE_RESOURCE}/{device_id}/doPeekInNow",
            {},
        )

    async def fetch_recent_images(self) -> None:
        """Refresh the map of latest image URL per image-sensor device.

        GETs the customer API ``getRecentImages`` endpoint (frames newest-first)
        and records, for each image-sensor device, the most recent frame's
        ``imageSrc`` URL and timestamp. This is the retrieval path for panel
        cameras (Qolsys/Honeywell/GC-Next) and PIR image cameras
        (Climax/DSC/PowerG), which the backend classifies as image sensors and
        which upload captures through the image-sensor upload flow.

        Existing entries for devices absent from this batch are preserved (the
        endpoint only returns a small recent window across all sensors).
        """
        try:
            resp = await self._get(_RECENT_IMAGES_RESOURCE)
        except Exception as err:  # noqa: BLE001 — network/parse errors are non-fatal
            log.debug("Failed to fetch recent image-sensor images: %s", err)
            return

        items = resp.get("data", [])
        if not isinstance(items, list):
            items = [items] if items else []

        # Endpoint returns newest-first, so keep the first URL seen per device.
        latest: dict[str, tuple[str, datetime | None]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            device_id = _relationship_id(item, *_IMAGE_SENSOR_RELATIONSHIP_NAMES)
            if device_id is None:
                continue
            _, _, snake_attrs = _extract_attrs(item)
            url = snake_attrs.get("image_src")
            if not url:
                continue
            ts = _parse_iso_timestamp(snake_attrs.get("timestamp"))
            latest.setdefault(device_id, (url, ts))

        self._latest_by_device.update(latest)

    def latest_image_url(self, device_short_id: str) -> str | None:
        """Return the most recent image URL for an image-sensor device id, if known."""
        entry = self._latest_by_device.get(device_short_id)
        return entry[0] if entry else None

    def latest_image_timestamp(self, device_short_id: str) -> datetime | None:
        """Return the timestamp of the most recent image for a device id, if known."""
        entry = self._latest_by_device.get(device_short_id)
        return entry[1] if entry else None
