"""Base controller class shared by all pyadc device controllers.

Each concrete controller (e.g. :class:`~pyadc.controllers.partition.PartitionController`)
inherits from :class:`BaseController` and sets three class-level attributes:

* ``resource_type`` — the JSON:API path segment (e.g. ``"devices/partition"``)
  used for REST fetches and action POSTs.
* ``model_class`` — the dataclass used to deserialise API responses.
* ``_event_state_map`` — a ``{ResourceEventType: new_state_value}`` mapping
  that drives :meth:`_handle_event`.  When an :class:`EventWSMessage` arrives
  whose ``event_type`` is in the map, the device's ``state`` field is updated
  to the corresponding value and a ``RESOURCE_UPDATED`` event is published.
  Set a value to ``None`` to publish the update without mutating state (used
  by controllers that prefer property-change messages for fine-grained
  updates, such as :class:`~pyadc.controllers.thermostat.ThermostatController`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, TypeVar

from pyadc.const import INT_TO_RESOURCE_EVENT_TYPE, ResourceEventType
from pyadc.events import EventBrokerTopic, ResourceEventMessage
from pyadc.exceptions import AuthenticationFailed, NotAuthorized
from pyadc.websocket.messages import (
    DeviceStatusUpdateWSMessage,
    EventWSMessage,
    MonitorEventWSMessage,
    PropertyChangeWSMessage,
    RawResourceEventMessage,
)

if TYPE_CHECKING:
    from pyadc import AlarmBridge

log = logging.getLogger(__name__)

AdcDeviceT = TypeVar("AdcDeviceT")


class BaseController:
    """Base class for all pyadc device controllers.

    Subclasses **must** set the following class attributes:

    * ``resource_type: str`` — API path segment for this device type
      (e.g. ``"devices/partition"``).
    * ``model_class: type`` — dataclass to instantiate from JSON:API objects.
    * ``_event_state_map: dict`` — ``{ResourceEventType: new_state_value}``
      mapping used by :meth:`_handle_event` to apply named state transitions.

    The constructor subscribes to ``RAW_RESOURCE_EVENT`` on the
    :class:`~pyadc.events.EventBroker` and routes incoming WebSocket messages
    to the appropriate ``_handle_*`` method.
    """

    resource_type: str = ""
    model_class: type | None = None
    _event_state_map: dict[ResourceEventType | str, Any] = {}

    def __init__(self, bridge: "AlarmBridge") -> None:
        self._bridge = bridge
        self._devices: dict[str, Any] = {}  # {resource_id: model_instance}
        self._devices_by_short_id: dict[str, Any] = {}  # {numeric_suffix: model_instance}

        self._bridge.event_broker.subscribe(
            [EventBrokerTopic.RAW_RESOURCE_EVENT],
            self._handle_raw_event,
        )

    @property
    def devices(self) -> list[Any]:
        """Return all currently known devices as a list."""
        return list(self._devices.values())

    def get(self, device_id: str) -> Any | None:
        """Look up a device by its resource ID.

        Args:
            device_id: The JSON:API resource ``id`` string.

        Returns:
            The model instance, or ``None`` if the device is not known.
        """
        return self._devices.get(device_id)

    async def fetch_all(self) -> list[Any]:
        """Fetch all devices of this type from the REST API."""
        try:
            log.debug("Fetching %s", self.resource_type)
            resp = await self._get(self.resource_type)
            # Device endpoints use NJsonApi (Accept: application/vnd.api+json)
            # and return JSON:API format: {"data": [{id, type, attributes}, ...]}
            items = resp.get("data", [])
            if not isinstance(items, list):
                items = [items] if items else []
            new_devices: dict[str, Any] = {}
            new_by_short: dict[str, Any] = {}
            for item in items:
                try:
                    device = self._parse_device(item)
                    new_devices[device.resource_id] = device
                    # WS messages use only the numeric suffix (e.g. "1203" from "104878280-1203")
                    short_id = device.resource_id.rsplit("-", 1)[-1]
                    new_by_short[short_id] = device
                except Exception as err:
                    log.debug("Failed to parse %s device: %s", self.resource_type, err)
            log.debug("Fetched %d %s device(s)", len(new_devices), self.resource_type)
            self._devices = new_devices
            self._devices_by_short_id = new_by_short
            return list(self._devices.values())
        except Exception as err:
            log.debug("Failed to fetch %s: %s", self.resource_type, err)
            return []

    def _parse_device(self, item: dict[str, Any]) -> Any:
        """Parse a JSON:API resource object into a model instance."""
        if self.model_class is None:
            raise NotImplementedError("model_class not set")
        return self.model_class.from_json_api(item)

    def _handle_raw_event(self, message: Any) -> None:
        if not isinstance(message, RawResourceEventMessage):
            return
        ws_msg = message.ws_message

        if isinstance(ws_msg, DeviceStatusUpdateWSMessage):
            log.debug("DeviceStatusUpdate: device_id=%s new_state=%s flag_mask=%s", ws_msg.device_id, ws_msg.new_state, ws_msg.flag_mask)
            self._handle_status_update(ws_msg)
        elif isinstance(ws_msg, MonitorEventWSMessage):
            # C# serializes EventTypeEnum as integer — convert to ResourceEventType string
            raw_et = ws_msg.event_type
            event_type: ResourceEventType | str | int
            if isinstance(raw_et, (int, float)):
                event_type = INT_TO_RESOURCE_EVENT_TYPE.get(int(raw_et), int(raw_et))
            else:
                event_type = raw_et
            log.debug("MonitorEvent: device_id=%s event_type=%s (raw=%s) device_type=%s", ws_msg.device_id, event_type, raw_et, ws_msg.device_type)
            ws_msg_with_type = ws_msg
            # Allow subclasses to handle the full message (e.g. to read event_value)
            self._handle_monitor_event(ws_msg_with_type, event_type)
        elif isinstance(ws_msg, EventWSMessage):
            log.debug("EventWSMessage: device_id=%s event_type=%s", ws_msg.device_id, ws_msg.event_type)
            self._handle_event(ws_msg)
        elif isinstance(ws_msg, PropertyChangeWSMessage):
            self._handle_property_change(ws_msg)

    def _get_device_by_ws_id(self, ws_id: str) -> Any | None:
        """Look up a device by WS message ID (short numeric suffix or full resource_id)."""
        return self._devices.get(ws_id) or self._devices_by_short_id.get(ws_id)

    def _handle_status_update(self, msg: DeviceStatusUpdateWSMessage) -> None:
        """Apply a ``DeviceStatusFlags`` bitmask update to the matching device.

        Calls :meth:`~pyadc.models.base.AdcDeviceResource.apply_status_flags`
        with the ``new_state`` and ``flag_mask`` from the WebSocket message,
        then publishes a ``RESOURCE_UPDATED`` event so subscribers refresh.
        """
        device = self._get_device_by_ws_id(msg.device_id)
        if device is None:
            return
        device.apply_status_flags(msg.new_state, msg.flag_mask)
        self._bridge.event_broker.publish(
            ResourceEventMessage(
                device_id=device.resource_id,
                device_type=self.resource_type,
            )
        )

    def _handle_event(self, msg: EventWSMessage) -> None:
        """Map a ``ResourceEventType`` to a device state update via ``_event_state_map``."""
        self._handle_event_by_id(msg.device_id, msg.event_type)

    def _handle_event_by_id(self, device_id: str, event_type: ResourceEventType | str | int) -> None:
        """Core event-state mapping logic shared by EventWSMessage and MonitorEventWSMessage.

        If ``event_type`` is present in ``_event_state_map`` and the mapped value
        is not ``None``, sets ``device.state`` and publishes ``RESOURCE_UPDATED``.
        """
        device = self._get_device_by_ws_id(device_id)
        if device is None:
            return

        new_state = self._event_state_map.get(event_type)
        if new_state is not None:
            device.state = new_state
            self._bridge.event_broker.publish(
                ResourceEventMessage(
                    device_id=device.resource_id,
                    device_type=self.resource_type,
                )
            )
        else:
            log.debug(
                "Unhandled event type %s for %s", event_type, self.resource_type
            )

    async def _post(self, path: str, body: dict | None = None) -> dict:
        """POST with automatic re-login retry on 401/403 (session/token expiry).

        ADC HTTP sessions expire after ~20 min of inactivity (WS pings don't
        count). If a command returns 401 (stale AFG token) or 403 (expired
        session cookie), we re-login once and retry.
        """
        try:
            return await self._bridge.client.post(path, body or {})
        except (NotAuthorized, AuthenticationFailed):
            log.info("Auth failure on POST %s — re-authenticating and retrying", path)
            await self._bridge.auth.login()
            return await self._bridge.client.post(path, body or {})

    async def _get(self, path: str) -> dict:
        """GET with automatic re-login retry on 401/403 (session/token expiry)."""
        try:
            return await self._bridge.client.get(path)
        except (NotAuthorized, AuthenticationFailed):
            log.info("Auth failure on GET %s — re-authenticating and retrying", path)
            await self._bridge.auth.login()
            return await self._bridge.client.get(path)

    async def _put(self, path: str, body: dict | None = None) -> dict:
        """PUT with automatic re-login retry on 401/403 (session/token expiry)."""
        try:
            return await self._bridge.client.put(path, body or {})
        except (NotAuthorized, AuthenticationFailed):
            log.info("Auth failure on PUT %s — re-authenticating and retrying", path)
            await self._bridge.auth.login()
            return await self._bridge.client.put(path, body or {})

    def _handle_monitor_event(self, msg: MonitorEventWSMessage, event_type: ResourceEventType | str | int) -> None:
        """Handle a MonitorEventWSMessage. Override in subclasses for event_value access."""
        self._handle_event_by_id(msg.device_id, event_type)

    def _handle_property_change(self, msg: PropertyChangeWSMessage) -> None:
        """Handle numeric property changes. Subclasses override as needed."""
