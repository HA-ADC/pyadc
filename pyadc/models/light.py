"""Light model for pyadc."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from pyadc.const import LightState, ResourceType
from pyadc.models.base import AdcDeviceResource, _camel_to_snake


@dataclass
class Light(AdcDeviceResource):
    """Alarm.com light resource."""

    resource_type: ClassVar[str] = ResourceType.LIGHT
    state: LightState = LightState.NO_STATE
    brightness: int | None = None  # 1-99 ADC scale
    supports_dimming: bool = False
    supports_rgb: bool = False
    supports_white_color: bool = False
    light_color_format: str | None = None  # RGBW, RGB, HSV, WARM_TO_COOL
    rgb_color: tuple[int, int, int] | None = None
    color_temp: int | None = None

    @property
    def brightness_pct(self) -> int | None:
        """Brightness as 0-255 HA scale."""
        if self.brightness is None:
            return None
        return round(self.brightness / 99 * 255)

    @classmethod
    def from_json_api(cls, data: dict[str, Any]) -> Self:
        """Parse from JSON:API resource object."""
        attrs = data.get("attributes", {})
        snake_attrs = {_camel_to_snake(k): v for k, v in attrs.items()}

        raw_state = snake_attrs.get("state")
        try:
            state = LightState(raw_state) if raw_state is not None else LightState.NO_STATE
        except ValueError:
            state = LightState.NO_STATE

        # Parse RGB color from hex string if available
        rgb_color: tuple[int, int, int] | None = None
        hex_color = snake_attrs.get("hex_color")
        if hex_color and isinstance(hex_color, str):
            hex_color = hex_color.lstrip("#")
            if len(hex_color) >= 6:
                try:
                    rgb_color = (
                        int(hex_color[0:2], 16),
                        int(hex_color[2:4], 16),
                        int(hex_color[4:6], 16),
                    )
                except ValueError:
                    rgb_color = None

        light_level = snake_attrs.get("light_level")
        brightness = int(light_level) if light_level is not None else None

        color_format = snake_attrs.get("light_color_format")
        light_color_format: str | None = None
        if color_format is not None:
            # Map enum int values to string names
            _format_map = {0: None, 1: "RGBW", 2: "RGB", 3: "WARM_TO_COOL", 4: "HSV"}
            light_color_format = _format_map.get(color_format, str(color_format))

        return cls(
            resource_id=data.get("id", ""),
            name=snake_attrs.get("description", ""),
            state=state,
            brightness=brightness,
            supports_dimming=snake_attrs.get("is_dimmer", False),
            supports_rgb=snake_attrs.get("supports_rgb_color_control", False),
            supports_white_color=snake_attrs.get("supports_white_light_color_control", False),
            light_color_format=light_color_format,
            rgb_color=rgb_color,
            battery_level_pct=snake_attrs.get("battery_level_null"),
        )
