from __future__ import annotations

from typing import TYPE_CHECKING

from pyadc.const import ResourceEventType, ResourceType
from pyadc.controllers.base import BaseController
from pyadc.models.lock import Lock, LockState

if TYPE_CHECKING:
    from pyadc import AlarmBridge


class LockController(BaseController):
    resource_type = ResourceType.LOCK
    model_class = Lock
    _event_state_map = {
        ResourceEventType.DOOR_LOCKED: LockState.LOCKED,
        ResourceEventType.DOOR_UNLOCKED: LockState.UNLOCKED,
    }

    async def lock(self, lock_id: str) -> None:
        """Lock a lock."""
        await self._bridge.client.post(
            f"{self.resource_type}/{lock_id}/lock",
            {},
        )

    async def unlock(self, lock_id: str) -> None:
        """Unlock a lock."""
        await self._bridge.client.post(
            f"{self.resource_type}/{lock_id}/unlock",
            {},
        )
