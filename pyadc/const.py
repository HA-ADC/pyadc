"""Constants, enumerations, and URL definitions for the pyadc library.

Enums are grouped by functional area with section comments.  Import
individual names directly; avoid importing ``*`` from this module.
"""

from __future__ import annotations

from enum import IntEnum, IntFlag, StrEnum

# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

URL_BASE = "https://www.alarm.com"
LOGIN_URL = "https://www.alarm.com/login.aspx"
# Login form uses ASP.NET cross-page postback: signInButton.PostBackUrl = "/Default.aspx"
# CustomerDotNet is deployed under /web/, so the actual target is /web/Default.aspx
FORM_SUBMIT_URL = "https://www.alarm.com/web/Default.aspx"
API_URL_BASE = "https://www.alarm.com/web/api/"

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

KEEP_ALIVE_INTERVAL_S = 240       # Server session timeout is 5 min — ping every 4 min for safe margin
KEEP_ALIVE_MAX_INTERVAL_S = 600   # Cap for keep-alive backoff on consecutive failures (10 min)
WS_KEEP_ALIVE_INTERVAL_S = 60     # aiohttp heartbeat interval (server also pings every 15 s)
WS_RECEIVE_TIMEOUT_S = 300        # Max silence before receive() times out — matches JWT lifetime
MAX_RECONNECT_WAIT_S = 30 * 60    # Cap for exponential back-off (30 min)
MAX_CONNECTION_ATTEMPTS = 25      # After this the WS transitions to DEAD
REQUEST_RETRY_LIMIT = 3           # REST request retry limit
KEEP_ALIVE_FAILURE_WARN_LIMIT = 3 # Consecutive HTTP keep-alive failures before WARNING log

# ---------------------------------------------------------------------------
# OTP / 2FA
# ---------------------------------------------------------------------------


class OtpType(IntFlag):
    """Two-factor authentication delivery method bitmask.

    Values are OR-combined in the ``enabledTwoFactorTypes`` API field.
    """

    APP = 1    # Authenticator app TOTP
    SMS = 2    # Text message
    EMAIL = 4  # E-mail


# ---------------------------------------------------------------------------
# WebSocket / Resource event types
# ---------------------------------------------------------------------------


class ResourceEventType(StrEnum):
    """All known ADC resource event type strings."""

    ARMED_AWAY = "ArmedAway"
    ARMED_NIGHT = "ArmedNight"
    ARMED_STAY = "ArmedStay"
    CLOSED = "Closed"
    DISARMED = "Disarmed"
    DOOR_LOCKED = "DoorLocked"
    DOOR_UNLOCKED = "DoorUnlocked"
    IMAGE_SENSOR_UPLOAD = "ImageSensorUpload"
    LIGHT_TURNED_OFF = "LightTurnedOff"
    LIGHT_TURNED_ON = "LightTurnedOn"
    OPENED = "Opened"
    OPENED_CLOSED = "OpenedClosed"
    SWITCH_LEVEL_CHANGED = "SwitchLevelChanged"
    THERMOSTAT_FAN_MODE_CHANGED = "ThermostatFanModeChanged"
    THERMOSTAT_MODE_CHANGED = "ThermostatModeChanged"
    THERMOSTAT_OFFSET = "ThermostatOffset"
    THERMOSTAT_SET_POINT_CHANGED = "ThermostatSetPointChanged"
    DOOR_LEFT_OPEN = "DoorLeftOpen"
    DOOR_LEFT_OPEN_RESTORAL = "DoorLeftOpenRestoral"
    WATER_VALVE_OPENED = "WaterValveOpened"
    WATER_VALVE_CLOSED = "WaterValveClosed"
    GARAGE_DOOR_OPENED = "GarageDoorOpened"
    GARAGE_DOOR_CLOSED = "GarageDoorClosed"
    DOORBELL_RANG = "DoorbellRang"
    MOTION_DETECTED = "MotionDetected"
    SMOKE_DETECTED = "SmokeDetected"
    CARBON_MONOXIDE_DETECTED = "CarbonMonoxideDetected"
    LEAK_DETECTED = "LeakDetected"
    FREEZE_DETECTED = "FreezeDetected"
    TAMPER_DETECTED = "TamperDetected"


# ---------------------------------------------------------------------------
# INT_TO_RESOURCE_EVENT_TYPE — C# EventTypeEnum integer → ResourceEventType
#
# The C# backend serializes MonitorEventWSMessage.EventType as an integer
# (using Newtonsoft.Json default enum serialization, no StringEnumConverter).
# This dict maps authoritative C# EventTypeEnum integer values to their
# Python ResourceEventType equivalents for use in _handle_raw_event.
#
# Source: software/DotNetShared/DotNetStandard/Alarm.Common.Enums/EventTypeEnum.cs
# ---------------------------------------------------------------------------

INT_TO_RESOURCE_EVENT_TYPE: dict[int, "ResourceEventType"] = {
    0: ResourceEventType.CLOSED,           # Closed = 0
    8: ResourceEventType.DISARMED,         # Disarmed = 8
    9: ResourceEventType.ARMED_STAY,       # ArmedStay = 9
    10: ResourceEventType.ARMED_AWAY,      # ArmedAway = 10
    15: ResourceEventType.OPENED,          # Opened = 15
    90: ResourceEventType.DOOR_UNLOCKED,   # DoorUnlocked = 90
    91: ResourceEventType.DOOR_LOCKED,     # DoorLocked = 91
    100: ResourceEventType.OPENED_CLOSED,  # OpenedClosed = 100
    113: ResourceEventType.ARMED_NIGHT,    # ArmedNight = 113
    94: ResourceEventType.THERMOSTAT_SET_POINT_CHANGED,  # ThermostatSetPointChanged = 94
    95: ResourceEventType.THERMOSTAT_MODE_CHANGED,       # ThermostatModeChanged = 95
    105: ResourceEventType.THERMOSTAT_OFFSET,            # ThermostatOffset = 105
    120: ResourceEventType.THERMOSTAT_FAN_MODE_CHANGED,  # ThermostatFanModeChanged = 120
    315: ResourceEventType.LIGHT_TURNED_ON,    # LightTurnedOn = 315
    316: ResourceEventType.LIGHT_TURNED_OFF,   # LightTurnedOff = 316
    317: ResourceEventType.SWITCH_LEVEL_CHANGED,  # SwitchLevelChanged = 317
}


# ---------------------------------------------------------------------------
# DeviceStatusFlags — bitmask from §4.2 DeviceStatusUpdate WebSocket messages
# ---------------------------------------------------------------------------


class DeviceStatusFlags(IntFlag):
    """Bitmask flags carried in DeviceStatusUpdate WebSocket messages.

    Note: OPEN and ARMED_STAY share bit 0x0001; their meaning is determined
    by device type context.  Similarly LEAK and SYSTEM_ALARM share 0x0800.
    """

    OPEN = 0x0001
    ARMED_STAY = 0x0001
    ARMED_AWAY = 0x0002
    ALARM = 0x0004
    LOW_BATTERY = 0x0008
    AC_POWER = 0x0010
    MALFUNCTION = 0x0020
    BYPASSED = 0x0040
    DURESS = 0x0080
    PHONE_LINE = 0x0100
    FIRE_PANIC = 0x0200
    AUX_PANIC = 0x0400
    LEAK = 0x0800
    SYSTEM_ALARM = 0x0800
    RADIO = 0x1000
    FREEZE_ALARM = 0x2000
    NO_ACTIVITY = 0x4000
    KEYPAD_TAMPER = 0x8000
    MODULE_MALFUNCTION = 0x10000
    COMM_FAILURE = 0x20000
    TAMPER = 0x80000
    DISABLED = 0x200000
    CRITICAL_BATTERY = 0x400000


# ---------------------------------------------------------------------------
# Partition / Arming
# ---------------------------------------------------------------------------


class ArmingState(IntEnum):
    """Arming state of a security partition (``ArmingStateEnum`` on the ADC API).

    HIDDEN (5) is returned by the API for partitions that are not visible in the
    current user's view; treat it as unknown/unavailable in UI.
    """

    DISARMED = 1
    ARMED_STAY = 2
    ARMED_AWAY = 3
    ARMED_NIGHT = 4
    HIDDEN = 5


# ---------------------------------------------------------------------------
# Generic device status
# ---------------------------------------------------------------------------


class DeviceStatus(IntEnum):
    """Discrete device status values (DeviceStatusEnum, selected)."""

    CLOSED = 2
    OPEN = 3
    DISARMED = 4
    ARMED_STAY = 5
    ARMED_AWAY = 6
    ARMED_NIGHT = 29
    LOW_BATTERY = 11
    MALFUNCTION = 13
    BYPASSED = 14
    CLOSING = 34
    OPENING = 35
    LEAK_DETECTED = 33
    TAMPER = 27
    CRITICAL_BATTERY = 31
    DISABLED = 30
    POSITION_UNKNOWN = 39


# ---------------------------------------------------------------------------
# Thermostat
# ---------------------------------------------------------------------------


class ThermostatTemperatureMode(IntEnum):
    """Thermostat temperature / HVAC mode (``ThermostatTemperatureModeEnum``).

    ENERGY_SAVE_HEAT (11) and ENERGY_SAVE_COOL (12) are ADC-specific "eco"
    modes that map to HEAT and COOL respectively in Home Assistant because
    HA's climate platform has no direct equivalent eco-heat/eco-cool concept.
    AUX_HEAT (4) is emergency/backup heat and also maps to HEAT in HA.
    """

    OFF = 0
    HEAT = 1
    COOL = 2
    AUTO = 3
    AUX_HEAT = 4
    ENERGY_SAVE_HEAT = 11
    ENERGY_SAVE_COOL = 12


class ThermostatStatus(IntEnum):
    """Web API ``ThermostatStatusEnum`` — the value sent in ``desiredState`` for setState.

    Distinct from ``ThermostatTemperatureMode`` (which is what the device/WS uses).
    The API maps: Unknown=0, Off=1, Heat=2, Cool=3, Auto=4, AuxHeat=5.
    """

    UNKNOWN = 0
    OFF = 1
    HEAT = 2
    COOL = 3
    AUTO = 4
    AUX_HEAT = 5


class ThermostatOperatingState(IntEnum):
    """Actual running state of the thermostat — maps to HA hvac_action."""

    OFF = 0x00
    HEATING = 0x01
    COOLING = 0x02
    FAN = 0x03
    PENDING_HEAT = 0x04
    PENDING_COOL = 0x05
    AUX_HEAT = 0x07
    SECOND_STAGE_HEAT = 0x08
    SECOND_STAGE_COOL = 0x09
    WAITING = 0xFD
    ERROR = 0xFE
    UNKNOWN = 0xFF


class ThermostatFanMode(IntEnum):
    """Fan mode setting (ThermostatFanModeEnum)."""

    AUTO_LOW = 0
    ON_LOW = 1
    AUTO_HIGH = 2
    ON_HIGH = 3
    AUTO_MEDIUM = 4
    ON_MEDIUM = 5
    CIRCULATE = 6
    HUMIDITY = 7


class ThermostatSetpointType(IntEnum):
    """Setpoint type / preset mode (ThermostatSetpointTypeEnum)."""

    FIXED = 0
    AWAY = 1
    HOME = 2
    SLEEP = 3


# ---------------------------------------------------------------------------
# Peripheral device states
# ---------------------------------------------------------------------------


class LightState(IntEnum):
    """State of a light device."""

    OFFLINE = 0
    NO_STATE = 1
    ON = 2
    OFF = 3
    LEVEL_CHANGE = 4


class LockState(IntEnum):
    """State of a lock device."""

    LOCKED = 1
    UNLOCKED = 2
    HIDDEN = 3
    UNKNOWN = 4


class CoverState(IntEnum):
    """State of a garage door / gate / cover device."""

    OPEN = 1
    CLOSED = 2
    UNKNOWN = 3
    OPENING = 4
    CLOSING = 5


class ValveState(IntEnum):
    """State of a water valve device.

    Values match C# WaterValveStatusEnum: Unknown=0, Closed=1, Open=2.
    """

    UNKNOWN = 0
    CLOSED = 1
    OPEN = 2


class SensorState(IntEnum):
    """State of a generic sensor device."""

    CLOSED = 1
    OPEN = 2
    IDLE = 3
    ACTIVE = 4
    DRY = 5
    WET = 6
    FULL = 7
    LOW = 8
    OPENED_CLOSED = 9
    ISSUE = 10
    OK = 11
    UNKNOWN = 12


# ---------------------------------------------------------------------------
# Device type catalog
# ---------------------------------------------------------------------------


class DeviceType(IntEnum):
    """All known ADC device type values (DeviceTypeEnum)."""

    CONTACT = 1
    MOTION = 2
    SOUND = 3
    BREAKAGE = 4
    SMOKE_HEAT = 5
    CARBON_MONOXIDE = 6
    RADON = 7
    TEMPERATURE = 8
    PANIC_BUTTON = 9
    CAMERA = 11
    LIGHT_LEGACY = 12
    SIREN_LEGACY = 14
    WATER = 16
    LIGHT_SWITCH_CONTROL = 17
    PARTITION = 18
    ZWAVE_THERMOSTAT = 20
    ZWAVE_LOCK = 21
    PUSH_BUTTON = 26
    ZWAVE_LIGHT = 28
    ZWAVE_SIREN = 29
    IMAGE_SENSOR = 30
    POINT_SAFE = 31
    POWER_METER = 32
    REMOTE_PANIC = 34
    GARAGE_DOOR = 36
    DOORBELL = 37
    WATER_VALVE = 40
    TEMPERATURE_SENSOR = 41
    TRACKER = 42
    WATER_MULTI_FUNCTION = 44
    WATER_FLOOD = 45
    CONTACT_MULTI_FUNCTION = 52
    IQ_SMOKE_MULTI_FUNCTION = 53
    GAS = 57
    ACCESS_CARD_READER = 73
    CLIMAX_PIR_CAMERA = 66
    DSC_PIR_CAMERA = 67
    QOLSYS_PANEL_CAMERA = 68
    GLASSBREAK = 19
    IQ_PANEL_GLASSBREAK = 83
    IQ_PANEL_MOTION = 89
    HONEYWELL_PANEL_CAMERA = 91
    POWERG_PIR_CAMERA = 113
    GC_NEXT_PANEL_CAMERA = 126


# ---------------------------------------------------------------------------
# REST API resource type path segments
# ---------------------------------------------------------------------------


class ResourceType:
    """REST API path segments matching actual RoutePrefix values in CustomerDotNet.

    Each value is the path after ``/web/api/`` as declared in the C# controller
    ``[RoutePrefix]`` attribute (e.g. ``api/devices/partitions`` → ``devices/partitions``).
    """

    PARTITION = "devices/partitions"
    SENSOR = "devices/sensors"
    LOCK = "devices/locks"
    LIGHT = "devices/lights"
    THERMOSTAT = "devices/thermostats"
    GARAGE_DOOR = "devices/garageDoors"
    GATE = "devices/gates"
    WATER_VALVE = "devices/waterValves"
    WATER_SENSOR = "devices/waterSensors"
    IMAGE_SENSOR = "video/smrfImages"  # SMRF (Smart Radio Motion Frame) image sensors
    CAMERA = "video/devices/cameras"
    SYSTEM = "systems/systems"
    TROUBLE_CONDITION = "troubleConditions/troubleConditions"
