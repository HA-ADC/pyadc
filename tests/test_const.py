"""Tests for pyadc constants and enums."""
from pyadc.const import (
    ArmingState, DeviceStatusFlags, OtpType, DeviceType, ResourceType,
    ThermostatTemperatureMode, LockState, CoverState, SensorState,
    LightState, ValveState,
)


def test_arming_state_values():
    assert ArmingState.DISARMED == 1
    assert ArmingState.ARMED_STAY == 2
    assert ArmingState.ARMED_AWAY == 3
    assert ArmingState.ARMED_NIGHT == 4


def test_arming_state_is_int_enum():
    assert isinstance(ArmingState.DISARMED, int)
    assert ArmingState(1) is ArmingState.DISARMED


def test_device_status_flags_bitmask():
    flags = DeviceStatusFlags.LOW_BATTERY | DeviceStatusFlags.TAMPER
    assert flags & DeviceStatusFlags.LOW_BATTERY
    assert flags & DeviceStatusFlags.TAMPER
    assert not (flags & DeviceStatusFlags.ALARM)


def test_device_status_flags_critical_bits():
    assert DeviceStatusFlags.LOW_BATTERY == 0x0008
    assert DeviceStatusFlags.CRITICAL_BATTERY == 0x400000
    assert DeviceStatusFlags.COMM_FAILURE == 0x20000
    assert DeviceStatusFlags.DISABLED == 0x200000


def test_device_status_flags_tamper_bits():
    assert DeviceStatusFlags.TAMPER == 0x80000
    assert DeviceStatusFlags.KEYPAD_TAMPER == 0x8000


def test_otp_type_flag():
    combined = OtpType.SMS | OtpType.EMAIL
    assert combined & OtpType.SMS
    assert combined & OtpType.EMAIL
    assert not (combined & OtpType.APP)


def test_otp_type_values():
    assert OtpType.APP == 1
    assert OtpType.SMS == 2
    assert OtpType.EMAIL == 4


def test_resource_type_strings():
    assert ResourceType.PARTITION == "devices/partitions"
    assert ResourceType.SENSOR == "devices/sensors"
    assert ResourceType.GARAGE_DOOR == "devices/garageDoors"
    assert ResourceType.GATE == "devices/gates"  # separate from garage-door!
    assert ResourceType.GATE != ResourceType.GARAGE_DOOR


def test_thermostat_temperature_mode_values():
    assert ThermostatTemperatureMode.OFF == 0
    assert ThermostatTemperatureMode.HEAT == 1
    assert ThermostatTemperatureMode.COOL == 2
    assert ThermostatTemperatureMode.AUTO == 3


def test_lock_state_values():
    assert LockState.LOCKED == 1
    assert LockState.UNLOCKED == 2


def test_cover_state_values():
    assert CoverState.OPEN == 1
    assert CoverState.CLOSED == 2
    assert CoverState.UNKNOWN == 3
    assert CoverState.OPENING == 4
    assert CoverState.CLOSING == 5


def test_sensor_state_open_and_closed():
    assert SensorState.CLOSED == 1
    assert SensorState.OPEN == 2


def test_light_state_values():
    assert LightState.ON == 2
    assert LightState.OFF == 3


def test_valve_state_values():
    assert ValveState.CLOSED == 1
    assert ValveState.OPEN == 2


def test_device_type_partition():
    assert DeviceType.PARTITION == 18


def test_device_status_flags_zero_is_falsy():
    assert not DeviceStatusFlags(0)
