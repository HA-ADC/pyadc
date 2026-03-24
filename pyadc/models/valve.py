"""Water valve model for pyadc."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from pyadc.const import ResourceType, ValveState
from pyadc.models.base import AdcDeviceResource, _camel_to_snake

log = logging.getLogger(__name__)


@dataclass
class WaterValve(AdcDeviceResource):
    """Alarm.com water valve resource."""

    resource_type: ClassVar[str] = ResourceType.WATER_VALVE
    state: ValveState = ValveState.UNKNOWN
    # Set to OPEN/CLOSED by HA-initiated commands; cleared when WS confirms.
    # ADC-initiated commands never set this, so they never show transitional state.
    transitioning_to: ValveState | None = field(default=None, repr=False)

    @property
    def is_opening(self) -> bool:
        """True only while a HA open command is in-flight."""
        return self.transitioning_to == ValveState.OPEN

    @property
    def is_closing(self) -> bool:
        """True only while a HA close command is in-flight."""
        return self.transitioning_to == ValveState.CLOSED

    def apply_status_flags(self, new_state: int, flag_mask: int) -> None:
        """Apply DeviceStatusFlags bitmask; bit 0 = 0→CLOSED, 1→OPEN."""
        super().apply_status_flags(new_state, flag_mask)
        if flag_mask & 0x3:
            self.state = ValveState.OPEN if (new_state & 0x1) else ValveState.CLOSED
            self.transitioning_to = None

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        """Parse from JSON:API resource object."""
        attrs = data.get("attributes", {})
        snake_attrs = {_camel_to_snake(k): v for k, v in attrs.items()}

        log.debug("WaterValve raw API attrs: %s", snake_attrs)

        # Trust the API state value even when has_state=False — the API
        # returns a meaningful state integer; has_state only indicates whether
        # the panel tracks state changes, not whether the current value is valid.
        raw_state = snake_attrs.get("state")
        try:
            state = ValveState(raw_state) if raw_state is not None else ValveState.UNKNOWN
        except ValueError:
            state = ValveState.UNKNOWN

        return cls(
            resource_id=data.get("id", ""),
            name=snake_attrs.get("description", ""),
            state=state,
            battery_level_pct=snake_attrs.get("battery_level_null"),
        )

    @property
    def model_label(self) -> str | None:
        return "Water Valve"
