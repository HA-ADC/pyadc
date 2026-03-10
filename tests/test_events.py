"""Tests for the EventBroker pub/sub system."""
import pytest
from pyadc.events import (
    EventBroker, EventBrokerMessage, EventBrokerTopic, ResourceEventMessage,
)


def test_subscribe_and_publish():
    broker = EventBroker()
    received = []

    def callback(msg):
        received.append(msg)

    broker.subscribe([EventBrokerTopic.RESOURCE_UPDATED], callback)
    msg = ResourceEventMessage(device_id="dev-1", device_type="devices/partition")
    broker.publish(msg)

    assert len(received) == 1
    assert received[0].device_id == "dev-1"


def test_unsubscribe():
    broker = EventBroker()
    received = []
    unsub = broker.subscribe(
        [EventBrokerTopic.RESOURCE_UPDATED], lambda m: received.append(m)
    )
    unsub()
    broker.publish(ResourceEventMessage(device_id="x", device_type="devices/partition"))
    assert len(received) == 0


def test_unsubscribe_is_idempotent():
    """Calling unsubscribe twice should not raise."""
    broker = EventBroker()
    unsub = broker.subscribe([EventBrokerTopic.RESOURCE_UPDATED], lambda m: None)
    unsub()
    unsub()  # should not raise


def test_device_id_filter():
    broker = EventBroker()
    received_a, received_b = [], []

    broker.subscribe(
        [EventBrokerTopic.RESOURCE_UPDATED],
        lambda m: received_a.append(m),
        device_id="dev-A",
    )
    broker.subscribe(
        [EventBrokerTopic.RESOURCE_UPDATED],
        lambda m: received_b.append(m),
        device_id="dev-B",
    )

    broker.publish(ResourceEventMessage(device_id="dev-A", device_type="devices/sensor"))

    assert len(received_a) == 1
    assert len(received_b) == 0


def test_broadcast_subscriber_receives_all():
    broker = EventBroker()
    received = []
    broker.subscribe(
        [EventBrokerTopic.RESOURCE_UPDATED], lambda m: received.append(m)
    )

    broker.publish(ResourceEventMessage(device_id="dev-1", device_type="x"))
    broker.publish(ResourceEventMessage(device_id="dev-2", device_type="x"))

    assert len(received) == 2


def test_callback_exception_isolated():
    """One bad callback must not stop others from running."""
    broker = EventBroker()
    received = []

    def bad_callback(msg):
        raise RuntimeError("oops")

    broker.subscribe([EventBrokerTopic.RESOURCE_UPDATED], bad_callback)
    broker.subscribe(
        [EventBrokerTopic.RESOURCE_UPDATED], lambda m: received.append(m)
    )

    # Should not raise
    broker.publish(ResourceEventMessage(device_id="x", device_type="x"))
    assert len(received) == 1


def test_multiple_topics():
    broker = EventBroker()
    received = []
    broker.subscribe(
        [EventBrokerTopic.RESOURCE_UPDATED, EventBrokerTopic.RESOURCE_ADDED],
        lambda m: received.append(m),
    )
    broker.publish(
        ResourceEventMessage(
            topic=EventBrokerTopic.RESOURCE_UPDATED, device_id="a", device_type="x"
        )
    )
    broker.publish(
        ResourceEventMessage(
            topic=EventBrokerTopic.RESOURCE_ADDED, device_id="b", device_type="x"
        )
    )
    assert len(received) == 2


def test_publish_no_subscribers_is_noop():
    """Publishing with no subscribers should not raise."""
    broker = EventBroker()
    broker.publish(ResourceEventMessage(device_id="x", device_type="x"))


def test_broadcast_and_device_specific_both_fire():
    """A broadcast sub and a device-specific sub both receive the same message."""
    broker = EventBroker()
    broadcast_received = []
    device_received = []

    broker.subscribe(
        [EventBrokerTopic.RESOURCE_UPDATED],
        lambda m: broadcast_received.append(m),
    )
    broker.subscribe(
        [EventBrokerTopic.RESOURCE_UPDATED],
        lambda m: device_received.append(m),
        device_id="dev-1",
    )

    broker.publish(ResourceEventMessage(device_id="dev-1", device_type="x"))
    assert len(broadcast_received) == 1
    assert len(device_received) == 1
