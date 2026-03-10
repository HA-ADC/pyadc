from __future__ import annotations

from typing import TYPE_CHECKING

from pyadc.const import ResourceEventType, ResourceType
from pyadc.controllers.base import BaseController
from pyadc.const import CoverState
from pyadc.models.cover import GarageDoor, Gate

if TYPE_CHECKING:
    from pyadc import AlarmBridge


class GarageDoorController(BaseController):
    """Controller for garage door devices."""

    resource_type = ResourceType.GARAGE_DOOR
    model_class = GarageDoor
    _event_state_map = {
        ResourceEventType.GARAGE_DOOR_OPENED: CoverState.OPEN,
        ResourceEventType.GARAGE_DOOR_CLOSED: CoverState.CLOSED,
    }

    async def open(self, device_id: str) -> None:
        """Open a garage door."""
        await self._bridge.client.post(
            f"{self.resource_type}/{device_id}/open",
            {},
        )

    async def close(self, device_id: str) -> None:
        """Close a garage door."""
        await self._bridge.client.post(
            f"{self.resource_type}/{device_id}/close",
            {},
        )


class GateController(BaseController):
    """Controller for gate devices."""

    resource_type = ResourceType.GATE
    model_class = Gate
    _event_state_map = {
        ResourceEventType.OPENED: CoverState.OPEN,
        ResourceEventType.CLOSED: CoverState.CLOSED,
    }

    async def open(self, device_id: str) -> None:
        """Open a gate."""
        await self._bridge.client.post(
            f"{self.resource_type}/{device_id}/open",
            {},
        )

    async def close(self, device_id: str) -> None:
        """Close a gate."""
        await self._bridge.client.post(
            f"{self.resource_type}/{device_id}/close",
            {},
        )
