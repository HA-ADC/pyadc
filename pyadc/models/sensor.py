"""Sensor model for pyadc."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from pyadc.const import DeviceType, DeviceStatusFlags, ResourceType, SensorState
from pyadc.models.base import AdcDeviceResource, _camel_to_snake


@dataclass
class Sensor(AdcDeviceResource):
    """Alarm.com sensor resource."""

    resource_type: ClassVar[str] = ResourceType.SENSOR
    state: SensorState = SensorState.UNKNOWN

    def apply_status_flags(self, new_state: int, flag_mask: int) -> None:
        """Apply DeviceStatusFlags bitmask; bit 0 = 0→CLOSED, 1→OPEN."""
        super().apply_status_flags(new_state, flag_mask)
        if flag_mask & 0x3:  # BITFLAG_STATE
            self.state = SensorState.OPEN if (new_state & 0x1) else SensorState.CLOSED
    device_type: DeviceType = DeviceType.CONTACT

    @property
    def is_open(self) -> bool:
        """Return True when the sensor is in an active/triggered state."""
        return self.state in (
            SensorState.OPEN,
            SensorState.ACTIVE,
            SensorState.WET,
            SensorState.ISSUE,
        )

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        """Parse from JSON:API resource object."""
        attrs = data.get("attributes", {})
        snake_attrs = {_camel_to_snake(k): v for k, v in attrs.items()}

        raw_state = snake_attrs.get("state")
        try:
            state = SensorState(raw_state) if raw_state is not None else SensorState.UNKNOWN
        except ValueError:
            state = SensorState.UNKNOWN

        raw_device_type = snake_attrs.get("device_type")
        try:
            device_type = DeviceType(raw_device_type) if raw_device_type is not None else DeviceType.CONTACT
        except ValueError:
            device_type = DeviceType.CONTACT

        return cls(
            resource_id=data.get("id", ""),
            name=snake_attrs.get("description", ""),
            state=state,
            device_type=device_type,
            bypassed=snake_attrs.get("is_bypassed", False),
            battery_level_pct=snake_attrs.get("battery_level_null"),
        )
