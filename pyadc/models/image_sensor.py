"""Image sensor model for pyadc."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Self

from pyadc.const import ResourceType
from pyadc.models.base import AdcDeviceResource, _camel_to_snake


@dataclass
class ImageSensor(AdcDeviceResource):
    """Alarm.com image sensor resource."""

    resource_type: ClassVar[str] = ResourceType.IMAGE_SENSOR
    last_image_url: str | None = None
    last_update: datetime | None = None

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        """Parse from JSON:API resource object."""
        attrs = data.get("attributes", {})
        snake_attrs = {_camel_to_snake(k): v for k, v in attrs.items()}

        last_update: datetime | None = None
        raw_ts = snake_attrs.get("last_update") or snake_attrs.get("last_update_timestamp")
        if raw_ts:
            try:
                last_update = datetime.fromisoformat(str(raw_ts))
            except (ValueError, TypeError):
                last_update = None

        return cls(
            resource_id=data.get("id", ""),
            name=snake_attrs.get("description", ""),
            last_image_url=snake_attrs.get("last_image_url"),
            last_update=last_update,
            battery_level_pct=snake_attrs.get("battery_level_null"),
        )
