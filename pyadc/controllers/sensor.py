from __future__ import annotations

from typing import TYPE_CHECKING

from pyadc.const import ResourceEventType, ResourceType
from pyadc.controllers.base import BaseController
from pyadc.models.sensor import Sensor, SensorState

if TYPE_CHECKING:
    from pyadc import AlarmBridge


class SensorController(BaseController):
    resource_type = ResourceType.SENSOR
    model_class = Sensor
    _event_state_map = {
        ResourceEventType.OPENED: SensorState.OPEN,
        ResourceEventType.CLOSED: SensorState.CLOSED,
        ResourceEventType.OPENED_CLOSED: SensorState.OPENED_CLOSED,
        ResourceEventType.DOOR_LEFT_OPEN: SensorState.OPEN,
        ResourceEventType.DOOR_LEFT_OPEN_RESTORAL: SensorState.CLOSED,
    }

    async def bypass(self, sensor_id: str) -> None:
        """Bypass a sensor."""
        await self._bridge.client.post(
            f"{self.resource_type}/{sensor_id}/bypass",
            {},
        )

    async def unbypass(self, sensor_id: str) -> None:
        """Remove bypass from a sensor."""
        await self._bridge.client.post(
            f"{self.resource_type}/{sensor_id}/unbypass",
            {},
        )
