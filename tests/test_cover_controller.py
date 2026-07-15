"""Tests for the cover (garage door / gate) transitional-state watchdog.

The watchdog mirrors the ADC backend's GarageDoorStateFailSafe: arm on entering
Opening/Closing, cancel on a terminal state, and — only if the transition never
settles — fire one active ``refreshState`` and resolve (UNKNOWN if it still
doesn't settle).  These tests drive that lifecycle directly.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import pyadc.controllers.cover as cover_mod
from pyadc.const import CoverState, ResourceEventType
from pyadc.controllers.cover import GarageDoorController
from pyadc.events import EventBroker, EventBrokerTopic, ResourceEventMessage
from pyadc.models.cover import GarageDoor

RESOURCE_ID = "104878280-1203"
SHORT_ID = "1203"


@pytest.fixture(autouse=True)
def fast_watchdog(monkeypatch):
    """Shrink the transition timeout so tests don't wait 75 s."""
    monkeypatch.setattr(cover_mod, "COVER_TRANSITION_TIMEOUT_S", 0.02)


def make_controller(state=CoverState.OPENING):
    bridge = MagicMock()
    bridge.event_broker = EventBroker()
    controller = GarageDoorController(bridge)
    device = GarageDoor(resource_id=RESOURCE_ID, name="Garage", state=state)
    controller._devices[RESOURCE_ID] = device
    controller._devices_by_short_id[SHORT_ID] = device
    # Neutralise real HTTP; individual tests override _get as needed.
    controller._post = AsyncMock(return_value={})
    controller._get = AsyncMock(return_value={})
    return controller, device


def collect_updates(broker: EventBroker) -> list[ResourceEventMessage]:
    events: list[ResourceEventMessage] = []
    broker.subscribe([EventBrokerTopic.RESOURCE_UPDATED], events.append)
    return events


async def drain_watchdog(controller):
    """Wait for the single pending watchdog task to finish."""
    task = controller._watchdog_tasks.get(RESOURCE_ID)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=1.0)


@pytest.mark.asyncio
async def test_open_arms_watchdog():
    controller, _ = make_controller()
    await controller.open(RESOURCE_ID)
    assert RESOURCE_ID in controller._watchdog_tasks
    assert not controller._watchdog_tasks[RESOURCE_ID].done()
    controller.close_watchdogs()


@pytest.mark.asyncio
async def test_terminal_event_cancels_watchdog():
    controller, device = make_controller()
    await controller.open(RESOURCE_ID)
    task = controller._watchdog_tasks[RESOURCE_ID]

    # Terminal "Opened" event lands before the timeout: watchdog must stand down.
    controller._handle_event_by_id(SHORT_ID, ResourceEventType.GARAGE_DOOR_OPENED)

    assert device.state == CoverState.OPEN
    assert RESOURCE_ID not in controller._watchdog_tasks
    await asyncio.sleep(0)  # let the requested cancellation propagate
    assert task.cancelled()
    controller._get.assert_not_called()  # never needed the active refresh


@pytest.mark.asyncio
async def test_expiry_refreshes_and_applies_settled_state():
    controller, device = make_controller(state=CoverState.OPENING)
    controller._get = AsyncMock(
        return_value={"data": {"id": RESOURCE_ID, "attributes": {"state": CoverState.OPEN.value}}}
    )
    updates = collect_updates(controller._bridge.event_broker)

    await controller.open(RESOURCE_ID)
    await drain_watchdog(controller)

    controller._get.assert_awaited_once()
    path = controller._get.await_args.args[0]
    assert path == f"devices/garageDoors/{RESOURCE_ID}/refreshState?sendCommands=true"
    assert device.state == CoverState.OPEN
    assert any(u.device_id == RESOURCE_ID for u in updates)


@pytest.mark.asyncio
async def test_expiry_unknown_when_refresh_never_settles():
    controller, device = make_controller(state=CoverState.OPENING)
    # refreshState comes back still transitional (or empty) → must not latch OPENING.
    controller._get = AsyncMock(
        return_value={"data": {"id": RESOURCE_ID, "attributes": {"state": CoverState.OPENING.value}}}
    )

    await controller.open(RESOURCE_ID)
    await drain_watchdog(controller)

    assert device.state == CoverState.UNKNOWN


@pytest.mark.asyncio
async def test_expiry_unknown_when_refresh_errors():
    controller, device = make_controller(state=CoverState.CLOSING)
    controller._get = AsyncMock(side_effect=RuntimeError("network boom"))

    await controller.close(RESOURCE_ID)
    await drain_watchdog(controller)

    assert device.state == CoverState.UNKNOWN


@pytest.mark.asyncio
async def test_no_refresh_if_settled_during_sleep():
    controller, device = make_controller(state=CoverState.OPENING)
    await controller.open(RESOURCE_ID)
    # Door settles to OPEN before the timer fires (cancels the watchdog).
    device.state = CoverState.OPEN
    controller._sync_watchdog(device)
    # Give any (wrongly) surviving task a chance to run.
    await asyncio.sleep(0.05)
    controller._get.assert_not_called()
    assert device.state == CoverState.OPEN


@pytest.mark.asyncio
async def test_sync_watchdog_arms_on_transition_cancels_on_settle():
    controller, device = make_controller(state=CoverState.CLOSED)
    # Enter a transition via an observed status change.
    device.state = CoverState.CLOSING
    controller._sync_watchdog(device)
    assert RESOURCE_ID in controller._watchdog_tasks

    device.state = CoverState.CLOSED
    controller._sync_watchdog(device)
    assert RESOURCE_ID not in controller._watchdog_tasks


@pytest.mark.asyncio
async def test_arm_is_idempotent():
    controller, _ = make_controller()
    await controller.open(RESOURCE_ID)
    first = controller._watchdog_tasks[RESOURCE_ID]
    await controller.open(RESOURCE_ID)  # re-arm while first still live
    assert controller._watchdog_tasks[RESOURCE_ID] is first
    controller.close_watchdogs()


@pytest.mark.asyncio
async def test_close_watchdogs_cancels_pending():
    controller, _ = make_controller()
    await controller.open(RESOURCE_ID)
    task = controller._watchdog_tasks[RESOURCE_ID]
    controller.close_watchdogs()
    assert not controller._watchdog_tasks
    await asyncio.sleep(0)  # let cancellation propagate
    assert task.cancelled()
