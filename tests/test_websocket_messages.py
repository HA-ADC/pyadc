"""Tests for WebSocketMessageParser and all WS message types."""
import pytest
from pyadc.websocket.messages import (
    WebSocketMessageParser, WSMessageType, DeviceStatusUpdateWSMessage,
    EventWSMessage, PropertyChangeWSMessage, MonitorEventWSMessage,
    BaseWSMessage,
)
from pyadc.const import DeviceStatusFlags


# ---------------------------------------------------------------------------
# DeviceStatusUpdateWSMessage
# ---------------------------------------------------------------------------

def test_parse_device_status_update():
    raw = {
        "DeviceId": "42",
        "UnitId": 1,
        "NewState": int(DeviceStatusFlags.LOW_BATTERY | DeviceStatusFlags.MALFUNCTION),
        "FlagMask": 0xFFFFFF,
        "EventDateUtc": "2025-01-01T00:00:00Z",
    }
    msg = WebSocketMessageParser.parse(raw)
    assert isinstance(msg, DeviceStatusUpdateWSMessage)
    assert msg.device_id == "42"
    assert msg.unit_id == 1
    assert msg.new_state & int(DeviceStatusFlags.LOW_BATTERY)
    assert msg.flag_mask == 0xFFFFFF


def test_device_status_update_active_flags():
    raw = {
        "DeviceId": "1",
        "UnitId": 0,
        "NewState": int(DeviceStatusFlags.LOW_BATTERY | DeviceStatusFlags.TAMPER),
        "FlagMask": int(DeviceStatusFlags.LOW_BATTERY),  # mask hides TAMPER
    }
    msg = WebSocketMessageParser.parse(raw)
    assert isinstance(msg, DeviceStatusUpdateWSMessage)
    assert DeviceStatusFlags.LOW_BATTERY in msg.active_flags
    assert DeviceStatusFlags.TAMPER not in msg.active_flags


def test_device_status_update_not_skipped():
    """Critical regression test: DeviceStatusUpdate must NOT be ignored."""
    raw = {"DeviceId": "1", "UnitId": 0, "NewState": 8, "FlagMask": 0xFF}
    msg = WebSocketMessageParser.parse(raw)
    assert isinstance(msg, DeviceStatusUpdateWSMessage), (
        "DeviceStatusUpdate must be parsed, not skipped!"
    )
    assert msg.message_type == WSMessageType.DEVICE_STATUS_UPDATE


def test_device_status_update_message_type():
    raw = {"DeviceId": "5", "UnitId": 0, "NewState": 0, "FlagMask": 0xFF}
    msg = WebSocketMessageParser.parse(raw)
    assert msg.message_type == WSMessageType.DEVICE_STATUS_UPDATE


def test_device_status_active_flags_zero_mask():
    raw = {"DeviceId": "3", "UnitId": 0, "NewState": 0xFF, "FlagMask": 0}
    msg = WebSocketMessageParser.parse(raw)
    assert isinstance(msg, DeviceStatusUpdateWSMessage)
    # With mask=0, all active_flags should be 0
    assert int(msg.active_flags) == 0


# ---------------------------------------------------------------------------
# MonitorEventWSMessage
# ---------------------------------------------------------------------------

def test_parse_monitor_event():
    raw = {
        "DeviceId": "55",
        "UnitId": 1,
        "EventType": "Alarm",
        "EventValue": 1.0,
        "DeviceType": "partition",
    }
    msg = WebSocketMessageParser.parse(raw)
    assert isinstance(msg, MonitorEventWSMessage)
    assert msg.device_id == "55"
    assert msg.event_type == "Alarm"
    assert msg.device_type == "partition"
    assert msg.event_value == 1.0


def test_parse_monitor_event_message_type():
    raw = {
        "DeviceId": "10",
        "UnitId": 0,
        "EventType": "Sensor",
        "EventValue": 0.0,
        "DeviceType": "sensor",
    }
    msg = WebSocketMessageParser.parse(raw)
    assert msg.message_type == WSMessageType.MONITOR_EVENT


# ---------------------------------------------------------------------------
# PropertyChangeWSMessage
# ---------------------------------------------------------------------------

def test_parse_property_change():
    raw = {
        "DeviceId": "99",
        "UnitId": 2,
        "Property": 1,
        "PropertyValue": 72.5,
        "ChangeDateUtc": "2025-01-01T00:00:00Z",
    }
    msg = WebSocketMessageParser.parse(raw)
    assert isinstance(msg, PropertyChangeWSMessage)
    assert msg.device_id == "99"
    assert msg.property_value == 72.5
    assert msg.property_id == 1
    assert msg.unit_id == 2


def test_parse_property_change_message_type():
    raw = {"DeviceId": "7", "UnitId": 0, "Property": 3, "PropertyValue": 68.0}
    msg = WebSocketMessageParser.parse(raw)
    assert msg.message_type == WSMessageType.PROPERTY_CHANGE


# ---------------------------------------------------------------------------
# EventWSMessage (fallback)
# ---------------------------------------------------------------------------

def test_parse_event_message_fallback():
    raw = {
        "deviceId": "77",
        "unitId": 3,
        "eventType": "ArmedAway",
        "eventDateUtc": "2025-01-01",
    }
    msg = WebSocketMessageParser.parse(raw)
    assert isinstance(msg, EventWSMessage)
    assert msg.device_id == "77"
    assert msg.unit_id == 3


def test_parse_event_message_known_event_type():
    from pyadc.const import ResourceEventType
    raw = {
        "deviceId": "88",
        "unitId": 0,
        "eventType": "Disarmed",
        "eventDateUtc": "2025-01-01",
    }
    msg = WebSocketMessageParser.parse(raw)
    assert isinstance(msg, EventWSMessage)
    assert msg.event_type == ResourceEventType.DISARMED


def test_parse_event_message_unknown_event_type_stored_as_string():
    raw = {
        "deviceId": "99",
        "unitId": 0,
        "eventType": "SomeFutureEvent",
        "eventDateUtc": "2025-01-01",
    }
    msg = WebSocketMessageParser.parse(raw)
    assert isinstance(msg, EventWSMessage)
    assert msg.event_type == "SomeFutureEvent"


def test_parse_event_message_type_field():
    raw = {"deviceId": "1", "unitId": 0, "eventType": "Disarmed"}
    msg = WebSocketMessageParser.parse(raw)
    assert msg.message_type == WSMessageType.EVENT


# ---------------------------------------------------------------------------
# Parser priority: DeviceStatusUpdate > MonitorEvent > PropertyChange > Event
# ---------------------------------------------------------------------------

def test_device_status_update_takes_priority_over_event():
    """NewState+FlagMask keys trigger DeviceStatusUpdate, not EventWSMessage."""
    raw = {
        "DeviceId": "5",
        "UnitId": 0,
        "NewState": 0,
        "FlagMask": 0xFF,
        "eventType": "Disarmed",  # would match EventWSMessage fallback
    }
    msg = WebSocketMessageParser.parse(raw)
    assert isinstance(msg, DeviceStatusUpdateWSMessage)
