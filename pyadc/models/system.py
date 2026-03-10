"""System and trouble-condition models for pyadc."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from pyadc.const import ResourceType
from pyadc.models.base import AdcResource, _camel_to_snake


@dataclass
class System(AdcResource):
    """Alarm.com system resource."""

    resource_type: ClassVar[str] = ResourceType.SYSTEM
    unit_id: int = 0

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        """Parse from JSON:API resource object."""
        attrs = data.get("attributes", {})
        snake_attrs = {_camel_to_snake(k): v for k, v in attrs.items()}
        return cls(
            resource_id=data.get("id", ""),
            name=snake_attrs.get("description", snake_attrs.get("name", "")),
            unit_id=snake_attrs.get("unit_id", 0),
        )


@dataclass
class TroubleCondition(AdcResource):
    """Alarm.com trouble condition resource."""

    resource_type: ClassVar[str] = ResourceType.TROUBLE_CONDITION
    device_id: str = ""
    description: str = ""
    condition_type: str = ""

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        """Parse from JSON:API resource object."""
        attrs = data.get("attributes", {})
        snake_attrs = {_camel_to_snake(k): v for k, v in attrs.items()}
        return cls(
            resource_id=data.get("id", ""),
            name=snake_attrs.get("description", ""),
            device_id=snake_attrs.get("device_id", ""),
            description=snake_attrs.get("description", ""),
            condition_type=snake_attrs.get("condition_type", ""),
        )
