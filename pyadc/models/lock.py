"""Lock model for pyadc."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from pyadc.const import DeviceStatusFlags, LockState, ResourceType
from pyadc.models.base import AdcDeviceResource, _camel_to_snake


@dataclass
class Lock(AdcDeviceResource):
    """Alarm.com lock resource."""

    resource_type: ClassVar[str] = ResourceType.LOCK
    state: LockState = LockState.UNKNOWN

    def apply_status_flags(self, new_state: int, flag_mask: int) -> None:
        """Apply DeviceStatusFlags bitmask; bit 0 = 0→LOCKED, 1→UNLOCKED."""
        super().apply_status_flags(new_state, flag_mask)
        if flag_mask & 0x3:  # BITFLAG_STATE
            bit_state = new_state & 0x3
            self.state = LockState.UNLOCKED if bit_state else LockState.LOCKED
    desired_state: LockState | None = None
    supports_temporary_user_codes: bool = False
    max_user_code_length: int = 0

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        """Parse from JSON:API resource object."""
        attrs = data.get("attributes", {})
        snake_attrs = {_camel_to_snake(k): v for k, v in attrs.items()}

        raw_state = snake_attrs.get("state")
        try:
            state = LockState(raw_state) if raw_state is not None else LockState.UNKNOWN
        except ValueError:
            state = LockState.UNKNOWN

        raw_desired = snake_attrs.get("desired_state")
        try:
            desired_state = LockState(raw_desired) if raw_desired is not None else None
        except ValueError:
            desired_state = None

        return cls(
            resource_id=data.get("id", ""),
            name=snake_attrs.get("description", ""),
            state=state,
            desired_state=desired_state,
            supports_temporary_user_codes=snake_attrs.get("supports_temporary_user_codes", False),
            max_user_code_length=snake_attrs.get("max_user_code_length", 0),
            battery_level_pct=snake_attrs.get("battery_level_null"),
        )
