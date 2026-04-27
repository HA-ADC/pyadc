"""Sensor model for pyadc."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from pyadc.const import DeviceType, DeviceStatusFlags, ResourceType, SensorState, DEVICE_TYPE_LABELS
from pyadc.models.base import AdcDeviceResource, _parse_enum, _extract_attrs

log = logging.getLogger(__name__)


@dataclass
class Sensor(AdcDeviceResource):
    """Alarm.com sensor resource."""

    resource_type: ClassVar[str] = ResourceType.SENSOR
    state: SensorState = SensorState.UNKNOWN
    device_type: DeviceType = DeviceType.CONTACT
    # Ambient temperature value and its unit ("F" or "C").
    # Populated via commercialTemperatureSensors REST on startup
    # and via PropertyChangeWSMessage (always °F) on updates.
    temperature: float | None = None
    temperature_unit: str = "F"  # "F" or "C"

    def apply_status_flags(self, new_state: int, flag_mask: int) -> None:
        """Apply DeviceStatusFlags bitmask; bit 0 = 0→CLOSED, 1→OPEN."""
        super().apply_status_flags(new_state, flag_mask)
        if flag_mask & 0x3:  # BITFLAG_STATE
            self.state = SensorState.OPEN if (new_state & 0x1) else SensorState.CLOSED

    @property
    def is_temperature_sensor(self) -> bool:
        return self.device_type in (DeviceType.TEMPERATURE, DeviceType.TEMPERATURE_SENSOR)

    @property
    def is_open(self) -> bool:
        """Return True when the sensor is in an active/triggered state."""
        return self.state in (
            SensorState.OPEN,
            SensorState.ACTIVE,
            SensorState.WET,
            SensorState.ISSUE,
        )

    @property
    def model_label(self) -> str | None:
        """Human-readable sensor type label derived from device_type."""
        return DEVICE_TYPE_LABELS.get(self.device_type)

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        """Parse from JSON:API resource object."""
        resource_id, name, snake_attrs = _extract_attrs(data)
        return cls(
            resource_id=resource_id,
            name=name,
            state=_parse_enum(snake_attrs, "state", SensorState, SensorState.UNKNOWN),
            device_type=_parse_enum(snake_attrs, "device_type", DeviceType, DeviceType.CONTACT),
            bypassed=snake_attrs.get("is_bypassed", False),
            battery_level_pct=snake_attrs.get("battery_level_null"),
        )
