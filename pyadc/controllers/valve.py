from __future__ import annotations

from typing import TYPE_CHECKING

from pyadc.const import ResourceEventType, ResourceType
from pyadc.controllers.base import BaseController
from pyadc.models.water_valve import WaterValve, WaterValveState

if TYPE_CHECKING:
    from pyadc import AlarmBridge


class ValveController(BaseController):
    """Controller for water valve devices."""

    resource_type = ResourceType.WATER_VALVE
    model_class = WaterValve
    _event_state_map = {
        ResourceEventType.Opened: WaterValveState.OPEN,
        ResourceEventType.Closed: WaterValveState.CLOSED,
    }

    async def open(self, valve_id: str) -> None:
        """Open a water valve."""
        await self._bridge.client.post(
            f"{self.resource_type}/{valve_id}/open",
            {},
        )

    async def close(self, valve_id: str) -> None:
        """Close a water valve."""
        await self._bridge.client.post(
            f"{self.resource_type}/{valve_id}/close",
            {},
        )
