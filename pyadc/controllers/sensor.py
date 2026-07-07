from __future__ import annotations

import asyncio
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

# Some ADC events are *momentary*: the panel reports a single "it happened"
# event with no matching "cleared" follow-up. Examples:
#   * MotionDetected — motion sensor / panel motion.
#   * OpenedClosed  — a door/window/garage contact that opened and closed as one
#     event (this is what a garage-door contact emits; without special handling
#     it would never register as open — see GitHub issue, "garage door open not
#     detected"). ADC does not send a sustained Opened for these.
# For these we briefly drive the sensor to its active state so automations and
# the logbook see the event, then auto-restore it to CLOSED after a short delay.
# The delay is per-event-type:
#   * OpenedClosed  — a momentary open/close pulse; clear almost immediately (1s)
#     since waiting 30s to restore a single pulse feels goofy.
#   * MotionDetected — keep "detected" for a longer window (30s) so automations
#     and the logbook have time to react to motion.
_MOMENTARY_CLEAR_DELAY_S = {
    ResourceEventType.OPENED_CLOSED: 1,
    ResourceEventType.MOTION_DETECTED: 30,
}


class SensorController(BaseController):
    """Controller for Alarm.com sensors (doors, windows, motion, temperature)."""

    resource_type = ResourceType.SENSOR
    model_class = Sensor
    _event_state_map = {
        ResourceEventType.OPENED: SensorState.OPEN,
        ResourceEventType.CLOSED: SensorState.CLOSED,
        # Momentary "opened & closed" pulse → briefly OPEN, then auto-cleared.
        ResourceEventType.OPENED_CLOSED: SensorState.OPEN,
        ResourceEventType.DOOR_LEFT_OPEN: SensorState.OPEN,
        ResourceEventType.DOOR_LEFT_OPEN_RESTORAL: SensorState.CLOSED,
        # Motion (incl. panel-camera motion). Momentary — auto-cleared below.
        ResourceEventType.MOTION_DETECTED: SensorState.ACTIVE,
    }

    # Events that set a transient active state and must auto-restore to CLOSED.
    _MOMENTARY_EVENTS = frozenset(
        {ResourceEventType.MOTION_DETECTED, ResourceEventType.OPENED_CLOSED}
    )

    def __init__(self, bridge: "AlarmBridge") -> None:
        super().__init__(bridge)
        # Pending auto-clear timers for momentary events, keyed by resource_id.
        self._clear_handles: dict[str, asyncio.TimerHandle] = {}

    def _handle_event_by_id(
        self, device_id: str, event_type: ResourceEventType | str | int
    ) -> None:
        # Any new event supersedes a pending momentary auto-clear (e.g. a real
        # sustained Opened/Closed after a pulse should win over the timer).
        self._cancel_clear(device_id)
        super()._handle_event_by_id(device_id, event_type)
        if event_type in self._MOMENTARY_EVENTS:
            self._schedule_clear(device_id, event_type)

    def _cancel_clear(self, device_id: str) -> None:
        device = self._get_device_by_ws_id(device_id)
        if device is None:
            return
        handle = self._clear_handles.pop(device.resource_id, None)
        if handle is not None:
            handle.cancel()

    def _schedule_clear(
        self, device_id: str, event_type: ResourceEventType | str | int
    ) -> None:
        """(Re)arm a timer to restore a sensor to CLOSED after a momentary event.

        The delay depends on the triggering event type (see
        ``_MOMENTARY_CLEAR_DELAY_S``); unknown types fall back to 30s.
        """
        device = self._get_device_by_ws_id(device_id)
        if device is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (e.g. unit test) — leave state as-is
        delay = _MOMENTARY_CLEAR_DELAY_S.get(event_type, 30)
        self._clear_handles[device.resource_id] = loop.call_later(
            delay, self._clear_momentary, device.resource_id
        )

    def _clear_momentary(self, resource_id: str) -> None:
        """Restore a sensor to CLOSED after a momentary event and notify subscribers."""
        self._clear_handles.pop(resource_id, None)
        device = self._get_device_by_ws_id(resource_id)
        if device is None or device.state not in (SensorState.ACTIVE, SensorState.OPEN):
            return
        device.state = SensorState.CLOSED
        self._bridge.event_broker.publish(
            ResourceEventMessage(
                device_id=device.resource_id,
                device_type=self.resource_type,
            )
        )

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

    # NOTE: There is intentionally no per-sensor bypass method. Alarm.com has no
    # ``devices/sensors/{id}/bypass`` endpoint (confirmed against the backend
    # SensorsController — it exposes only read-only bypass status). Bypass is a
    # partition command: use PartitionController.bypass_sensors().
