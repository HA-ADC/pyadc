"""pyadc — async Python client for the Alarm.com API.

This module exposes :class:`AlarmBridge`, the top-level facade that owns all
device controllers, the REST client, the auth flow, and the WebSocket client.
Import from here rather than from internal sub-modules.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from pyadc.auth import AuthController
from pyadc.client import AdcClient
from pyadc.const import (
    ArmingState,
    CoverState,
    LightState,
    LockState,
    ThermostatFanMode,
    ThermostatSetpointType,
    ThermostatTemperatureMode,
    ValveState,
)
from pyadc.controllers.camera import CameraController
from pyadc.controllers.cover import GarageDoorController, GateController
from pyadc.controllers.image_sensor import ImageSensorController
from pyadc.controllers.light import LightController
from pyadc.controllers.lock import LockController
from pyadc.controllers.partition import PartitionController
from pyadc.controllers.sensor import SensorController
from pyadc.controllers.system import SystemController
from pyadc.controllers.thermostat import ThermostatController
from pyadc.controllers.valve import ValveController
from pyadc.controllers.water_meter import WaterMeterController
from pyadc.controllers.water_sensor import WaterSensorController
from pyadc.events import EventBroker
from pyadc.exceptions import NotInitialized
from pyadc.websocket.client import WebSocketClient

log = logging.getLogger(__name__)


class AlarmBridge:
    """Main entry point for the pyadc library.

    Owns all device controllers, the HTTP client, the auth controller, and
    the WebSocket client.  Call :meth:`initialize` once to authenticate and
    populate all device lists, then :meth:`start_websocket` for real-time
    updates.

    Usage::

        async with aiohttp.ClientSession() as session:
            bridge = AlarmBridge(session, "user@example.com", "password")
            await bridge.initialize()
            for partition in bridge.partitions.devices:
                print(partition.name, partition.state)
            await bridge.start_websocket()
            # Real-time state changes flow through bridge.event_broker.
            # ...
            await bridge.stop()

    Attributes:
        event_broker: Central pub/sub bus.  Subscribe here to receive device
            state change notifications and connection events.
        client: Low-level REST client (rarely needed directly).
        auth: Authentication controller.
        websocket: WebSocket client.
        cameras: :class:`~pyadc.controllers.camera.CameraController`
        partitions: :class:`~pyadc.controllers.partition.PartitionController`
        sensors: :class:`~pyadc.controllers.sensor.SensorController`
        locks: :class:`~pyadc.controllers.lock.LockController`
        lights: :class:`~pyadc.controllers.light.LightController`
        thermostats: :class:`~pyadc.controllers.thermostat.ThermostatController`
        garage_doors: :class:`~pyadc.controllers.cover.GarageDoorController`
        gates: :class:`~pyadc.controllers.cover.GateController`
        water_valves: :class:`~pyadc.controllers.valve.ValveController`
        water_sensors: :class:`~pyadc.controllers.water_sensor.WaterSensorController`
        water_meters: :class:`~pyadc.controllers.water_meter.WaterMeterController`
        image_sensors: :class:`~pyadc.controllers.image_sensor.ImageSensorController`
        systems: :class:`~pyadc.controllers.system.SystemController`
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        *,
        mfa_cookie: str = "",
        two_factor_cookie: str = "",
        base_url: str = "https://www.alarm.com",
    ) -> None:
        """Create an AlarmBridge instance.

        Args:
            session: Shared :class:`aiohttp.ClientSession` used for all HTTP
                and WebSocket requests.  The caller is responsible for closing
                it after :meth:`stop` returns.
            username: Alarm.com account e-mail address.
            password: Alarm.com account password.
            mfa_cookie: Pre-obtained two-factor auth cookie that skips the OTP
                challenge.  Obtain it by calling :meth:`auth.verify_otp` and
                storing the returned value across sessions.
            two_factor_cookie: Alias for *mfa_cookie* (backward compat).
            base_url: Root URL of the Alarm.com deployment.  Defaults to the
                production endpoint.  Pass an alternative URL (e.g. a staging
                server) via HA's advanced config to target a different backend.
        """
        self._session = session
        self._initialized = False

        # Core infrastructure
        self.event_broker = EventBroker()
        self.client = AdcClient(session, base_url=base_url)
        self.auth = AuthController(
            client=self.client,
            session=session,
            username=username,
            password=password,
            mfa_cookie=mfa_cookie or two_factor_cookie,
            base_url=base_url,
        )
        self.websocket = WebSocketClient(self)

        # Device controllers
        self.cameras = CameraController(self)
        self.partitions = PartitionController(self)
        self.sensors = SensorController(self)
        self.locks = LockController(self)
        self.lights = LightController(self)
        self.thermostats = ThermostatController(self)
        self.garage_doors = GarageDoorController(self)
        self.gates = GateController(self)
        self.water_valves = ValveController(self)
        self.water_sensors = WaterSensorController(self)
        self.water_meters = WaterMeterController(self)
        self.image_sensors = ImageSensorController(self)
        self.systems = SystemController(self)

    @property
    def initialized(self) -> bool:
        """Return ``True`` after :meth:`initialize` has completed successfully."""
        return self._initialized

    async def initialize(self) -> None:
        """Authenticate and fetch all device state via REST.

        Steps:
        1. :meth:`auth.login` — full 4-step login (may raise
           :exc:`~pyadc.exceptions.OtpRequired` if MFA is needed).
        2. Parallel REST fetch for every device type.
        3. :meth:`auth.start_keep_alive` — starts the 5-minute ping loop.

        Raises:
            OtpRequired: If the account requires a two-factor code.  Call
                :meth:`auth.send_otp_sms` or :meth:`auth.send_otp_email`,
                then :meth:`auth.verify_otp`, then retry :meth:`initialize`.
            AuthenticationFailed: On invalid credentials or locked account.
        """
        await self.auth.login()

        # Fetch all device types in parallel
        await asyncio.gather(
            self.systems.fetch_all(),
            self.cameras.fetch_all(),
            self.partitions.fetch_all(),
            self.sensors.fetch_all(),
            self.locks.fetch_all(),
            self.lights.fetch_all(),
            self.thermostats.fetch_all(),
            self.garage_doors.fetch_all(),
            self.gates.fetch_all(),
            self.water_valves.fetch_all(),
            self.water_sensors.fetch_all(),
            self.water_meters.fetch_all(),
            self.image_sensors.fetch_all(),
            return_exceptions=True,
        )

        await self.auth.start_keep_alive()
        self._initialized = True
        log.info("AlarmBridge initialized successfully")

    async def start_websocket(self) -> None:
        """Start the WebSocket client for real-time device updates.

        Raises:
            NotInitialized: If :meth:`initialize` has not been called yet.
        """
        if not self._initialized:
            raise NotInitialized("Call initialize() first")
        await self.websocket.start()

    async def stop(self) -> None:
        """Stop the WebSocket client and the session keep-alive task."""
        await self.websocket.stop()
        await self.auth.stop_keep_alive()

    async def refresh_all(self) -> None:
        """Re-fetch all device state from REST.

        Useful for a manual refresh or after a WebSocket reconnect to fill any
        state gaps that occurred while the connection was down.
        """
        await asyncio.gather(
            self.systems.fetch_all(),
            self.cameras.fetch_all(),
            self.partitions.fetch_all(),
            self.sensors.fetch_all(),
            self.locks.fetch_all(),
            self.lights.fetch_all(),
            self.thermostats.fetch_all(),
            self.garage_doors.fetch_all(),
            self.gates.fetch_all(),
            self.water_valves.fetch_all(),
            self.water_sensors.fetch_all(),
            self.water_meters.fetch_all(),
            self.image_sensors.fetch_all(),
            return_exceptions=True,
        )

    # --- Convenience pass-through action methods ---
    # These delegate to the relevant controller so callers don't need to
    # reach inside bridge.partitions.<method>(...) directly.

    async def arm_away(self, partition_id: str, **kwargs: Any) -> None:
        """Arm the partition in Away mode.  See :meth:`PartitionController.arm_away`."""
        await self.partitions.arm_away(partition_id, **kwargs)

    async def arm_stay(self, partition_id: str, **kwargs: Any) -> None:
        """Arm the partition in Stay mode.  See :meth:`PartitionController.arm_stay`."""
        await self.partitions.arm_stay(partition_id, **kwargs)

    async def arm_night(self, partition_id: str, **kwargs: Any) -> None:
        """Arm the partition in Night mode.  See :meth:`PartitionController.arm_night`."""
        await self.partitions.arm_night(partition_id, **kwargs)

    async def disarm(self, partition_id: str, **kwargs: Any) -> None:
        """Disarm the partition.  See :meth:`PartitionController.disarm`."""
        await self.partitions.disarm(partition_id, **kwargs)
