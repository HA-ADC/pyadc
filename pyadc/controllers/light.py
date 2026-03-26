from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from pyadc.const import ResourceEventType, ResourceType
from pyadc.controllers.base import BaseController, _validate_device_id
from pyadc.models.light import Light, LightState
from pyadc.websocket.messages import MonitorEventWSMessage, PropertyChangeWSMessage

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
        # SwitchLevelChanged is handled separately in _handle_monitor_event
    }

    async def turn_on(
        self,
        light_id: str,
        brightness: int | None = None,
    ) -> None:
        """Turn a light on, optionally setting brightness (0–100)."""
        _validate_device_id(light_id)
        body: dict[str, Any] = {}
        if brightness is not None:
            body["dimmerLevel"] = max(0, min(100, brightness))
        await self._post(
            f"{self.resource_type}/{light_id}/turnOn",
            body,
        )

    async def set_color(
        self,
        light_id: str,
        hex_color: str,
        color_format: int = 1,
    ) -> None:
        """Set the RGB color of a light via PUT.

        Args:
            light_id: The full resource ID of the light.
            hex_color: Hex color string, e.g. ``"#FF8800"`` or ``"FF8800"``.
            color_format: ADC light color format (1=RGBW, 2=RGB).
        """
        _validate_device_id(light_id)
        if not hex_color.startswith("#"):
            hex_color = f"#{hex_color}"
        device = self._devices.get(light_id)
        description = (device.name or "") if device else ""
        log.debug("set_color: light_id=%s hex=%s format=%s", light_id, hex_color, color_format)
        # Match the full payload format the ADC web app sends for PUT.
        # The server's ValidateDeviceData calls device.Description.Trim() so
        # description must not be null.
        body: dict = {
            "id": light_id,
            "type": "devices/lights",
            "description": description,
            "remoteCommandsEnabled": True,
            "stateTrackingEnabled": True,
            "shouldUpdateMultiLevelState": True,
            "hexColor": hex_color,
            "lightColorFormat": color_format,
        }
        if device:
            body["isDimmer"] = device.supports_dimming
            body["supportsRGBColorControl"] = device.supports_rgb
            body["supportsWhiteLightColorControl"] = device.supports_white_color
            if device.brightness is not None:
                body["lightLevel"] = device.brightness
            # deviceIcon is required — UpdateDeviceDataToDatabase always calls
            # DeviceIcon["icon"] without a null check.
            body["deviceIcon"] = {"icon": device.icon_id if device.icon_id is not None else 0}
        await self._put(
            f"{self.resource_type}/{light_id}",
            body,
        )

    async def turn_off(self, light_id: str) -> None:
        """Turn a light off."""
        _validate_device_id(light_id)
        await self._post(
            f"{self.resource_type}/{light_id}/turnOff",
            {},
        )

    def _handle_monitor_event(self, msg: MonitorEventWSMessage, event_type: ResourceEventType | str | int) -> None:
        """Handle monitor events; extracts brightness from SwitchLevelChanged."""
        if event_type == ResourceEventType.SWITCH_LEVEL_CHANGED:
            device = self._get_device_by_ws_id(msg.device_id)
            if device is not None:
                try:
                    new_level = int(msg.event_value)
                except (ValueError, TypeError):
                    log.debug(
                        "SwitchLevelChanged: non-numeric event_value %r for device %s — skipping",
                        msg.event_value, msg.device_id,
                    )
                    return
                log.debug(
                    "SwitchLevelChanged: device_id=%s new_brightness=%s",
                    msg.device_id, new_level,
                )
                device.brightness = new_level
                device.state = LightState.LEVEL_CHANGE if new_level > 0 else LightState.OFF
                from pyadc.events import ResourceEventMessage
                self._bridge.event_broker.publish(
                    ResourceEventMessage(
                        device_id=device.resource_id,
                        device_type=self.resource_type,
                    )
                )
                # For RGB lights, schedule a color re-fetch — ADC does not push
                # color changes via WebSocket, but a color change in ADC often
                # also fires SwitchLevelChanged, so we piggyback here.
                if device.supports_rgb:
                    asyncio.ensure_future(
                        self._refresh_rgb_color(device.resource_id)
                    )
                return
        super()._handle_monitor_event(msg, event_type)

    async def _refresh_rgb_color(self, resource_id: str) -> None:
        """Re-fetch a single light from the REST API to pick up the latest color."""
        try:
            resp = await self._get(f"{self.resource_type}/{resource_id}")
            item = resp.get("data", {})
            if isinstance(item, list):
                item = item[0] if item else {}
            if not item:
                return
            attrs = item.get("attributes", {})
            hex_color: str | None = attrs.get("hexColor")
            if not hex_color or not isinstance(hex_color, str):
                return
            hex_color = hex_color.lstrip("#")
            if len(hex_color) < 6:
                return
            try:
                new_color = (
                    int(hex_color[0:2], 16),
                    int(hex_color[2:4], 16),
                    int(hex_color[4:6], 16),
                )
            except ValueError:
                return
            device: Light | None = self._devices.get(resource_id)
            if device is None:
                return
            if new_color == device.rgb_color:
                return
            log.debug(
                "Color refresh: device_id=%s old=%s new=%s",
                resource_id, device.rgb_color, new_color,
            )
            device.rgb_color = new_color
            from pyadc.events import ResourceEventMessage
            self._bridge.event_broker.publish(
                ResourceEventMessage(
                    device_id=resource_id,
                    device_type=self.resource_type,
                )
            )
        except Exception as err:
            log.debug("Failed to refresh light color for %s: %s", resource_id, err)

    def _handle_property_change(self, msg: PropertyChangeWSMessage) -> None:
        """Update brightness level on SwitchLevelChanged property messages."""
        device = self._get_device_by_ws_id(msg.device_id)
        if device is None:
            return
        if msg.property_id == _PROPERTY_LIGHT_LEVEL:
            try:
                device.brightness = int(msg.property_value)
            except (ValueError, TypeError):
                log.debug(
                    "LightLevel: non-numeric property_value %r for device %s — skipping",
                    msg.property_value, msg.device_id,
                )
                return
            device.state = LightState.LEVEL_CHANGE
            from pyadc.events import ResourceEventMessage

            self._bridge.event_broker.publish(
                ResourceEventMessage(
                    device_id=device.resource_id,
                    device_type=self.resource_type,
                )
            )
