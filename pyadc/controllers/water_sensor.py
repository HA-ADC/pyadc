from __future__ import annotations

from typing import TYPE_CHECKING

from pyadc.const import ResourceType
from pyadc.controllers.base import BaseController
from pyadc.models.water_sensor import WaterSensor

if TYPE_CHECKING:
    from pyadc import AlarmBridge


class WaterSensorController(BaseController):
    """Controller for water/leak sensor devices (read-only, state via WS events)."""

    resource_type = ResourceType.WATER_SENSOR
    model_class = WaterSensor
    _event_state_map = {}
