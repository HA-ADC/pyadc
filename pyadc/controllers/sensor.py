from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pyadc.const import ResourceEventType, ResourceType
from pyadc.controllers.base import BaseController
from pyadc.events import ResourceEventMessage
from pyadc.models.sensor import Sensor, SensorState
from pyadc.websocket.messages import PropertyChangeWSMessage

if TYPE_CHECKING:
    from pyadc import AlarmBridge

log = logging.getLogger(__name__)

# DevicePropertyEnum.AmbientTemperature100thsF = 1
# Value is in 0.01°F units (e.g., 7234 = 72.34°F)
_AMBIENT_TEMP_PROPERTY_ID = 1
_COMMERCIAL_TEMP_RESOURCE = "devices/commercialTemperatureSensors"


class SensorController(BaseController):
    """Controller for Alarm.com sensors (doors, windows, motion, temperature)."""

    resource_type = ResourceType.SENSOR
    model_class = Sensor
    _event_state_map = {
        ResourceEventType.OPENED: SensorState.OPEN,
        ResourceEventType.CLOSED: SensorState.CLOSED,
        ResourceEventType.OPENED_CLOSED: SensorState.OPENED_CLOSED,
        ResourceEventType.DOOR_LEFT_OPEN: SensorState.OPEN,
        ResourceEventType.DOOR_LEFT_OPEN_RESTORAL: SensorState.CLOSED,
    }

    async def fetch_all(self) -> list[Sensor]:
        """Fetch all sensors, then prime temperature sensors with initial values."""
        devices = await super().fetch_all()
        await self._fetch_initial_temperatures()
        return devices

    async def _fetch_initial_temperatures(self) -> None:
        """Call commercialTemperatureSensors to get current AmbientTemp on startup.

        The regular sensors endpoint doesn't include a temperature value.
        The commercialTemperatureSensors endpoint returns ambientTemp already
        converted to the account's preferred unit (°C or °F).
        """
        temp_sensors = [d for d in self._devices.values() if d.is_temperature_sensor]
        if not temp_sensors:
            return

        # Build ?ids[]=<short_id>&ids[]=... query
        ids_param = "&".join(
            f"ids%5B%5D={s.resource_id.rsplit('-', 1)[-1]}" for s in temp_sensors
        )
        try:
            resp = await self._get(f"{_COMMERCIAL_TEMP_RESOURCE}?{ids_param}")
            items = resp.get("data", [])
            if not isinstance(items, list):
                items = [items] if items else []
            for item in items:
                attrs = item.get("attributes", {})
                ambient = attrs.get("ambientTemp")
                if ambient is None:
                    continue
                item_id = str(item.get("id", ""))
                device = self._get_device_by_ws_id(item_id)
                if device is None or not device.is_temperature_sensor:
                    continue
                device.temperature = float(ambient)
                # The commercial endpoint returns the value in the account's configured
                # unit. There is no explicit unit field in the response, but we know
                # normal indoor temps in °C are always < 50 while in °F they exceed 50.
                device.temperature_unit = "C" if float(ambient) < 50 else "F"
                log.debug("Initial temperature: %s = %s°%s", device.name, ambient, device.temperature_unit)
                self._bridge.event_broker.publish(
                    ResourceEventMessage(
                        device_id=device.resource_id,
                        device_type=self.resource_type,
                    )
                )
        except Exception as err:
            log.debug("Failed to fetch initial temperatures: %s", err)

    def _handle_property_change(self, msg: PropertyChangeWSMessage) -> None:
        """Handle ambient temperature updates from PropertyChangeWSMessage."""
        if msg.property_id != _AMBIENT_TEMP_PROPERTY_ID:
            return
        device: Sensor | None = self._get_device_by_ws_id(msg.device_id)
        if device is None or not device.is_temperature_sensor:
            return
        device.temperature = round(msg.property_value / 100.0, 1)
        device.temperature_unit = "F"
        log.debug("Temperature update: %s → %.1f°F", device.name, device.temperature)
        self._bridge.event_broker.publish(
            ResourceEventMessage(
                device_id=device.resource_id,
                device_type=self.resource_type,
            )
        )

    async def bypass(self, sensor_id: str) -> None:
        """Bypass a sensor."""
        await self._post(f"{self.resource_type}/{sensor_id}/bypass", {})

    async def unbypass(self, sensor_id: str) -> None:
        """Remove bypass from a sensor."""
        await self._post(f"{self.resource_type}/{sensor_id}/unbypass", {})
