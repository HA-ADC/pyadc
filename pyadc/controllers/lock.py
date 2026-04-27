from __future__ import annotations

from typing import TYPE_CHECKING

from pyadc.const import ResourceEventType, ResourceType
from pyadc.controllers.base import BaseController, _validate_device_id
from pyadc.models.lock import Lock, LockState

if TYPE_CHECKING:
    from pyadc import AlarmBridge


class LockController(BaseController):
    """Controller for Alarm.com smart lock devices."""

    resource_type = ResourceType.LOCK
    model_class = Lock
    _event_state_map = {
        ResourceEventType.DOOR_LOCKED: LockState.LOCKED,
        ResourceEventType.DOOR_UNLOCKED: LockState.UNLOCKED,
    }

    async def lock(self, lock_id: str) -> None:
        """Lock a lock."""
        _validate_device_id(lock_id)
        await self._post(
            f"{self.resource_type}/{lock_id}/lock",
            {},
        )

    async def unlock(self, lock_id: str) -> None:
        """Unlock a lock."""
        _validate_device_id(lock_id)
        await self._post(
            f"{self.resource_type}/{lock_id}/unlock",
            {},
        )
