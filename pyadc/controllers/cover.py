from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pyadc.const import (
    COVER_REFRESH_HTTP_TIMEOUT_S,
    COVER_TRANSITION_TIMEOUT_S,
    CoverState,
    ResourceEventType,
    ResourceType,
)
from pyadc.controllers.base import BaseController, _validate_device_id
from pyadc.events import ResourceEventMessage
from pyadc.models.cover import GarageDoor, Gate

if TYPE_CHECKING:
    from pyadc import AlarmBridge

log = logging.getLogger(__name__)

# States a cover passes *through* rather than rests in.  A cover sitting in one
# of these for longer than COVER_TRANSITION_TIMEOUT_S is what the watchdog acts on.
_TRANSITIONAL_STATES = frozenset({CoverState.OPENING, CoverState.CLOSING})


class BaseCoverController(BaseController):
    """Shared logic for garage-door and gate controllers.

    Both expose ``open``/``close`` and carry a transitional-state watchdog: when
    a cover enters ``OPENING``/``CLOSING`` — whether from a local command or an
    observed push — a one-shot timer is armed.  A terminal ``OPEN``/``CLOSED``
    cancels it; if the timer expires while still transitional, the controller
    fires a single ``refreshState?sendCommands=true`` (the backend's active
    device re-poll) and resolves to the settled state.  This mirrors the ADC
    backend's own ``GarageDoorStateFailSafe`` and is *not* continual polling: a
    stationary cover never enters a transitional state, so it never arms.
    """

    def __init__(self, bridge: "AlarmBridge") -> None:
        super().__init__(bridge)
        # resource_id → pending watchdog task (one per device at most)
        self._watchdog_tasks: dict[str, asyncio.Task] = {}

    # --- Commands ---------------------------------------------------------

    async def _toggle(self, device_id: str, action: str) -> None:
        """Open or close the cover, then arm the transitional watchdog."""
        _validate_device_id(device_id)
        await self._post(f"{self.resource_type}/{device_id}/{action}", {})
        # The command puts the cover into a transitional phase (HA also sets this
        # optimistically).  Arm now — like the backend does on command — so a
        # door that never pushes a terminal state still gets reconciled.
        self._arm_watchdog(device_id)

    async def open(self, device_id: str) -> None:
        """Open the cover."""
        await self._toggle(device_id, "open")

    async def close(self, device_id: str) -> None:
        """Close the cover."""
        await self._toggle(device_id, "close")

    # --- State-change hooks -----------------------------------------------

    def _handle_status_update(self, msg) -> None:  # type: ignore[override]
        super()._handle_status_update(msg)
        device = self._get_device_by_ws_id(msg.device_id)
        if device is not None:
            self._sync_watchdog(device)

    def _handle_event_by_id(self, device_id, event_type) -> None:  # type: ignore[override]
        super()._handle_event_by_id(device_id, event_type)
        device = self._get_device_by_ws_id(device_id)
        if device is not None:
            self._sync_watchdog(device)

    def _sync_watchdog(self, device) -> None:
        """Arm on entering a transitional state; cancel once it settles."""
        if getattr(device, "state", None) in _TRANSITIONAL_STATES:
            self._arm_watchdog(device.resource_id)
        else:
            self._cancel_watchdog(device.resource_id)

    # --- Watchdog machinery -----------------------------------------------

    def _arm_watchdog(self, resource_id: str) -> None:
        """Start a one-shot watchdog for *resource_id* unless one is already live.

        Keeping any in-flight timer (rather than resetting it) means repeated
        ``Opening`` pushes can't push the deadline out indefinitely — the wait is
        measured from when the transition first began.
        """
        existing = self._watchdog_tasks.get(resource_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._watchdog(resource_id), name=f"cover_watchdog_{resource_id}"
        )
        self._watchdog_tasks[resource_id] = task

    def _cancel_watchdog(self, resource_id: str) -> None:
        task = self._watchdog_tasks.pop(resource_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _watchdog(self, resource_id: str) -> None:
        """Wait out the transition; if it never settles, force an active refresh."""
        try:
            await asyncio.sleep(COVER_TRANSITION_TIMEOUT_S)
            device = self._get_device_by_ws_id(resource_id)
            # A terminal state may have landed during the sleep; only act if the
            # cover is genuinely still mid-transition.
            if device is None or getattr(device, "state", None) not in _TRANSITIONAL_STATES:
                return
            log.info(
                "%s %s stuck in %s for %ss — actively refreshing device state",
                self.resource_type,
                resource_id,
                getattr(device, "state", None),
                COVER_TRANSITION_TIMEOUT_S,
            )
            await self._refresh_device_state(resource_id)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pragma: no cover - defensive
            log.warning("Cover watchdog for %s failed: %s", resource_id, err)
        finally:
            # Remove ourselves so a later transition can arm a fresh timer.
            if self._watchdog_tasks.get(resource_id) is asyncio.current_task():
                self._watchdog_tasks.pop(resource_id, None)

    async def _refresh_device_state(self, resource_id: str) -> None:
        """Hit ``refreshState?sendCommands=true`` and apply the settled state.

        This is the same active device poll the backend fail-safe performs: the
        endpoint sends a GetDeviceState command to the physical opener, then
        blocks-polls the DB (up to ~120 s) for the result.  If it still doesn't
        settle to Open/Closed, we set ``UNKNOWN`` rather than leave a phantom
        ``OPENING`` — matching how the alarm.com app renders a non-terminal door.
        """
        settled: CoverState | None = None
        try:
            async with asyncio.timeout(COVER_REFRESH_HTTP_TIMEOUT_S):
                resp = await self._get(
                    f"{self.resource_type}/{resource_id}/refreshState?sendCommands=true"
                )
            item = resp.get("data") if isinstance(resp, dict) else None
            if item:
                fresh = self._parse_device(item)
                settled = getattr(fresh, "state", None)
        except Exception as err:
            log.warning("refreshState for %s failed: %s", resource_id, err)

        device = self._get_device_by_ws_id(resource_id)
        if device is None:
            return

        if settled is not None and settled not in _TRANSITIONAL_STATES:
            device.state = settled
        else:
            # The active poll came back empty, errored, or still transitional —
            # don't latch "opening" forever; report honest uncertainty.
            log.info(
                "%s %s did not settle after active refresh — marking UNKNOWN",
                self.resource_type,
                resource_id,
            )
            device.state = CoverState.UNKNOWN

        self._bridge.event_broker.publish(
            ResourceEventMessage(
                device_id=device.resource_id,
                device_type=self.resource_type,
            )
        )

    # --- Teardown ---------------------------------------------------------

    def close_watchdogs(self) -> None:
        """Cancel every pending watchdog (called on bridge shutdown)."""
        for task in self._watchdog_tasks.values():
            if not task.done():
                task.cancel()
        self._watchdog_tasks.clear()


class GarageDoorController(BaseCoverController):
    """Controller for garage door devices."""

    resource_type = ResourceType.GARAGE_DOOR
    model_class = GarageDoor
    _event_state_map = {
        ResourceEventType.GARAGE_DOOR_OPENED: CoverState.OPEN,
        ResourceEventType.GARAGE_DOOR_CLOSED: CoverState.CLOSED,
        ResourceEventType.OPENED: CoverState.OPEN,
        ResourceEventType.CLOSED: CoverState.CLOSED,
    }


class GateController(BaseCoverController):
    """Controller for gate devices."""

    resource_type = ResourceType.GATE
    model_class = Gate
    _event_state_map = {
        ResourceEventType.OPENED: CoverState.OPEN,
        ResourceEventType.CLOSED: CoverState.CLOSED,
    }
