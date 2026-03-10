"""Partition (alarm control panel) model for pyadc."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from pyadc.const import ArmingState, ResourceType
from pyadc.models.base import AdcDeviceResource, _camel_to_snake


@dataclass
class Partition(AdcDeviceResource):
    """Alarm.com security partition (alarm control panel) resource.

    A *partition* represents one independently arm/disarm-able zone of a
    security system.  Most residential systems have a single partition; larger
    commercial systems may have several.

    Attributes:
        state: Current arming state (:class:`~pyadc.const.ArmingState`).
        desired_state: Pending state transition requested by the user, or
            ``None`` when the system is in a stable state.
        uncleared_issues: ``True`` if there are panel faults or alarms that
            have not been acknowledged.
        force_bypass_available: Panel supports force-bypass arming.
        no_entry_delay_available: Panel supports arming without an entry delay.
        silent_arming_available: Panel supports silent arming.
        supports_night_arming: Panel has a Night arming mode.
    """

    resource_type: ClassVar[str] = ResourceType.PARTITION
    state: ArmingState = ArmingState.DISARMED
    desired_state: ArmingState | None = None
    uncleared_issues: bool = False
    force_bypass_available: bool = False
    no_entry_delay_available: bool = False
    silent_arming_available: bool = False
    supports_night_arming: bool = False

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        """Parse from JSON:API resource object."""
        attrs = data.get("attributes", {})
        snake_attrs = {_camel_to_snake(k): v for k, v in attrs.items()}

        raw_state = snake_attrs.get("state")
        try:
            state = ArmingState(raw_state) if raw_state is not None else ArmingState.DISARMED
        except ValueError:
            state = ArmingState.DISARMED

        raw_desired = snake_attrs.get("desired_state")
        try:
            desired_state = ArmingState(raw_desired) if raw_desired is not None else None
        except ValueError:
            desired_state = None

        return cls(
            resource_id=data.get("id", ""),
            name=snake_attrs.get("description", ""),
            state=state,
            desired_state=desired_state,
            uncleared_issues=snake_attrs.get("uncleared_issues", False),
            force_bypass_available=snake_attrs.get("force_bypass_available", False),
            no_entry_delay_available=snake_attrs.get("no_entry_delay_available", False),
            silent_arming_available=snake_attrs.get("silent_arming_available", False),
            supports_night_arming=snake_attrs.get("supports_night_arming", False),
            battery_level_pct=snake_attrs.get("battery_level_null"),
        )
