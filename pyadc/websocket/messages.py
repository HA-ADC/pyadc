"""WebSocket message types and parser for the Alarm.com real-time feed.

The ADC WebSocket stream carries four distinct message shapes:

* :class:`EventWSMessage` — named state-change events (arm, disarm, open,
  close, …) identified by an event-type string.
* :class:`PropertyChangeWSMessage` — numeric property updates (temperature,
  humidity, brightness, …) identified by a property-ID integer.
* :class:`DeviceStatusUpdateWSMessage` — bitmask status updates using
  :class:`~pyadc.const.DeviceStatusFlags`.  **Important:** the community
  library ``pyalarmdotcomajax`` incorrectly skips this message type with the
  comment "ADC webapp doesn't use this".  ADC backend source confirms these
  messages are actively sent for all device state changes; we handle them.
* :class:`MonitorEventWSMessage` — alarm/monitor events.

:class:`WebSocketMessageParser` detects the message type from field presence
and returns the appropriate typed dataclass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pyadc.const import DeviceStatusFlags, ResourceEventType
from pyadc.events import EventBrokerMessage, EventBrokerTopic

log = logging.getLogger(__name__)

UNDEFINED = object()


class WSMessageType(str, Enum):
    EVENT = "EventWSMessage"
    PROPERTY_CHANGE = "PropertyChangeWSMessage"
    DEVICE_STATUS_UPDATE = "DeviceStatusUpdate"  # Bitmask state — NOT skipped
    MONITOR_EVENT = "MonitorEvent"
    SEVERE_WEATHER = "SevereWeatherEvent"
    UNKNOWN = "Unknown"


@dataclass
class BaseWSMessage:
    """Common base for all parsed WebSocket messages.

    Attributes:
        raw: The original unparsed JSON dict, preserved for debugging.
        message_type: Detected :class:`WSMessageType`.
    """

    raw: dict[str, Any] = field(default_factory=dict)
    message_type: WSMessageType = WSMessageType.UNKNOWN


@dataclass
class EventWSMessage(BaseWSMessage):
    """ResourceEventType state change (arm/disarm/open/close etc.)."""

    message_type: WSMessageType = WSMessageType.EVENT
    event_type: ResourceEventType | str = ""
    device_id: str = ""
    unit_id: int = 0
    event_date_utc: str = ""


@dataclass
class PropertyChangeWSMessage(BaseWSMessage):
    """Numeric property change (temperature, humidity, brightness etc.)."""

    message_type: WSMessageType = WSMessageType.PROPERTY_CHANGE
    device_id: str = ""
    unit_id: int = 0
    property_id: int = 0  # DevicePropertyEnum value
    property_value: float = 0.0
    change_date_utc: str = ""


@dataclass
class DeviceStatusUpdateWSMessage(BaseWSMessage):
    """Device status bitmask update carrying :class:`~pyadc.const.DeviceStatusFlags`.

    ADC backend sends this message type for *all* device state changes.  Each
    message provides two complementary integers:

    * ``new_state`` (``NewState``) — the proposed new values for every flag
      bit in the 32-bit status word.
    * ``flag_mask`` (``FlagMask``) — a bitmask indicating which bits have
      changed and are therefore valid in this update.  Bits absent from
      ``flag_mask`` should be ignored so previously known state is not
      overwritten by stale data.

    Usage of :attr:`active_flags`::

        msg.active_flags  # DeviceStatusFlags with only the valid bits set
    """

    message_type: WSMessageType = WSMessageType.DEVICE_STATUS_UPDATE
    device_id: str = ""  # from DeviceId field
    unit_id: int = 0
    new_state: int = 0  # DeviceStatusFlags bitmask
    flag_mask: int = 0  # Which bits are valid in new_state
    event_date_utc: str = ""

    @property
    def active_flags(self) -> DeviceStatusFlags:
        """Return the valid flags from new_state masked by flag_mask."""
        return DeviceStatusFlags(self.new_state & self.flag_mask)


@dataclass
class MonitorEventWSMessage(BaseWSMessage):
    """Alarm/monitor event."""

    message_type: WSMessageType = WSMessageType.MONITOR_EVENT
    device_id: str = ""
    unit_id: int = 0
    event_type: str = ""
    event_value: float = 0.0
    device_type: str = ""


@dataclass(kw_only=True)
class RawResourceEventMessage(EventBrokerMessage):
    """Carries a parsed WS message through the EventBroker."""

    topic: EventBrokerTopic = EventBrokerTopic.RAW_RESOURCE_EVENT
    ws_message: BaseWSMessage


class WebSocketMessageParser:
    """Detects and parses WebSocket message types from raw JSON dicts.

    Detection order (field-presence based):
    1. ``NewState`` + ``FlagMask`` → :class:`DeviceStatusUpdateWSMessage`
    2. ``EventType`` + ``DeviceType`` → :class:`MonitorEventWSMessage`
    3. ``Property`` + ``PropertyValue`` → :class:`PropertyChangeWSMessage`
    4. Fallback → :class:`EventWSMessage`
    """

    @staticmethod
    def parse(raw: dict[str, Any]) -> BaseWSMessage:
        """Parse a raw JSON dict into a typed message object.

        Args:
            raw: Decoded JSON object from the WebSocket text frame.

        Returns:
            A concrete :class:`BaseWSMessage` subclass instance.
        """
        if "NewState" in raw and "FlagMask" in raw:
            return DeviceStatusUpdateWSMessage(
                raw=raw,
                device_id=str(raw.get("DeviceId", "")),
                unit_id=raw.get("UnitId", 0),
                new_state=raw.get("NewState", 0),
                flag_mask=raw.get("FlagMask", 0),
                event_date_utc=raw.get("EventDateUtc", ""),
            )
        if "EventType" in raw and "DeviceType" in raw:
            return MonitorEventWSMessage(
                raw=raw,
                device_id=str(raw.get("DeviceId", "")),
                unit_id=raw.get("UnitId", 0),
                event_type=raw.get("EventType", ""),
                event_value=raw.get("EventValue", 0.0),
                device_type=raw.get("DeviceType", ""),
            )
        if "Property" in raw and "PropertyValue" in raw:
            return PropertyChangeWSMessage(
                raw=raw,
                device_id=str(raw.get("DeviceId", "")),
                unit_id=raw.get("UnitId", 0),
                property_id=raw.get("Property", 0),
                property_value=raw.get("PropertyValue", 0.0),
                change_date_utc=raw.get("ChangeDateUtc", ""),
            )
        # Default: parse as EventWSMessage
        event_type_raw = raw.get("eventType", raw.get("EventType", ""))
        try:
            event_type = ResourceEventType(event_type_raw)
        except (ValueError, KeyError):
            event_type = event_type_raw
        return EventWSMessage(
            raw=raw,
            event_type=event_type,
            device_id=str(raw.get("deviceId", raw.get("DeviceId", ""))),
            unit_id=raw.get("unitId", raw.get("UnitId", 0)),
            event_date_utc=raw.get("eventDateUtc", raw.get("EventDateUtc", "")),
        )
