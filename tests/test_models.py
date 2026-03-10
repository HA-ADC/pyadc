"""Tests for pyadc model dataclasses."""
import pytest
from pyadc.const import (
    ArmingState, DeviceStatusFlags, SensorState, LockState, LightState,
    CoverState, ThermostatTemperatureMode, ValveState, DeviceType, ResourceType,
)
from pyadc.models.base import AdcDeviceResource
from pyadc.models.partition import Partition
from pyadc.models.sensor import Sensor
from pyadc.models.light import Light
from pyadc.models.cover import GarageDoor, Gate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_api_item(resource_type, resource_id, description, **attrs):
    """Build a minimal JSON:API resource dict.

    Note: from_json_api uses the 'description' attribute key for the device
    name (not 'name'), so we put the human-readable label under 'description'.
    """
    return {
        "id": resource_id,
        "type": resource_type,
        "attributes": {"description": description, **attrs},
    }


# ---------------------------------------------------------------------------
# Partition
# ---------------------------------------------------------------------------

def test_partition_from_json_api():
    item = make_api_item("devices/partition", "p-1", "Main", state=1)
    p = Partition.from_json_api(item)
    assert p.resource_id == "p-1"
    assert p.name == "Main"
    assert p.state == ArmingState.DISARMED


def test_partition_from_json_api_armed_away():
    item = make_api_item("devices/partition", "p-2", "Away", state=3)
    p = Partition.from_json_api(item)
    assert p.state == ArmingState.ARMED_AWAY


def test_partition_from_json_api_invalid_state_defaults_to_disarmed():
    item = make_api_item("devices/partition", "p-3", "Bad", state=999)
    p = Partition.from_json_api(item)
    assert p.state == ArmingState.DISARMED


def test_partition_resource_type():
    assert Partition.resource_type == ResourceType.PARTITION


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------

def test_sensor_is_open_when_open():
    s = Sensor(resource_id="s-1", name="Door")
    s.state = SensorState.OPEN
    assert s.is_open is True


def test_sensor_is_open_when_active():
    s = Sensor(resource_id="s-1", name="Motion")
    s.state = SensorState.ACTIVE
    assert s.is_open is True


def test_sensor_is_open_when_wet():
    s = Sensor(resource_id="s-1", name="Water")
    s.state = SensorState.WET
    assert s.is_open is True


def test_sensor_is_open_when_issue():
    s = Sensor(resource_id="s-1", name="Issue")
    s.state = SensorState.ISSUE
    assert s.is_open is True


def test_sensor_is_not_open_when_closed():
    s = Sensor(resource_id="s-1", name="Door")
    s.state = SensorState.CLOSED
    assert s.is_open is False


def test_sensor_is_not_open_when_dry():
    s = Sensor(resource_id="s-1", name="Water")
    s.state = SensorState.DRY
    assert s.is_open is False


def test_sensor_from_json_api():
    item = make_api_item(
        "devices/sensor", "s-10", "Front Door",
        state=2,  # OPEN
        deviceType=1,  # CONTACT
    )
    s = Sensor.from_json_api(item)
    assert s.resource_id == "s-10"
    assert s.name == "Front Door"
    assert s.state == SensorState.OPEN
    assert s.is_open is True


# ---------------------------------------------------------------------------
# Light
# ---------------------------------------------------------------------------

def test_light_brightness_pct_full():
    light = Light(resource_id="l-1", name="Lamp", brightness=99)
    assert light.brightness_pct == 255


def test_light_brightness_pct_zero():
    light = Light(resource_id="l-1", name="Lamp", brightness=0)
    assert light.brightness_pct == 0


def test_light_brightness_pct_half():
    light = Light(resource_id="l-1", name="Lamp", brightness=50)
    expected = round(50 / 99 * 255)
    assert light.brightness_pct == expected


def test_light_brightness_pct_none_when_brightness_is_none():
    light = Light(resource_id="l-1", name="Lamp", brightness=None)
    assert light.brightness_pct is None


# ---------------------------------------------------------------------------
# AdcDeviceResource.apply_status_flags
# ---------------------------------------------------------------------------

def test_apply_status_flags_low_battery_and_malfunction():
    d = Partition(resource_id="p-1", name="Test")
    new_state = int(DeviceStatusFlags.LOW_BATTERY | DeviceStatusFlags.MALFUNCTION)
    flag_mask = 0xFFFFFF
    d.apply_status_flags(new_state, flag_mask)
    assert d.low_battery is True
    assert d.malfunction is True
    assert d.critical_battery is False
    assert d.tamper is False


def test_apply_status_flags_mask_limits_bits():
    d = Partition(resource_id="p-1", name="Test")
    new_state = int(DeviceStatusFlags.LOW_BATTERY | DeviceStatusFlags.TAMPER)
    # Mask only includes LOW_BATTERY, so TAMPER should NOT be set
    flag_mask = int(DeviceStatusFlags.LOW_BATTERY)
    d.apply_status_flags(new_state, flag_mask)
    assert d.low_battery is True
    assert d.tamper is False


def test_apply_status_flags_comm_failure():
    d = Partition(resource_id="p-1", name="Test")
    d.apply_status_flags(int(DeviceStatusFlags.COMM_FAILURE), 0xFFFFFF)
    assert d.comm_failure is True
    assert d.low_battery is False


def test_apply_status_flags_disabled():
    d = Partition(resource_id="p-1", name="Test")
    d.apply_status_flags(int(DeviceStatusFlags.DISABLED), 0xFFFFFF)
    assert d.is_disabled is True


def test_apply_status_flags_clears_previously_set_flags():
    d = Partition(resource_id="p-1", name="Test")
    d.apply_status_flags(int(DeviceStatusFlags.LOW_BATTERY), 0xFFFFFF)
    assert d.low_battery is True
    # Apply again with no flags set — low_battery should clear
    d.apply_status_flags(0, 0xFFFFFF)
    assert d.low_battery is False


def test_apply_status_flags_keypad_tamper_sets_tamper():
    d = Partition(resource_id="p-1", name="Test")
    d.apply_status_flags(int(DeviceStatusFlags.KEYPAD_TAMPER), 0xFFFFFF)
    assert d.tamper is True


# ---------------------------------------------------------------------------
# GarageDoor / Gate
# ---------------------------------------------------------------------------

def test_garage_door_from_json_api():
    item = make_api_item("devices/garage-door", "gd-1", "Garage", state=2)
    gd = GarageDoor.from_json_api(item)
    assert gd.resource_id == "gd-1"
    assert gd.name == "Garage"
    assert gd.state == CoverState.CLOSED


def test_garage_door_open_state():
    item = make_api_item("devices/garage-door", "gd-2", "Side Garage", state=1)
    gd = GarageDoor.from_json_api(item)
    assert gd.state == CoverState.OPEN


def test_gate_resource_type():
    assert Gate.resource_type == ResourceType.GATE
    assert GarageDoor.resource_type == ResourceType.GARAGE_DOOR
    assert Gate.resource_type != GarageDoor.resource_type


def test_gate_from_json_api():
    item = make_api_item("devices/gate", "gt-1", "Front Gate", state=1)
    gt = Gate.from_json_api(item)
    assert gt.resource_id == "gt-1"
    assert gt.state == CoverState.OPEN
