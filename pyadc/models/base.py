"""Base dataclasses for pyadc device models.

All device models inherit from :class:`AdcDeviceResource`, which in turn
inherits from :class:`AdcResource`.  The base classes handle JSON:API
deserialization (camelCase → snake_case) and the common bitmask flag update
applied by DeviceStatusUpdate WebSocket messages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Self


def _camel_to_snake(name: str) -> str:
    """Convert camelCase string to snake_case."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


@dataclass
class AdcResource:
    """Base class for all ADC JSON:API resources.

    Attributes:
        resource_id: The JSON:API ``id`` string (e.g.
            ``"1234567890123456789-0"``)
        name: Human-readable device name extracted from the ``description``
            attribute (falls back to ``name`` if ``description`` is absent).
        resource_type: Class-level API path segment (e.g.
            ``"devices/partition"``).  Set by each concrete model class.
    """

    resource_id: str
    name: str
    resource_type: ClassVar[str] = ""

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        """Parse from a JSON:API resource object.

        Args:
            data: A single JSON:API resource object with ``id``, ``type``, and
                ``attributes`` keys.

        Returns:
            A populated instance of the concrete subclass.
        """
        attrs = data.get("attributes", {})
        snake_attrs = {_camel_to_snake(k): v for k, v in attrs.items()}
        resource_id = data.get("id", "")
        name = snake_attrs.get("description", snake_attrs.get("name", ""))
        return cls(resource_id=resource_id, name=name, **{
            k: v for k, v in snake_attrs.items()
            if k not in ("description", "name") and k in cls.__dataclass_fields__
        })


@dataclass
class AdcDeviceResource(AdcResource):
    """Base class for manageable ADC devices.

    Extends :class:`AdcResource` with the health/status flag attributes that
    are common to every controllable device and updated via
    :meth:`apply_status_flags` from ``DeviceStatusUpdate`` WebSocket messages.

    Attributes:
        malfunction: Device is reporting a general malfunction.
        low_battery: Battery is below normal operating threshold.
        critical_battery: Battery level is critically low.
        tamper: Physical tamper or keypad tamper detected.
        comm_failure: Communication failure between device and panel.
        is_disabled: Device has been administratively disabled.
        bypassed: Sensor is currently bypassed in the partition.
        battery_level_pct: Battery percentage (0–100), or ``None`` if not
            reported by this device type.
    """

    malfunction: bool = False
    low_battery: bool = False
    critical_battery: bool = False
    tamper: bool = False
    comm_failure: bool = False
    is_disabled: bool = False
    bypassed: bool = False
    battery_level_pct: int | None = None

    def apply_status_flags(self, new_state: int, flag_mask: int) -> None:
        """Update device health flags from a ``DeviceStatusUpdate`` bitmask.

        The ``DeviceStatusUpdate`` WebSocket message carries two integers:
        ``NewState`` (the new flag values) and ``FlagMask`` (which bits are
        actually meaningful for this event).  Only bits present in
        ``flag_mask`` are valid; the rest should be ignored to avoid
        clobbering previously known state with stale bits.

        Args:
            new_state: Raw ``NewState`` integer from the WebSocket message —
                the proposed new values for all flag bits.
            flag_mask: Raw ``FlagMask`` integer — a bitmask indicating which
                bits in ``new_state`` contain valid (updated) information.
                Bits absent from the mask retain their current value.
        """
        from pyadc.const import DeviceStatusFlags

        masked = new_state & flag_mask
        self.malfunction = bool(masked & DeviceStatusFlags.MALFUNCTION)
        self.low_battery = bool(masked & DeviceStatusFlags.LOW_BATTERY)
        self.critical_battery = bool(masked & DeviceStatusFlags.CRITICAL_BATTERY)
        self.tamper = bool(masked & DeviceStatusFlags.TAMPER) or bool(
            masked & DeviceStatusFlags.KEYPAD_TAMPER
        )
        self.comm_failure = bool(masked & DeviceStatusFlags.COMM_FAILURE)
        self.is_disabled = bool(masked & DeviceStatusFlags.DISABLED)
        self.bypassed = bool(masked & DeviceStatusFlags.BYPASSED)
