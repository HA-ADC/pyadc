# pyadc

`pyadc` is a standalone async Python library for the [Alarm.com](https://www.alarm.com) API. It has **zero Home Assistant dependencies** and can be used in any Python project.

## Features

- **4-step authentication** with OTP/2FA and device trust support
- **Async REST client** with AFG anti-forgery token handling
- **3-task WebSocket client** (reader / processor / keepalive) for real-time push updates
- **Full `DeviceStatusUpdate` bitmask handling** — fixes a known gap in community libraries
- **JWT expiry detection** — close code 1008 triggers automatic re-auth and reconnect
- **JWT key version rotation** — tries `ver=A` then falls back to `ver=B`

## Supported Device Types

| ADC Device | `DeviceType` | Controller | Model |
|------------|-------------|------------|-------|
| Security partitions | `PARTITION` | `PartitionController` | `Partition` |
| Contact sensors (door/window) | `CONTACT` | `SensorController` | `Sensor` |
| Motion sensors | `MOTION` | `SensorController` | `Sensor` |
| Smoke/heat detectors | `SMOKE_HEAT` | `SensorController` | `Sensor` |
| CO detectors | `CARBON_MONOXIDE` | `SensorController` | `Sensor` |
| Gas sensors | `GAS` (57) | `SensorController` | `Sensor` |
| Glassbreak sensors | `GLASSBREAK` | `SensorController` | `Sensor` |
| Sound sensors | `SOUND` | `SensorController` | `Sensor` |
| Water/leak sensors | `WATER_MULTI_FUNCTION` (44) | `WaterSensorController` | `WaterSensor` |
| Water flood sensors | `WATER_FLOOD` (45) | `WaterSensorController` | `WaterSensor` |
| Locks | `DOOR_LOCK` | `LockController` | `Lock` |
| Lights (on/off, dim, RGB, color temp) | `DIMMER`, `LIGHT`, `RGB_LIGHT` | `LightController` | `Light` |
| On/off switches | `LIGHT_SWITCH_CONTROL` (17) | `LightController` | `Light` |
| Thermostats | `THERMOSTAT` | `ThermostatController` | `Thermostat` |
| Garage doors | `GARAGE_DOOR` | `GarageDoorController` | `GarageDoor` |
| Gates | `GATE` | `GateController` | `Gate` |
| Water valves | `WATER_VALVE` | `ValveController` | `WaterValve` |
| Water meters (ADC-SHM-100-A) | `devices/water-meter` | `WaterMeterController` | `WaterMeter` |
| Image sensors | `IMAGE_SENSOR` | `ImageSensorController` | `ImageSensor` |
| Cameras | `CAMERA` | `CameraController` | `Camera` |

## Installation

```bash
pip install pyadc
```

Development install (includes pytest, aioresponses):

```bash
git clone <repo>
cd HA_pyADC/pyadc
pip install -e ".[dev]"
```

## Basic Usage

```python
import asyncio
import aiohttp
from pyadc import AlarmBridge

async def main():
    async with aiohttp.ClientSession() as session:
        bridge = AlarmBridge(session, "user@example.com", "password")

        # Step 1: authenticate and load all device state via REST
        await bridge.initialize()

        # Step 2: access devices
        for partition in bridge.partitions.devices:
            print(partition.name, partition.state)

        for sensor in bridge.sensors.devices:
            print(sensor.name, sensor.is_open)

        # Step 3: start real-time WebSocket updates
        await bridge.start_websocket()

        # Updates now arrive via EventBroker — no polling needed
        await asyncio.sleep(300)

        await bridge.stop()

asyncio.run(main())
```

## OTP / Two-Factor Authentication

If your account requires MFA, `initialize()` raises `OtpRequired`. Handle it like this:

```python
from pyadc.exceptions import OtpRequired

try:
    await bridge.initialize()
except OtpRequired as exc:
    # exc.otp_types is an OtpType bitmask of available methods
    await bridge.auth.send_otp_sms()       # or send_otp_email()

    code = input("Enter OTP code: ")
    mfa_cookie = await bridge.auth.verify_otp(code)

    # Optionally trust this device to skip OTP on future logins
    await bridge.auth.trust_device()
```

Pass the returned `mfa_cookie` to `AlarmBridge` on future runs to skip 2FA:

```python
bridge = AlarmBridge(session, username, password, mfa_cookie=mfa_cookie)
```

## Real-Time Events

Subscribe to device updates via the `EventBroker`:

```python
from pyadc.events import EventBrokerTopic

# Subscribe to all device updates
def on_any_update(message):
    print("State change:", message.device_id, message.device_type)

bridge.event_broker.subscribe([EventBrokerTopic.RESOURCE_UPDATED], on_any_update)

# Subscribe to a specific device only
bridge.event_broker.subscribe(
    [EventBrokerTopic.RESOURCE_UPDATED],
    on_any_update,
    device_id="your-device-id",
)
```

EventBroker callbacks are synchronous and run within the asyncio event loop.

## Arming / Disarming

```python
partition_id = bridge.partitions.devices[0].resource_id

await bridge.arm_away(partition_id)
await bridge.arm_stay(partition_id)
await bridge.arm_night(partition_id)   # only if partition.supports_night_arming
await bridge.disarm(partition_id)
```

## Controlling Other Devices

```python
# Locks
await bridge.locks.lock(lock_id)
await bridge.locks.unlock(lock_id)

# Lights
await bridge.lights.turn_on(light_id, brightness=128)   # 0-255
await bridge.lights.turn_on(light_id, rgb_color=(255, 0, 0))
await bridge.lights.turn_off(light_id)

# Thermostat
await bridge.thermostats.set_state(
    thermostat_id,
    mode=ThermostatTemperatureMode.COOL,
    cool_setpoint=72.0,
)

# Garage door / gate
await bridge.garage_doors.open(door_id)
await bridge.garage_doors.close(door_id)
await bridge.gates.open(gate_id)

# Water valve
await bridge.water_valves.open(valve_id)
await bridge.water_valves.close(valve_id)
```

## Manually Refreshing State

Re-fetch all device state from the REST API (e.g. after recovering from an outage):

```python
await bridge.refresh_all()
```

---

## Architecture

```
pyadc/
├── __init__.py          # AlarmBridge — main entry point, wires everything together
├── auth.py              # AuthController — 4-step login, OTP, WS token, keep-alive
├── client.py            # AdcClient — aiohttp wrapper, AFG token, error mapping
├── const.py             # All URLs, enums (ArmingState, DeviceStatusFlags, etc.)
├── events.py            # EventBroker — pub/sub for device state changes
├── exceptions.py        # Exception hierarchy rooted at PyadcException
├── models/              # Dataclasses for every device type
│   ├── base.py          # AdcResource, AdcDeviceResource (apply_status_flags)
│   ├── partition.py
│   ├── sensor.py
│   ├── lock.py
│   ├── light.py
│   ├── thermostat.py
│   ├── cover.py         # GarageDoor, Gate
│   ├── valve.py
│   ├── water_sensor.py
│   ├── water_meter.py   # WaterMeter (ADC-SHM-100-A Water Dragon, REST-polled)
│   ├── image_sensor.py
│   ├── camera.py
│   └── system.py
├── controllers/         # Per-device REST + WS event handling
│   ├── base.py          # BaseController with _event_state_map dispatch
│   ├── partition.py     # arm/disarm actions
│   ├── sensor.py        # bypass/unbypass
│   ├── lock.py
│   ├── light.py
│   ├── thermostat.py
│   ├── cover.py
│   ├── valve.py
│   ├── water_sensor.py
│   ├── water_meter.py   # fetch_all only (no WS events)
│   ├── image_sensor.py  # peek_in_now
│   └── system.py
└── websocket/
    ├── client.py        # 3-task WS client (reader/processor/keepalive)
    └── messages.py      # WebSocketMessageParser + typed message dataclasses
```

### Data flow

```
AlarmBridge.initialize()
  └── AuthController.login()          REST: 4-step login
  └── Each controller.fetch_all()     REST: load all devices

AlarmBridge.start_websocket()
  └── WebSocketClient._reader_task    WS: receive frames → queue
  └── WebSocketClient._processor_task queue → WebSocketMessageParser → EventBroker.publish()
  └── WebSocketClient._keepalive_task ping every 60s

EventBroker.publish()
  └── BaseController._handle_raw_event()
        ├── DeviceStatusUpdateWSMessage → apply_status_flags() → RESOURCE_UPDATED
        ├── EventWSMessage → _event_state_map lookup → update device.state → RESOURCE_UPDATED
        └── PropertyChangeWSMessage → _handle_property_change() → RESOURCE_UPDATED
```

---

## Adding a New Device Type

1. **Add constants** to `const.py`:
   - New entry in `ResourceType` (the API path, e.g. `"devices/my-device"`)
   - New state enum if needed
   - New `DeviceType` value if it's a sensor subtype

2. **Add a model** in `models/my_device.py`:
   ```python
   from pyadc.models.base import AdcDeviceResource
   from pyadc.const import ResourceType

   @dataclass
   class MyDevice(AdcDeviceResource):
       resource_type: ClassVar[str] = ResourceType.MY_DEVICE
       state: MyDeviceState = MyDeviceState.UNKNOWN
   ```

3. **Add a controller** in `controllers/my_device.py`:
   ```python
   from pyadc.controllers.base import BaseController
   from pyadc.const import ResourceEventType, ResourceType
   from pyadc.models.my_device import MyDevice

   class MyDeviceController(BaseController):
       resource_type = ResourceType.MY_DEVICE
       model_class = MyDevice
       _event_state_map = {
           ResourceEventType.SomeEvent: MyDeviceState.ACTIVE,
       }

       async def do_action(self, device_id: str) -> None:
           await self._bridge.client.post(f"{self.resource_type}/{device_id}/action", {})
   ```

4. **Wire it in `AlarmBridge`** (`__init__.py`):
   - Add `self.my_devices = MyDeviceController(self)` in `__init__`
   - Add `self.my_devices.fetch_all()` to both `initialize()` and `refresh_all()`

5. **Add tests** in `tests/test_models.py` and `tests/test_websocket_messages.py`.

---

## Running Tests

```bash
cd HA_pyADC/pyadc
pip install -e ".[dev]"
pytest tests/ -v
```

Tests use `unittest.mock` — no real Alarm.com credentials needed.

To run a specific test file:
```bash
pytest tests/test_events.py -v
pytest tests/test_websocket_messages.py -v -k "status_update"
```

---

## License

MIT
