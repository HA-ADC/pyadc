"""Cover models (garage door, gate) for pyadc."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from pyadc.const import CoverState, ResourceType
from pyadc.models.base import AdcDeviceResource, _camel_to_snake

# WS BITFLAG_STATE bits → CoverState
_WS_BITS_TO_COVER = {0: CoverState.CLOSED, 1: CoverState.OPEN, 2: CoverState.OPENING, 3: CoverState.CLOSING}


def _parse_cover(cls: type, data: dict[str, Any], resource_type: str) -> Any:
    """Shared parsing logic for cover devices."""
    attrs = data.get("attributes", {})
    snake_attrs = {_camel_to_snake(k): v for k, v in attrs.items()}

    raw_state = snake_attrs.get("state")
    try:
        state = CoverState(raw_state) if raw_state is not None else CoverState.UNKNOWN
    except ValueError:
        state = CoverState.UNKNOWN

    raw_desired = snake_attrs.get("desired_state")
    try:
        desired_state = CoverState(raw_desired) if raw_desired is not None else None
    except ValueError:
        desired_state = None

    return cls(
        resource_id=data.get("id", ""),
        name=snake_attrs.get("description", ""),
        state=state,
        desired_state=desired_state,
        battery_level_pct=snake_attrs.get("battery_level_null"),
    )


@dataclass
class GarageDoor(AdcDeviceResource):
    """Alarm.com garage door resource."""

    resource_type: ClassVar[str] = ResourceType.GARAGE_DOOR
    state: CoverState = CoverState.UNKNOWN
    desired_state: CoverState | None = None

    def apply_status_flags(self, new_state: int, flag_mask: int) -> None:
        """Apply DeviceStatusFlags bitmask; bits 0-1 encode cover position."""
        super().apply_status_flags(new_state, flag_mask)
        if flag_mask & 0x3:
            cover = _WS_BITS_TO_COVER.get(new_state & 0x3)
            if cover is not None:
                self.state = cover

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        """Parse from JSON:API resource object."""
        return _parse_cover(cls, data, ResourceType.GARAGE_DOOR)

    @property
    def model_label(self) -> str | None:
        return "Garage Door"


@dataclass
class Gate(AdcDeviceResource):
    """Alarm.com gate resource."""

    resource_type: ClassVar[str] = ResourceType.GATE
    state: CoverState = CoverState.UNKNOWN
    desired_state: CoverState | None = None

    def apply_status_flags(self, new_state: int, flag_mask: int) -> None:
        """Apply DeviceStatusFlags bitmask; bits 0-1 encode cover position."""
        super().apply_status_flags(new_state, flag_mask)
        if flag_mask & 0x3:
            cover = _WS_BITS_TO_COVER.get(new_state & 0x3)
            if cover is not None:
                self.state = cover

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        """Parse from JSON:API resource object."""
        return _parse_cover(cls, data, ResourceType.GATE)

    @property
    def model_label(self) -> str | None:
        return "Gate"
