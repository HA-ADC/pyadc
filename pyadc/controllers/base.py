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

from pyadc.const import ResourceEventType
from pyadc.events import EventBrokerTopic, ResourceEventMessage
from pyadc.websocket.messages import (
    DeviceStatusUpdateWSMessage,
    EventWSMessage,
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
        self._devices: dict[str, Any] = {}  # {device_id: model_instance}

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
            resp = await self._bridge.client.get(self.resource_type)
            items = resp.get("data", [])
            if isinstance(items, dict):
                items = [items]
            new_devices: dict[str, Any] = {}
            for item in items:
                try:
                    device = self._parse_device(item)
                    new_devices[device.resource_id] = device
                except Exception as err:
                    log.debug("Failed to parse %s device: %s", self.resource_type, err)
            self._devices = new_devices
            return list(self._devices.values())
        except Exception as err:
            log.warning("Failed to fetch %s: %s", self.resource_type, err)
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
            self._handle_status_update(ws_msg)
        elif isinstance(ws_msg, EventWSMessage):
            self._handle_event(ws_msg)
        elif isinstance(ws_msg, PropertyChangeWSMessage):
            self._handle_property_change(ws_msg)

    def _handle_status_update(self, msg: DeviceStatusUpdateWSMessage) -> None:
        """Apply a ``DeviceStatusFlags`` bitmask update to the matching device.

        Calls :meth:`~pyadc.models.base.AdcDeviceResource.apply_status_flags`
        with the ``new_state`` and ``flag_mask`` from the WebSocket message,
        then publishes a ``RESOURCE_UPDATED`` event so subscribers refresh.
        """
        device = self._devices.get(msg.device_id)
        if device is None:
            return
        device.apply_status_flags(msg.new_state, msg.flag_mask)
        self._bridge.event_broker.publish(
            ResourceEventMessage(
                device_id=msg.device_id,
                device_type=self.resource_type,
            )
        )

    def _handle_event(self, msg: EventWSMessage) -> None:
        """Map a ``ResourceEventType`` to a device state update via ``_event_state_map``.

        If ``msg.event_type`` is present in ``_event_state_map`` and the
        mapped value is not ``None``, sets ``device.state`` to the new value
        and publishes ``RESOURCE_UPDATED``.  If the value *is* ``None`` only
        the event is published (used when state must be inferred from separate
        property-change messages).
        """
        device = self._devices.get(msg.device_id)
        if device is None:
            return

        new_state = self._event_state_map.get(msg.event_type)
        if new_state is not None:
            device.state = new_state
            self._bridge.event_broker.publish(
                ResourceEventMessage(
                    device_id=msg.device_id,
                    device_type=self.resource_type,
                )
            )
        else:
            log.debug(
                "Unhandled event type %s for %s", msg.event_type, self.resource_type
            )

    def _handle_property_change(self, msg: PropertyChangeWSMessage) -> None:
        """Handle numeric property changes. Subclasses override as needed."""
