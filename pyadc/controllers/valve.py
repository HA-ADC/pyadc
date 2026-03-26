from __future__ import annotations

from typing import TYPE_CHECKING

from pyadc.const import ResourceEventType, ResourceType, ValveState
from pyadc.controllers.base import BaseController, _validate_device_id
from pyadc.events import ResourceEventMessage
from pyadc.models.valve import WaterValve

if TYPE_CHECKING:
    from pyadc import AlarmBridge


class ValveController(BaseController):
    """Controller for water valve devices."""

    resource_type = ResourceType.WATER_VALVE
    model_class = WaterValve
    # ADC sends generic "Opened"/"Closed" events (not "WaterValveOpened") for valves.
    _event_state_map = {
        ResourceEventType.WATER_VALVE_OPENED: ValveState.OPEN,
        ResourceEventType.WATER_VALVE_CLOSED: ValveState.CLOSED,
        ResourceEventType.OPENED: ValveState.OPEN,
        ResourceEventType.CLOSED: ValveState.CLOSED,
    }

    def _handle_event_by_id(self, device_id: str, event_type) -> None:
        """Clear transitioning_to before delegating to base state update."""
        device: WaterValve | None = self._get_device_by_ws_id(device_id)
        if device is not None:
            device.transitioning_to = None
        super()._handle_event_by_id(device_id, event_type)

    async def open(self, valve_id: str) -> None:
        """Open a water valve (optimistic transitioning_to update then POST)."""
        _validate_device_id(valve_id)
        device: WaterValve | None = self._devices.get(valve_id)
        if device is not None:
            device.transitioning_to = ValveState.OPEN
            self._bridge.event_broker.publish(
                ResourceEventMessage(
                    device_id=device.resource_id,
                    device_type=self.resource_type,
                )
            )
        await self._post(f"{self.resource_type}/{valve_id}/open", {})

    async def close(self, valve_id: str) -> None:
        """Close a water valve (optimistic transitioning_to update then POST)."""
        _validate_device_id(valve_id)
        device: WaterValve | None = self._devices.get(valve_id)
        if device is not None:
            device.transitioning_to = ValveState.CLOSED
            self._bridge.event_broker.publish(
                ResourceEventMessage(
                    device_id=device.resource_id,
                    device_type=self.resource_type,
                )
            )
        await self._post(f"{self.resource_type}/{valve_id}/close", {})
