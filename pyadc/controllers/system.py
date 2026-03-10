from __future__ import annotations

from typing import TYPE_CHECKING

from pyadc.const import ResourceType
from pyadc.controllers.base import BaseController
from pyadc.models.system import System

if TYPE_CHECKING:
    from pyadc import AlarmBridge


class SystemController(BaseController):
    """Controller for system-level resources."""

    resource_type = ResourceType.SYSTEM
    model_class = System
    _event_state_map = {}
