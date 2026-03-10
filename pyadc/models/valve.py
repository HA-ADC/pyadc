"""Water valve model for pyadc."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from pyadc.const import ResourceType, ValveState
from pyadc.models.base import AdcDeviceResource, _camel_to_snake


@dataclass
class WaterValve(AdcDeviceResource):
    """Alarm.com water valve resource."""

    resource_type: ClassVar[str] = ResourceType.WATER_VALVE
    state: ValveState = ValveState.UNKNOWN
    desired_state: ValveState | None = None

    def apply_status_flags(self, new_state: int, flag_mask: int) -> None:
        """Apply DeviceStatusFlags bitmask; bit 0 = 0→CLOSED, 1→OPEN."""
        super().apply_status_flags(new_state, flag_mask)
        if flag_mask & 0x3:
            self.state = ValveState.OPEN if (new_state & 0x1) else ValveState.CLOSED

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        """Parse from JSON:API resource object."""
        attrs = data.get("attributes", {})
        snake_attrs = {_camel_to_snake(k): v for k, v in attrs.items()}

        raw_state = snake_attrs.get("state")
        try:
            state = ValveState(raw_state) if raw_state is not None else ValveState.UNKNOWN
        except ValueError:
            state = ValveState.UNKNOWN

        raw_desired = snake_attrs.get("desired_state")
        try:
            desired_state = ValveState(raw_desired) if raw_desired is not None else None
        except ValueError:
            desired_state = None

        return cls(
            resource_id=data.get("id", ""),
            name=snake_attrs.get("description", ""),
            state=state,
            desired_state=desired_state,
            battery_level_pct=snake_attrs.get("battery_level_null"),
        )
