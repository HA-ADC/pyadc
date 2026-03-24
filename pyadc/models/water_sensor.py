"""Water sensor model for pyadc."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from pyadc.const import ResourceType, SensorState
from pyadc.models.base import AdcDeviceResource, _camel_to_snake


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
        attrs = data.get("attributes", {})
        snake_attrs = {_camel_to_snake(k): v for k, v in attrs.items()}

        raw_state = snake_attrs.get("state")
        try:
            state = SensorState(raw_state) if raw_state is not None else SensorState.UNKNOWN
        except ValueError:
            state = SensorState.UNKNOWN

        return cls(
            resource_id=data.get("id", ""),
            name=snake_attrs.get("description", ""),
            state=state,
            battery_level_pct=snake_attrs.get("battery_level_null"),
        )

    @property
    def model_label(self) -> str | None:
        return "Water Sensor"
