"""EventBroker — pub/sub event system for pyadc.

Provides a lightweight synchronous publish/subscribe bus used to propagate
device state changes and connection events across the library.

**Callback contract:**  All callbacks are plain synchronous callables invoked
directly from :meth:`EventBroker.publish`.  Because ``publish`` is always
called from within the asyncio event loop (either from the WebSocket
processor task or from a REST-response handler), callbacks must not block.
Use ``loop.call_soon`` or ``asyncio.create_task`` if you need to schedule
async work from inside a callback.
"""

from __future__ import annotations

__all__ = [
    "EventBroker",
    "EventBrokerTopic",
    "EventBrokerMessage",
    "ResourceEventMessage",
    "EventBrokerCallbackT",
]

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)

EventBrokerCallbackT = Callable[["EventBrokerMessage"], None]


class EventBrokerTopic(str, Enum):
    """Topics that can be published and subscribed to.

    ``RESOURCE_ADDED/UPDATED/DELETED`` carry :class:`ResourceEventMessage`.
    ``CONNECTION_EVENT`` carries :class:`~pyadc.websocket.client.ConnectionEvent`.
    ``RAW_RESOURCE_EVENT`` carries :class:`~pyadc.websocket.messages.RawResourceEventMessage`.
    """

    RESOURCE_ADDED = "resource_added"
    RESOURCE_UPDATED = "resource_updated"
    RESOURCE_DELETED = "resource_deleted"
    CONNECTION_EVENT = "connection_event"
    RAW_RESOURCE_EVENT = "raw_resource_event"


@dataclass(kw_only=True)
class EventBrokerMessage:
    """Base message published through the event broker."""

    topic: EventBrokerTopic


@dataclass(kw_only=True)
class ResourceEventMessage(EventBrokerMessage):
    """Message carrying a device resource update."""

    topic: EventBrokerTopic = field(default=EventBrokerTopic.RESOURCE_UPDATED)
    device_id: str
    device_type: str  # ResourceType string, e.g. "devices/sensor"


class EventBroker:
    """Synchronous pub/sub event broker.

    Supports two subscription granularities:

    * **Broadcast** (``device_id=None``): the callback fires for *every*
      message on the subscribed topic, regardless of which device it
      concerns.  Use this for connection state monitoring or logging.
    * **Device-level** (``device_id=<id>``): the callback fires only when
      the message is a :class:`ResourceEventMessage` whose ``device_id``
      matches.  Use this to refresh a single HA entity efficiently.

    Both sets of callbacks are invoked for a matching
    :class:`ResourceEventMessage` — broadcast callbacks first, then
    device-specific ones.

    Callbacks must be **synchronous** and **non-blocking**.  They are called
    directly from whichever asyncio task calls :meth:`publish` (typically the
    WebSocket processor task or a REST response handler), so blocking inside a
    callback will stall the event loop.
    """

    def __init__(self) -> None:
        # {topic: {device_id | None: [callbacks]}}
        self._subscribers: dict[
            EventBrokerTopic,
            dict[str | None, list[EventBrokerCallbackT]],
        ] = defaultdict(lambda: defaultdict(list))

    def subscribe(
        self,
        topics: list[EventBrokerTopic],
        callback: EventBrokerCallbackT,
        device_id: str | None = None,
    ) -> Callable[[], None]:
        """Register *callback* for each topic in *topics*.

        Args:
            topics:    One or more topics to subscribe to.
            callback:  Function called with an EventBrokerMessage on publish.
            device_id: When provided, only messages for this device trigger
                       the callback (requires the message to be a
                       ResourceEventMessage).  Pass None to receive all
                       messages on the topic regardless of device.

        Returns:
            A zero-argument callable that, when called, removes all
            registrations created by this subscribe() call.
        """
        for topic in topics:
            self._subscribers[topic][device_id].append(callback)

        def unsubscribe() -> None:
            for topic in topics:
                bucket = self._subscribers[topic][device_id]
                try:
                    bucket.remove(callback)
                except ValueError:
                    pass
                if not bucket:
                    del self._subscribers[topic][device_id]

        return unsubscribe

    def publish(self, message: EventBrokerMessage) -> None:
        """Dispatch *message* to all matching subscribers.

        Callbacks subscribed with device_id=None always fire for the message's
        topic.  Callbacks subscribed with a specific device_id fire only when
        *message* is a ResourceEventMessage whose device_id matches.
        """
        topic_subs = self._subscribers.get(message.topic)
        if not topic_subs:
            return

        # Broadcast (device_id-agnostic) subscribers.
        for callback in list(topic_subs.get(None, [])):
            try:
                callback(message)
            except Exception:
                log.exception("EventBroker callback raised an exception (topic=%s)", message.topic)

        # Device-specific subscribers — only when the message carries a device_id.
        if isinstance(message, ResourceEventMessage):
            for callback in list(topic_subs.get(message.device_id, [])):
                try:
                    callback(message)
                except Exception:
                    log.exception(
                        "EventBroker callback raised an exception (topic=%s, device_id=%s)",
                        message.topic,
                        message.device_id,
                    )
