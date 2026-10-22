"""Thermostat (climate) model for pyadc."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from pyadc.const import (
    ResourceType,
    ThermostatFanMode,
    ThermostatOperatingState,
    ThermostatSetpointType,
    ThermostatStatus,
    ThermostatTemperatureMode,
)
from pyadc.models.base import AdcDeviceResource, _parse_enum, _extract_attrs


# The REST ``state`` / ``desiredState`` attributes use the web API's
# ThermostatStatusEnum (Unknown=0, Off=1, Heat=2, Cool=3, Auto=4, AuxHeat=5),
# which is offset from the device/WebSocket ThermostatTemperatureModeEnum.
_STATUS_TO_MODE: dict[ThermostatStatus, ThermostatTemperatureMode] = {
    ThermostatStatus.OFF: ThermostatTemperatureMode.OFF,
    ThermostatStatus.HEAT: ThermostatTemperatureMode.HEAT,
    ThermostatStatus.COOL: ThermostatTemperatureMode.COOL,
    ThermostatStatus.AUTO: ThermostatTemperatureMode.AUTO,
    ThermostatStatus.AUX_HEAT: ThermostatTemperatureMode.AUX_HEAT,
}


def _parse_mode(raw_state: Any, raw_inferred: Any) -> ThermostatTemperatureMode:
    """Return the configured HVAC mode from a REST thermostat resource.

    ``state`` is the configured mode as a ThermostatStatusEnum value; e.g. a
    thermostat in Auto reports ``state=4`` (verified against the ADC web UI).
    ``inferredState`` is *not* the mode: it tracks the direction ADC infers the
    thermostat is working in, so it reads Cool for a thermostat in Auto that is
    cooling. It is only used as a fallback when ``state`` is missing or Unknown.
    """
    try:
        mode = _STATUS_TO_MODE.get(ThermostatStatus(int(raw_state)))
    except (TypeError, ValueError):
        mode = None
    if mode is not None:
        return mode
    try:
        return ThermostatTemperatureMode(int(raw_inferred))
    except (TypeError, ValueError):
        return ThermostatTemperatureMode.OFF


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
        resource_id, name, snake_attrs = _extract_attrs(data)

        state = _parse_mode(snake_attrs.get("state"), snake_attrs.get("inferred_state"))

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

        fan_mode = _parse_enum(snake_attrs, "fan_mode", ThermostatFanMode, ThermostatFanMode.AUTO_LOW)
        operating_state = _parse_enum(snake_attrs, "operating_state", ThermostatOperatingState, None)
        setpoint_type = _parse_enum(snake_attrs, "setpoint_type", ThermostatSetpointType, None)

        uses_celsius = snake_attrs.get("uses_celsius", False)
        # Fallback: detect Celsius from ambient temp or setpoint range if flag absent.
        # Indoor Celsius values (15-35) vs Fahrenheit (50-100) never overlap.
        if not uses_celsius:
            ambient = snake_attrs.get("ambient_temp") or snake_attrs.get("forwarding_ambient_temp")
            heat_sp = snake_attrs.get("heat_setpoint")
            try:
                if ambient is not None and float(ambient) < 50:
                    uses_celsius = True
                elif heat_sp is not None and float(heat_sp) < 50:
                    uses_celsius = True
            except (ValueError, TypeError):
                pass

        return cls(
            resource_id=resource_id,
            name=name,
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

    @property
    def model_label(self) -> str | None:
        return "Z-Wave Thermostat"
