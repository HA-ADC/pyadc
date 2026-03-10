from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pyadc.const import ResourceEventType, ResourceType
from pyadc.controllers.base import BaseController
from pyadc.models.light import Light, LightState
from pyadc.websocket.messages import PropertyChangeWSMessage

if TYPE_CHECKING:
    from pyadc import AlarmBridge

log = logging.getLogger(__name__)

# property_id 4 = LightColor/brightness level in ADC property change messages
_PROPERTY_LIGHT_LEVEL = 4


class LightController(BaseController):
    resource_type = ResourceType.LIGHT
    model_class = Light
    _event_state_map = {
        ResourceEventType.LIGHT_TURNED_ON: LightState.ON,
        ResourceEventType.LIGHT_TURNED_OFF: LightState.OFF,
        # SwitchLevelChanged is handled separately in _handle_property_change
    }

    async def turn_on(
        self,
        light_id: str,
        brightness: int | None = None,
        rgb: tuple[int, int, int] | None = None,
    ) -> None:
        """Turn a light on, optionally setting brightness (0–100) or RGB color."""
        body: dict[str, Any] = {}
        if brightness is not None:
            body["dimmerLevel"] = max(0, min(100, brightness))
        if rgb is not None:
            r, g, b = rgb
            body["r"] = r
            body["g"] = g
            body["b"] = b
        await self._post(
            f"{self.resource_type}/{light_id}/turnOn",
            body,
        )

    async def turn_off(self, light_id: str) -> None:
        """Turn a light off."""
        await self._post(
            f"{self.resource_type}/{light_id}/turnOff",
            {},
        )

    def _handle_property_change(self, msg: PropertyChangeWSMessage) -> None:
        """Update brightness level on SwitchLevelChanged property messages."""
        device = self._devices.get(msg.device_id)
        if device is None:
            return
        if msg.property_id == _PROPERTY_LIGHT_LEVEL:
            device.brightness = int(msg.property_value)
            device.state = LightState.LEVEL_CHANGE
            from pyadc.events import ResourceEventMessage

            self._bridge.event_broker.publish(
                ResourceEventMessage(
                    device_id=msg.device_id,
                    device_type=self.resource_type,
                )
            )
