"""Thermostat (climate) model for pyadc."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from pyadc.const import (
    ResourceType,
    ThermostatFanMode,
    ThermostatOperatingState,
    ThermostatSetpointType,
    ThermostatTemperatureMode,
)
from pyadc.models.base import AdcDeviceResource, _camel_to_snake


@dataclass
class Thermostat(AdcDeviceResource):
    """Alarm.com Z-Wave thermostat (climate) resource.

    Attributes:
        state: Current HVAC mode (:class:`~pyadc.const.ThermostatTemperatureMode`).
        operating_state: What the system is actively doing right now
            (:class:`~pyadc.const.ThermostatOperatingState`).  Maps to HA's
            ``hvac_action``.
        fan_mode: Current fan setting (:class:`~pyadc.const.ThermostatFanMode`).
        current_temperature: Measured ambient temperature (in the unit given
            by ``temperature_unit``).
        target_temperature_heat: Heat setpoint.
        target_temperature_cool: Cool setpoint.
        current_humidity: Measured relative humidity (%), or ``None``.
        target_humidity: Desired humidity setpoint, or ``None``.
        setpoint_type: Active preset schedule
            (:class:`~pyadc.const.ThermostatSetpointType`), or ``None``.
        supports_fan_only: Device supports a fan-only mode.
        supports_humidity_control: Device has a humidity control feature.
        temperature_unit: ``"F"`` (Fahrenheit) or ``"C"`` (Celsius).
    """

    resource_type: ClassVar[str] = ResourceType.THERMOSTAT
    state: ThermostatTemperatureMode = ThermostatTemperatureMode.OFF
    operating_state: ThermostatOperatingState | None = None
    fan_mode: ThermostatFanMode = ThermostatFanMode.AUTO_LOW
    current_temperature: float | None = None
    target_temperature_heat: float | None = None
    target_temperature_cool: float | None = None
    current_humidity: float | None = None
    target_humidity: float | None = None
    setpoint_type: ThermostatSetpointType | None = None
    supports_fan_only: bool = False
    supports_humidity_control: bool = False
    temperature_unit: str = "F"  # "F" or "C"
    supported_temperature_modes: list[ThermostatTemperatureMode] = field(default_factory=lambda: [
        ThermostatTemperatureMode.OFF,
        ThermostatTemperatureMode.HEAT,
        ThermostatTemperatureMode.COOL,
        ThermostatTemperatureMode.AUTO,
    ])

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        """Parse from JSON:API resource object."""
        attrs = data.get("attributes", {})
        snake_attrs = {_camel_to_snake(k): v for k, v in attrs.items()}

        raw_state = snake_attrs.get("state")
        # inferredState is what ADC actually displays (e.g. AUTO=3 even when
        # the raw Z-Wave state reports AUX_HEAT=4). Fall back to state if absent.
        raw_inferred = snake_attrs.get("inferred_state")
        raw_mode = raw_inferred if raw_inferred is not None else raw_state
        try:
            state = ThermostatTemperatureMode(raw_mode) if raw_mode is not None else ThermostatTemperatureMode.OFF
        except ValueError:
            state = ThermostatTemperatureMode.OFF

        # Build supported modes list from explicit boolean flags in the API response.
        # Fall back to a sensible default set if none of the flags are present.
        _mode_flag_map = [
            ("supports_off_mode", ThermostatTemperatureMode.OFF),
            ("supports_heat_mode", ThermostatTemperatureMode.HEAT),
            ("supports_cool_mode", ThermostatTemperatureMode.COOL),
            ("supports_auto_mode", ThermostatTemperatureMode.AUTO),
            ("supports_aux_heat_mode", ThermostatTemperatureMode.AUX_HEAT),
        ]
        supported_modes = [mode for flag, mode in _mode_flag_map if snake_attrs.get(flag, False)]
        if not supported_modes:
            supported_modes = [
                ThermostatTemperatureMode.OFF,
                ThermostatTemperatureMode.HEAT,
                ThermostatTemperatureMode.COOL,
                ThermostatTemperatureMode.AUTO,
            ]

        raw_fan = snake_attrs.get("fan_mode")
        try:
            fan_mode = ThermostatFanMode(raw_fan) if raw_fan is not None else ThermostatFanMode.AUTO_LOW
        except ValueError:
            fan_mode = ThermostatFanMode.AUTO_LOW

        raw_op = snake_attrs.get("operating_state")
        try:
            operating_state = ThermostatOperatingState(raw_op) if raw_op is not None else None
        except ValueError:
            operating_state = None

        raw_setpoint = snake_attrs.get("setpoint_type")
        try:
            setpoint_type = ThermostatSetpointType(raw_setpoint) if raw_setpoint is not None else None
        except ValueError:
            setpoint_type = None

        uses_celsius = snake_attrs.get("uses_celsius", False)
        # Fallback: detect Celsius from ambient temp or setpoint range if flag absent.
        # Indoor Celsius values (15-35) vs Fahrenheit (50-100) never overlap.
        if not uses_celsius:
            ambient = snake_attrs.get("ambient_temp") or snake_attrs.get("forwarding_ambient_temp")
            heat_sp = snake_attrs.get("heat_setpoint")
            if ambient is not None and float(ambient) < 50:
                uses_celsius = True
            elif heat_sp is not None and float(heat_sp) < 50:
                uses_celsius = True

        return cls(
            resource_id=data.get("id", ""),
            name=snake_attrs.get("description", ""),
            state=state,
            operating_state=operating_state,
            fan_mode=fan_mode,
            current_temperature=snake_attrs.get("ambient_temp") or snake_attrs.get("forwarding_ambient_temp"),
            target_temperature_heat=snake_attrs.get("heat_setpoint"),
            target_temperature_cool=snake_attrs.get("cool_setpoint"),
            current_humidity=snake_attrs.get("humidity_level"),
            setpoint_type=setpoint_type,
            supports_fan_only=snake_attrs.get("supports_fan_mode", False),
            supports_humidity_control=snake_attrs.get("supports_humidity", False),
            temperature_unit="C" if uses_celsius else "F",
            supported_temperature_modes=supported_modes,
            battery_level_pct=snake_attrs.get("battery_level_null"),
        )
