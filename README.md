# pyadc

`pyadc` is a standalone async Python library for the [Alarm.com](https://www.alarm.com) API endpoints, utilizing WebSocket messages for device state. Meant to be used with the [Alarm.com Home Assistant Integration](https://github.com/HA-ADC/alarmdotcom-ha). This is an unofficial Alarm.com library and **should not** be used as a replacement for home security. An Alarm.com subscription is also required to utilize this package.

## Safety Warnings
This integration is intended for casual use with Home Assistant and not as a replacement to keep you safe.

- This integration communicates with Alarm.com over a channel that can be broken or changed at any time.
- It may take several minutes for this integration to receive a status update from Alarm.com's servers.
- Your automations may be buggy.
- This code may be buggy. It's written by volunteers in their free time and testing is spotty.
- You should use Alarm.com's official apps, devices, and services for notifications of all kinds related to safety, break-ins, property damage (e.g.: freeze sensors), etc.

Where possible, use local control for smart home devices that are natively supported by Home Assistant (lights, garage door openers, etc.). Locally controlled devices will continue to work during internet outages whereas this integration will not.

## Features

- **4-step authentication** with OTP/2FA and device trust support
- **Async REST client** with AFG anti-forgery token handling
- **2-task WebSocket client** (reader / processor) for real-time push updates
- **Full `DeviceStatusUpdate` bitmask handling** — fixes a known gap in community libraries
- **Seamless WebSocket token rotation** — the backend force-closes every socket when its JWT expires, so pyadc opens a replacement connection *before* the old one dies (make-before-break) and dedupes the overlapping frames; no events are lost during handovers
- **Coverage-gap recovery** — if a socket dies before its replacement connects, a `RECONNECTED` event triggers a full REST state resync
- **JWT expiry detection** — close code 1008 (failed rotation fallback) triggers automatic re-auth and reconnect
- **JWT key version rotation** — tries `ver=A` then falls back to `ver=B`
- **Camera support** — signed snapshot URLs, WebRTC live streaming via ADC's Janus gateway (optional `webrtc` extra), and momentary person / vehicle / animal / package object-detection events

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

Requires Python 3.12+.

WebRTC live camera streaming needs the optional `webrtc` extra (pulls in `aiortc`; everything else — including snapshots and detection events — works without it):

```bash
pip install "pyadc[webrtc]"
```

Development install (includes pytest, aioresponses, aiortc):

```bash
git clone git@github.com:HA-ADC/pyadc.git
cd pyadc
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

## Cameras

```python
camera = bridge.cameras.devices[0]

# Short-lived signed URL to a JPEG still frame
url = await bridge.cameras.get_snapshot_url(camera)

# WebRTC stream info (Janus gateway URL, token, ICE servers).
# Credentials expire in ~1 hour, so fetch fresh each time.
source = await bridge.cameras.get_live_video_source(camera, hd=True)
```

Live streaming end-to-end (bridging Janus's offerer role to a browser client)
is handled by `pyadc.janus.JanusSession` and requires the `webrtc` extra.

### Object detection

Cameras with video analytics push detection events over the WebSocket
(`VideoCameraTriggered` / `VideoAnalyticsDetection`). The controller decodes
them into four momentary flags on the `Camera` model — `person_detected`,
`vehicle_detected`, `animal_detected`, `package_detected` — each auto-clearing
10 s after the last detection event (repeat detections re-arm the timer).
Flag transitions publish `RESOURCE_UPDATED` on the `EventBroker`, so subscribe
to the camera's `device_id` to react to detections.

## Manually Refreshing State

Re-fetch all device state from the REST API (e.g. after recovering from an outage):

```python
await bridge.refresh_all()
```

---

## Architecture

```
pyadc/
├── __init__.py          # AlarmBridge — main entry point, controller registry
├── auth.py              # AuthController — 4-step login, OTP, WS token, keep-alive
├── client.py            # AdcClient — aiohttp wrapper, AFG token, error mapping
├── const.py             # All URLs, enums (ArmingState, DeviceStatusFlags, etc.)
├── events.py            # EventBroker — pub/sub for device state changes
├── exceptions.py        # Exception hierarchy rooted at PyadcException
├── janus.py             # JanusSession — WebRTC bridge to ADC's Janus gateway (needs `webrtc` extra)
├── models/              # Dataclasses for every device type
│   ├── base.py          # AdcResource, AdcDeviceResource, _parse_enum(), _extract_attrs()
│   ├── auth.py
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
│   ├── base.py          # BaseController — _event_state_map dispatch, _request_with_retry()
│   ├── partition.py     # arm/disarm actions
│   ├── sensor.py        # bypass/unbypass
│   ├── lock.py
│   ├── light.py
│   ├── thermostat.py
│   ├── cover.py
│   ├── valve.py
│   ├── water_sensor.py
│   ├── water_meter.py   # fetch_all only (no WS events)
│   ├── image_sensor.py  # peek_in_now, recent-images cache (panel/PIR cameras)
│   ├── camera.py        # snapshots, live video sources, detection events
│   └── system.py
└── websocket/
    ├── client.py        # 2-task WS client (reader/processor), token rotation
    └── messages.py      # WebSocketMessageParser + typed message dataclasses
```

### Data flow

```
AlarmBridge.initialize()
  └── AuthController.login()          REST: 4-step login
  └── Each controller.fetch_all()     REST: load all devices

AlarmBridge.start_websocket()
  └── WebSocketClient._reader_task    WS: receive frames → queue (reconnects on close)
  └── WebSocketClient._processor_task queue → WebSocketMessageParser → EventBroker.publish()

EventBroker.publish()
  └── BaseController._handle_raw_event()
        ├── DeviceStatusUpdateWSMessage → apply_status_flags() → RESOURCE_UPDATED
        ├── MonitorEventWSMessage → _handle_monitor_event() → RESOURCE_UPDATED
        │     (CameraController overrides this to decode detection events)
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

4. **Register in `AlarmBridge`** (`__init__.py`):
   - Add `("my_devices", MyDeviceController)` to `_CONTROLLER_REGISTRY`
   - Add a type annotation: `my_devices: MyDeviceController`
   - That's it — `__init__` and `_fetch_all_devices()` iterate the registry automatically.

5. **Add tests** in `tests/test_models.py` and `tests/test_websocket_messages.py`.

---

## Releasing a New Version

Follow these steps whenever you want to publish a new `pyadc` version and wire it into `alarmdotcom_ha`.

### 1. Bump the version

Edit `pyadc/pyproject.toml`:
```toml
[project]
version = "X.Y.Z"
```

Use [semver](https://semver.org/):
- **patch** (`X.Y.Z+1`) — bug fixes, no API changes
- **minor** (`X.Y+1.0`) — new device types or backward-compatible API additions
- **major** (`X+1.0.0`) — breaking changes to `AlarmBridge`, models, or controller interfaces

### 2. Run the test suite

```bash
cd pyadc
pytest tests/ -v
```

All tests must pass before tagging.

### 3. Commit and tag

```bash
git add pyadc/pyproject.toml
git commit -m "chore: bump pyadc to vX.Y.Z"
git tag vX.Y.Z
git push origin main --tags
```

The tag **must** be in the form `vX.Y.Z` — `alarmdotcom_ha`'s `manifest.json` references it by tag name.

### 4. Update `alarmdotcom_ha` to use the new release

Edit `alarmdotcom_ha/custom_components/alarmdotcom_ha/manifest.json`:
```json
"requirements": ["pyadc @ git+https://github.com/HA-ADC/pyadc@vX.Y.Z"]
```

Commit that change in the `alarmdotcom_ha` repo:
```bash
git add alarmdotcom_ha/custom_components/alarmdotcom_ha/manifest.json
git commit -m "chore: update pyadc dependency to vX.Y.Z"
git push origin main
```

### 5. Dev environment note

The devcontainer has **no outbound internet access**, so HA cannot install from the git URL. For local development use `deploy.sh` from the workspace root — it locates the running HA devcontainer, copies the pyadc source and the custom component in, and restarts HA:

```bash
./deploy.sh
```

The committed `manifest.json` always uses the full git URL — only strip it in the container.

---

## Running Tests

```bash
cd pyadc
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
