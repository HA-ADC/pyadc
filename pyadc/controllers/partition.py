"""Partition (alarm control panel) controller for pyadc.

Handles REST fetches and arming/disarming actions for
:class:`~pyadc.models.partition.Partition` devices, and maps incoming
WebSocket ``EventWSMessage`` events to :class:`~pyadc.const.ArmingState`
values via the ``_event_state_map``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyadc.const import ArmingState, ResourceEventType, ResourceType
from pyadc.controllers.base import BaseController
from pyadc.models.partition import Partition

if TYPE_CHECKING:
    from pyadc import AlarmBridge


class PartitionController(BaseController):
    """Controller for Alarm.com security partitions.

    Wraps arming, disarming, bypass, and fault-clearing actions, and
    maintains live state via WebSocket event → ArmingState mapping.
    """

    resource_type = ResourceType.PARTITION
    model_class = Partition
    _event_state_map = {
        ResourceEventType.ArmedAway: ArmingState.ARMED_AWAY,
        ResourceEventType.ArmedStay: ArmingState.ARMED_STAY,
        ResourceEventType.ArmedNight: ArmingState.ARMED_NIGHT,
        ResourceEventType.Disarmed: ArmingState.DISARMED,
    }

    async def arm_away(
        self,
        partition_id: str,
        *,
        silent: bool = False,
        force_bypass: bool = False,
        no_entry_delay: bool = False,
        force_arm: bool = False,
    ) -> None:
        """Arm the partition in Away mode.

        Args:
            partition_id: Resource ID of the partition to arm.
            silent: Suppress exit-delay beeps.
            force_bypass: Automatically bypass open sensors that would
                otherwise prevent arming.
            no_entry_delay: Arm without an entry delay (instant alarm).
            force_arm: Arm even if conditions would normally prevent it.
        """
        await self._bridge.client.post(
            f"{self.resource_type}/{partition_id}/armAway",
            {
                "silentArming": silent,
                "forceBypass": force_bypass,
                "noEntryDelay": no_entry_delay,
                "forceArm": force_arm,
            },
        )

    async def arm_stay(
        self,
        partition_id: str,
        *,
        silent: bool = False,
        force_bypass: bool = False,
        no_entry_delay: bool = False,
    ) -> None:
        """Arm the partition in Stay mode (perimeter only).

        Args:
            partition_id: Resource ID of the partition to arm.
            silent: Suppress exit-delay beeps.
            force_bypass: Automatically bypass open sensors.
            no_entry_delay: Arm without an entry delay.
        """
        await self._bridge.client.post(
            f"{self.resource_type}/{partition_id}/armStay",
            {
                "silentArming": silent,
                "forceBypass": force_bypass,
                "noEntryDelay": no_entry_delay,
            },
        )

    async def arm_night(self, partition_id: str) -> None:
        """Arm the partition in Night mode (if supported by the panel).

        Args:
            partition_id: Resource ID of the partition to arm.
        """
        await self._bridge.client.post(
            f"{self.resource_type}/{partition_id}/armNight",
            {"nightArming": True},
        )

    async def disarm(self, partition_id: str, *, clear_alarms: bool = False) -> None:
        """Disarm the partition.

        Args:
            partition_id: Resource ID of the partition to disarm.
            clear_alarms: When ``True``, simultaneously acknowledge any active
                alarms so the panel returns to a clean state.
        """
        await self._bridge.client.post(
            f"{self.resource_type}/{partition_id}/disarm",
            {"clearAlarms": clear_alarms},
        )

    async def bypass_sensors(
        self,
        partition_id: str,
        sensor_ids: list[str],
        *,
        bypass: bool = True,
    ) -> None:
        """Bypass or unbypass a list of sensors on the partition.

        Args:
            partition_id: Resource ID of the owning partition.
            sensor_ids: List of sensor resource IDs to act on.
            bypass: ``True`` to bypass (ignore) the sensors; ``False`` to
                restore them to normal monitoring.
        """
        await self._bridge.client.post(
            f"{self.resource_type}/{partition_id}/bypassSensors",
            {
                "bypass": "|".join(sensor_ids) if bypass else "",
                "unbypass": "" if bypass else "|".join(sensor_ids),
            },
        )

    async def clear_panel_faults(self, partition_id: str) -> None:
        """Clear all panel faults (trouble conditions) on a partition.

        Args:
            partition_id: Resource ID of the partition to clear.
        """
        await self._bridge.client.post(
            f"{self.resource_type}/{partition_id}/clearPanelFaults",
            {},
        )
