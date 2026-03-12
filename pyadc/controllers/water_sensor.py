from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pyadc.const import ResourceType
from pyadc.controllers.base import BaseController
from pyadc.models.water_sensor import WaterSensor

if TYPE_CHECKING:
    from pyadc import AlarmBridge

log = logging.getLogger(__name__)


class WaterSensorController(BaseController):
    """Controller for water/leak sensor devices (read-only, state via WS events)."""

    resource_type = ResourceType.WATER_SENSOR
    model_class = WaterSensor
    _event_state_map = {}

    def _parse_device(self, item: dict) -> WaterSensor:
        """Parse a water sensor device and log raw attributes for device identification."""
        device = super()._parse_device(item)
        attrs = item.get("attributes", {})
        managed_type = attrs.get("managedDeviceType")
        model_id = attrs.get("deviceModelId") or attrs.get("device_model_id")
        log.debug(
            "WaterSensor[%s] name=%r managedDeviceType=%r deviceModelId=%r",
            device.resource_id,
            device.name,
            managed_type,
            model_id,
        )
        return device
