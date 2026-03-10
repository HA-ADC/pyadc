from __future__ import annotations

from typing import TYPE_CHECKING

from pyadc.const import ResourceEventType, ResourceType
from pyadc.controllers.base import BaseController
from pyadc.const import ValveState
from pyadc.models.valve import WaterValve

if TYPE_CHECKING:
    from pyadc import AlarmBridge


class ValveController(BaseController):
    """Controller for water valve devices."""

    resource_type = ResourceType.WATER_VALVE
    model_class = WaterValve
    _event_state_map = {
        ResourceEventType.WATER_VALVE_OPENED: ValveState.OPEN,
        ResourceEventType.WATER_VALVE_CLOSED: ValveState.CLOSED,
    }

    async def open(self, valve_id: str) -> None:
        """Open a water valve."""
        await self._post(
            f"{self.resource_type}/{valve_id}/open",
            {},
        )

    async def close(self, valve_id: str) -> None:
        """Close a water valve."""
        await self._post(
            f"{self.resource_type}/{valve_id}/close",
            {},
        )
