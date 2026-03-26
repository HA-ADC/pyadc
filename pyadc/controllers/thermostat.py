"""Thermostat controller for pyadc.

Manages :class:`~pyadc.models.thermostat.Thermostat` devices.  Unlike most
controllers, thermostat mode/fan/setpoint changes don't map cleanly to a
single enum value, so ``_event_state_map`` maps to ``None`` — the
``RESOURCE_UPDATED`` event is still published (triggering entity refresh) but
state is updated via :class:`~pyadc.websocket.messages.PropertyChangeWSMessage`
messages handled in :meth:`ThermostatController._handle_property_change`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pyadc.const import ResourceEventType, ResourceType, ThermostatFanMode, ThermostatStatus, ThermostatTemperatureMode
from pyadc.controllers.base import BaseController, _validate_device_id
from pyadc.models.thermostat import Thermostat
from pyadc.websocket.messages import MonitorEventWSMessage, PropertyChangeWSMessage

if TYPE_CHECKING:
    from pyadc import AlarmBridge

log = logging.getLogger(__name__)

# ADC property_id values for thermostat
_PROPERTY_AMBIENT_TEMP = 1
_PROPERTY_HEAT_SETPOINT = 2
_PROPERTY_COOL_SETPOINT = 3


class ThermostatController(BaseController):
    """Controller for Alarm.com Z-Wave thermostats.

    Temperature and setpoint updates arrive via
    :class:`~pyadc.websocket.messages.PropertyChangeWSMessage` (property IDs
    1 = ambient temp, 2 = heat setpoint, 3 = cool setpoint) which are handled
    by :meth:`_handle_property_change`.  Mode/fan/preset changes use named
    events (mapped to ``None`` in ``_event_state_map``) and trigger a full
    entity refresh.
    """

    resource_type = ResourceType.THERMOSTAT
    model_class = Thermostat
    _event_state_map = {
        ResourceEventType.THERMOSTAT_MODE_CHANGED: None,
        ResourceEventType.THERMOSTAT_FAN_MODE_CHANGED: None,
        ResourceEventType.THERMOSTAT_SET_POINT_CHANGED: None,
        ResourceEventType.THERMOSTAT_OFFSET: None,
    }

    async def set_state(
        self,
        thermostat_id: str,
        mode: ThermostatTemperatureMode | None = None,
        fan_mode: ThermostatFanMode | None = None,
        heat_setpoint: float | None = None,
        cool_setpoint: float | None = None,
    ) -> None:
        """Update one or more thermostat settings in a single API call.

        All parameters are optional; only those that are not ``None`` are
        included in the request body.

        Args:
            thermostat_id: Resource ID of the thermostat to update.
            mode: New HVAC mode
                (:class:`~pyadc.const.ThermostatTemperatureMode`).
            fan_mode: New fan mode
                (:class:`~pyadc.const.ThermostatFanMode`).
            heat_setpoint: Heat setpoint temperature.  The ADC API expects
                values in tenths of a degree Fahrenheit (e.g. ``720`` = 72 °F).
            cool_setpoint: Cool setpoint temperature (same unit convention).
        """
        # The web API uses ThermostatStatusEnum for desiredState, which has different
        # integer values from ThermostatTemperatureMode (the device/WS enum).
        _mode_to_status: dict[ThermostatTemperatureMode, ThermostatStatus] = {
            ThermostatTemperatureMode.OFF: ThermostatStatus.OFF,
            ThermostatTemperatureMode.HEAT: ThermostatStatus.HEAT,
            ThermostatTemperatureMode.COOL: ThermostatStatus.COOL,
            ThermostatTemperatureMode.AUTO: ThermostatStatus.AUTO,
            ThermostatTemperatureMode.AUX_HEAT: ThermostatStatus.AUX_HEAT,
            ThermostatTemperatureMode.ENERGY_SAVE_HEAT: ThermostatStatus.HEAT,
            ThermostatTemperatureMode.ENERGY_SAVE_COOL: ThermostatStatus.COOL,
        }
        body: dict[str, Any] = {}
        if mode is not None:
            body["desiredState"] = _mode_to_status.get(mode, ThermostatStatus.OFF)
        if fan_mode is not None:
            body["desiredFanMode"] = fan_mode
        if heat_setpoint is not None:
            body["desiredHeatSetpoint"] = heat_setpoint
        if cool_setpoint is not None:
            body["desiredCoolSetpoint"] = cool_setpoint

        log.debug("Thermostat setState: %s", body)
        _validate_device_id(thermostat_id)
        await self._post(
            f"{self.resource_type}/{thermostat_id}/setState",
            body,
        )

    def _handle_property_change(self, msg: PropertyChangeWSMessage) -> None:
        """Update temperature and setpoint values from PropertyChange messages.

        All temperature properties arrive in 0.01°F units (e.g. 7300 = 73.00°F).
        Setpoints are converted to the device's configured unit before storing.
        """
        log.debug("Thermostat PropertyChange: device_id=%s property_id=%s value=%s", msg.device_id, msg.property_id, msg.property_value)
        device = self._get_device_by_ws_id(msg.device_id)
        if device is None:
            log.debug("Thermostat PropertyChange: no device for id=%s (known: %s)", msg.device_id, list(self._devices_by_short_id.keys()))
            return

        def _f100_to_unit(value_100ths_f: float) -> float:
            """Convert 0.01°F units to the device's temperature unit."""
            temp_f = value_100ths_f / 100.0
            if device.temperature_unit == "C":
                return round((temp_f - 32.0) * 5.0 / 9.0, 1)
            return round(temp_f, 1)

        changed = True
        if msg.property_id == _PROPERTY_AMBIENT_TEMP:
            device.current_temperature = _f100_to_unit(msg.property_value)
        elif msg.property_id == _PROPERTY_HEAT_SETPOINT:
            device.target_temperature_heat = _f100_to_unit(msg.property_value)
        elif msg.property_id == _PROPERTY_COOL_SETPOINT:
            device.target_temperature_cool = _f100_to_unit(msg.property_value)
        else:
            changed = False

        if changed:
            from pyadc.events import ResourceEventMessage

            self._bridge.event_broker.publish(
                ResourceEventMessage(
                    device_id=device.resource_id,
                    device_type=self.resource_type,
                )
            )

    def _handle_monitor_event(self, msg: MonitorEventWSMessage, event_type: ResourceEventType | str | int) -> None:
        """Handle thermostat mode/fan change events by reading event_value directly.

        The C# backend embeds the new mode integer in EventValue:
        - ThermostatModeChanged (95): event_value = ThermostatTemperatureMode int
        - ThermostatFanModeChanged (120): event_value = ThermostatFanMode int
        Set these on the model before publishing so HA sees the new state immediately.
        """
        device = self._get_device_by_ws_id(msg.device_id)
        if device is None:
            return

        if event_type == ResourceEventType.THERMOSTAT_MODE_CHANGED:
            try:
                device.state = ThermostatTemperatureMode(int(msg.event_value))
                log.debug("Thermostat mode changed: %s → %s", device.name, device.state)
            except (ValueError, KeyError):
                log.debug("Unknown thermostat mode value: %s", msg.event_value)
        elif event_type == ResourceEventType.THERMOSTAT_FAN_MODE_CHANGED:
            try:
                device.fan_mode = ThermostatFanMode(int(msg.event_value))
                log.debug("Thermostat fan mode changed: %s → %s", device.name, device.fan_mode)
            except (ValueError, KeyError):
                log.debug("Unknown thermostat fan mode value: %s", msg.event_value)

        from pyadc.events import ResourceEventMessage
        self._bridge.event_broker.publish(
            ResourceEventMessage(
                device_id=device.resource_id,
                device_type=self.resource_type,
            )
        )

    def _handle_event(self, msg: Any) -> None:
        """Re-fetch thermostat state on mode-change events."""
        device = self._get_device_by_ws_id(msg.device_id)
        if device is None:
            return
        # Thermostat mode/fan/setpoint changes don't map to a simple state enum;
        # downstream consumers should re-fetch or rely on PropertyChange messages.
        from pyadc.events import ResourceEventMessage

        self._bridge.event_broker.publish(
            ResourceEventMessage(
                device_id=device.resource_id,
                device_type=self.resource_type,
            )
        )
