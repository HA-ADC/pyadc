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

from pyadc.const import ResourceEventType, ResourceType, ThermostatFanMode, ThermostatTemperatureMode
from pyadc.controllers.base import BaseController
from pyadc.models.thermostat import Thermostat
from pyadc.websocket.messages import PropertyChangeWSMessage

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
        body: dict[str, Any] = {}
        if mode is not None:
            body["desiredState"] = mode
        if fan_mode is not None:
            body["desiredFanMode"] = fan_mode
        if heat_setpoint is not None:
            body["desiredHeatSetpoint"] = heat_setpoint
        if cool_setpoint is not None:
            body["desiredCoolSetpoint"] = cool_setpoint

        await self._post(
            f"{self.resource_type}/{thermostat_id}/setState",
            body,
        )

    def _handle_property_change(self, msg: PropertyChangeWSMessage) -> None:
        """Update temperature and setpoint values from PropertyChange messages."""
        device = self._devices.get(msg.device_id)
        if device is None:
            return

        changed = True
        if msg.property_id == _PROPERTY_AMBIENT_TEMP:
            device.temperature = msg.property_value
        elif msg.property_id == _PROPERTY_HEAT_SETPOINT:
            device.heat_setpoint = msg.property_value
        elif msg.property_id == _PROPERTY_COOL_SETPOINT:
            device.cool_setpoint = msg.property_value
        else:
            changed = False

        if changed:
            from pyadc.events import ResourceEventMessage

            self._bridge.event_broker.publish(
                ResourceEventMessage(
                    device_id=msg.device_id,
                    device_type=self.resource_type,
                )
            )

    def _handle_event(self, msg: Any) -> None:
        """Re-fetch thermostat state on mode-change events."""
        device = self._devices.get(msg.device_id)
        if device is None:
            return
        # Thermostat mode/fan/setpoint changes don't map to a simple state enum;
        # downstream consumers should re-fetch or rely on PropertyChange messages.
        from pyadc.events import ResourceEventMessage

        self._bridge.event_broker.publish(
            ResourceEventMessage(
                device_id=msg.device_id,
                device_type=self.resource_type,
            )
        )
