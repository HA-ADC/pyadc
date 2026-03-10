from __future__ import annotations

from typing import TYPE_CHECKING

from pyadc.const import ResourceEventType, ResourceType
from pyadc.controllers.base import BaseController
from pyadc.models.image_sensor import ImageSensor

if TYPE_CHECKING:
    from pyadc import AlarmBridge


class ImageSensorController(BaseController):
    """Controller for image sensor devices."""

    resource_type = ResourceType.IMAGE_SENSOR
    model_class = ImageSensor
    _event_state_map = {
        ResourceEventType.IMAGE_SENSOR_UPLOAD: None,  # triggers event but no simple state change
    }

    async def peek_in_now(self, sensor_id: str) -> None:
        """Request an immediate image capture from the sensor."""
        await self._post(
            f"{self.resource_type}/{sensor_id}/doPeekInNow",
            {},
        )
