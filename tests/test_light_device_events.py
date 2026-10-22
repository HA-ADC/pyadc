"""Tests for momentary light events (double-tap / Central Scene) on DEVICE_EVENT.

State events (on/off/level) must keep updating state on RESOURCE_UPDATED and
must never leak onto DEVICE_EVENT; everything else a light reports is
published on DEVICE_EVENT so consumers can trigger on it.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pyadc.const import LightState, ResourceEventType
from pyadc.controllers.light import LightController, decode_button_event
from pyadc.events import (
    DeviceEventMessage,
    EventBroker,
    EventBrokerTopic,
    ResourceEventMessage,
)
from pyadc.models.light import Light
from pyadc.websocket.messages import RawResourceEventMessage, WebSocketMessageParser

RESOURCE_ID = "104878280-1240"
SHORT_ID = "1240"


def make_controller():
    bridge = MagicMock()
    bridge.event_broker = EventBroker()
    controller = LightController(bridge)
    device = Light(resource_id=RESOURCE_ID, name="Basement Stairs", state=LightState.OFF)
    controller._devices[RESOURCE_ID] = device
    controller._devices_by_short_id[SHORT_ID] = device
    events: list[DeviceEventMessage] = []
    updates: list[ResourceEventMessage] = []
    bridge.event_broker.subscribe([EventBrokerTopic.DEVICE_EVENT], events.append)
    bridge.event_broker.subscribe([EventBrokerTopic.RESOURCE_UPDATED], updates.append)
    return controller, device, events, updates


def feed(controller, raw: dict) -> None:
    controller._handle_raw_event(
        RawResourceEventMessage(ws_message=WebSocketMessageParser.parse(raw))
    )


def monitor_frame(event_type, value=0.0, device_id=SHORT_ID) -> dict:
    return {
        "UnitId": 1,
        "DeviceId": device_id,
        "EventType": event_type,
        "EventValue": value,
        "DeviceType": 17,
    }


def test_unknown_monitor_event_published_raw():
    controller, device, events, updates = make_controller()
    feed(controller, monitor_frame(4242, 2.0))
    assert len(events) == 1
    ev = events[0]
    assert ev.topic is EventBrokerTopic.DEVICE_EVENT
    assert ev.device_id == RESOURCE_ID
    assert ev.event_type == 4242
    assert ev.event_value == 2.0
    assert ev.action is None and ev.button is None
    assert ev.raw["EventType"] == 4242
    assert updates == []  # no state change
    assert device.state == LightState.OFF


# Real frames captured from the ADC websocket (Front Porch Light, double-tap up/down).
CAPTURED_UP = {"EventDateUtc": "2026-10-22T18:36:20.707Z", "UnitId": 104878280, "DeviceId": 1240,
               "EventType": 163, "EventValue": 30001.0, "CorrelatedId": 1240,
               "QstringForExtraData": None, "DeviceType": -1}
CAPTURED_DOWN = {**CAPTURED_UP, "EventValue": 30002.0}


@pytest.mark.parametrize(("frame", "button"), [(CAPTURED_UP, 1), (CAPTURED_DOWN, 2)])
def test_double_tap_decoded(frame, button):
    controller, device, events, updates = make_controller()
    feed(controller, frame)
    assert len(events) == 1
    ev = events[0]
    assert ev.device_id == RESOURCE_ID
    assert ev.event_type == "ButtonPressed"
    assert (ev.action, ev.button, ev.key_attribute) == ("KeyPressed2x", button, 3)
    assert updates == []
    assert device.state == LightState.OFF


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.0, ("KeyPressed", 1, 0)),       # live: single tap up
        (2.0, ("KeyPressed", 2, 0)),       # live: single tap down
        (10001.0, ("KeyReleased", 1, 1)),
        (20002.0, ("KeyHeldDown", 2, 2)),
        (40002.0, ("KeyPressed3x", 2, 4)),
        (60001.0, ("KeyPressed5x", 1, 6)),
    ],
)
def test_key_attributes_decoded(value, expected):
    controller, _, events, _ = make_controller()
    feed(controller, monitor_frame(163, value))
    assert (events[0].action, events[0].button, events[0].key_attribute) == expected


def test_unknown_key_attribute_passes_through_undecoded():
    controller, _, events, _ = make_controller()
    feed(controller, monitor_frame(163, 90002.0))
    assert (events[0].action, events[0].button, events[0].key_attribute) == (None, 2, 9)


@pytest.mark.parametrize("value", [30000.0, None, "junk", -5.0])
def test_malformed_button_value(value):
    assert decode_button_event("ButtonPressed", value) == (None, None, None)


def test_decode_accepts_raw_int_type():
    assert decode_button_event(163, 30002) == ("KeyPressed2x", 2, 3)


def test_state_events_not_device_events():
    controller, device, events, updates = make_controller()
    feed(controller, monitor_frame(315))  # LightTurnedOn
    feed(controller, monitor_frame(317, 40.0))  # SwitchLevelChanged
    assert events == []
    assert len(updates) == 2
    assert device.brightness == 40


def test_event_ws_message_unknown_type_published():
    controller, _, events, updates = make_controller()
    feed(controller, {"eventType": "SomethingNew", "deviceId": SHORT_ID})
    assert len(events) == 1
    assert events[0].event_type == "SomethingNew"
    assert events[0].event_value is None
    assert updates == []


def test_event_ws_message_state_type_still_updates():
    controller, device, events, updates = make_controller()
    feed(controller, {"eventType": "LightTurnedOn", "deviceId": SHORT_ID})
    assert events == []
    assert device.state == LightState.ON
    assert len(updates) == 1


def test_unknown_device_ignored():
    controller, _, events, _ = make_controller()
    feed(controller, monitor_frame(4242, device_id="9999"))
    assert events == []


def test_device_level_subscription_receives_device_event():
    controller, _, _, _ = make_controller()
    mine: list = []
    controller._bridge.event_broker.subscribe(
        [EventBrokerTopic.DEVICE_EVENT], mine.append, device_id=RESOURCE_ID
    )
    feed(controller, monitor_frame(4242))
    assert len(mine) == 1


def test_decode_button_event_unknown():
    assert decode_button_event(123456, 30001.0) == (None, None, None)


def test_resource_event_type_value_is_plain_string(monkeypatch):
    # A ResourceEventType that isn't a light state event must be published as
    # its plain string value so consumers can compare/serialise it.
    controller, _, events, _ = make_controller()
    controller._publish_device_event(SHORT_ID, ResourceEventType.DOORBELL_RANG, None, {})
    assert events[0].event_type == "DoorbellRang"
    assert type(events[0].event_type) is str
