"""Water meter (Water Dragon ADC-SHM-100-A) model for pyadc.

The Water Dragon is fetched via the standard JSON:API endpoint:
    GET /web/api/devices/waterMeters/{id}

JSON:API type: ``devices/water-meter``

Key attributes from the API (mirrors WaterMeter.cs in CustomerDotNet):
    - ``description``               — device name
    - ``waterUsageToday``           — current day's usage (double, in volumeUnit)
    - ``averageDailyWaterUsage``    — 30-day rolling average (double?, in volumeUnit)
    - ``volumeUnit``                — 0=Gallons, 1=Liters (VolumeUnitsEnum from C#)
    - ``dailyUsageDisplayMinimum``  — gauge min (always 0 for gallons)
    - ``dailyUsageDisplayMaximum``  — gauge max (derived from average by server)
    - ``waterIssues``               — list of active TroubleCondition IDs (leak alerts)
    - ``requiresCalibrationSetup``  — device needs calibration before use
    - ``hasValve``                  — device has an associated shutoff valve
    - ``isMalfunctioning``          — hardware fault flag
    - ``batteryLevelNull``          — battery % or null

Note: The water meter has ``hasState=false`` — no state enum is returned.
Leak state must be inferred from ``waterIssues`` or ``isMalfunctioning``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Self

from pyadc.models.base import AdcDeviceResource, _extract_attrs


VOLUME_UNIT_GALLONS = 0  # VolumeUnitsEnum.Gallon = 0 in C#
VOLUME_UNIT_LITERS = 1   # VolumeUnitsEnum.Liter  = 1 in C#


@dataclass
class WaterMeter(AdcDeviceResource):
    """Alarm.com Water Dragon water meter device."""

    resource_type: ClassVar[str] = "devices/water-meter"

    usage_today: float = 0.0
    """Water usage today in the account's volume unit. Resets to 0 at midnight."""

    average_daily_usage: float | None = None
    """30-day rolling average daily usage in the account's volume unit."""

    volume_unit: int = VOLUME_UNIT_GALLONS
    """0 = Gallons, 1 = Liters (mirrors C# VolumeUnitsEnum)."""

    daily_usage_display_minimum: float = 0.0
    """Gauge display minimum (server-computed; typically 0)."""

    daily_usage_display_maximum: float | None = None
    """Gauge display maximum (server-computed from average; use for gauge card max)."""

    has_active_issues: bool = False
    """True when the device has active water-issue trouble conditions (leak alerts)."""

    requires_calibration_setup: bool = False
    """True when the meter needs calibration before readings are reliable."""

    has_valve: bool = False
    """True when the meter has an associated shutoff valve device."""

    @property
    def is_leaking(self) -> bool:
        return self.has_active_issues

    @property
    def unit_label(self) -> str:
        return "gal" if self.volume_unit == VOLUME_UNIT_GALLONS else "L"

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        resource_id, name, snake_attrs = _extract_attrs(data)

        water_issues = snake_attrs.get("water_issues") or []
        has_issues = bool(water_issues)

        # volume_unit=0 is Gallons; can't use `or` guard since 0 is falsy.
        vol = snake_attrs.get("volume_unit")
        volume_unit = int(vol) if vol is not None else VOLUME_UNIT_GALLONS

        display_max = snake_attrs.get("daily_usage_display_maximum")

        try:
            usage_today = float(snake_attrs.get("water_usage_today") or 0)
        except (ValueError, TypeError):
            usage_today = 0.0

        avg_raw = snake_attrs.get("average_daily_water_usage")
        try:
            average_daily_usage: float | None = float(avg_raw) if avg_raw is not None else None
        except (ValueError, TypeError):
            average_daily_usage = None

        try:
            daily_usage_minimum = float(snake_attrs.get("daily_usage_display_minimum") or 0)
        except (ValueError, TypeError):
            daily_usage_minimum = 0.0

        try:
            daily_usage_maximum: float | None = float(display_max) if display_max is not None else None
        except (ValueError, TypeError):
            daily_usage_maximum = None

        return cls(
            resource_id=resource_id,
            name=name,
            usage_today=usage_today,
            average_daily_usage=average_daily_usage,
            volume_unit=volume_unit,
            daily_usage_display_minimum=daily_usage_minimum,
            daily_usage_display_maximum=daily_usage_maximum,
            has_active_issues=has_issues,
            requires_calibration_setup=bool(
                snake_attrs.get("requires_calibration_setup", False)
            ),
            has_valve=bool(snake_attrs.get("has_valve", False)),
            malfunction=bool(snake_attrs.get("is_malfunctioning", False)),
            battery_level_pct=snake_attrs.get("battery_level_null"),
        )

    @property
    def model_label(self) -> str | None:
        return "Water Meter"
