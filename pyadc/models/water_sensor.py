"""Water sensor model for pyadc."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from pyadc.const import ResourceType, SensorState
from pyadc.models.base import AdcDeviceResource, _parse_enum, _extract_attrs


@dataclass
class WaterSensor(AdcDeviceResource):
    """Alarm.com water sensor resource."""

    resource_type: ClassVar[str] = ResourceType.WATER_SENSOR
    state: SensorState = SensorState.UNKNOWN

    @property
    def is_wet(self) -> bool:
        """Return True when water is detected."""
        return self.state == SensorState.WET

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        """Parse from JSON:API resource object."""
        resource_id, name, snake_attrs = _extract_attrs(data)
        return cls(
            resource_id=resource_id,
            name=name,
            state=_parse_enum(snake_attrs, "state", SensorState, SensorState.UNKNOWN),
            battery_level_pct=snake_attrs.get("battery_level_null"),
        )

    @property
    def model_label(self) -> str | None:
        return "Water Sensor"
