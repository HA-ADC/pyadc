"""Water meter controller for pyadc.

Fetches Water Dragon (ADC-SHM-100-A) devices from the standard JSON:API endpoint:
    GET /web/api/devices/waterMeters

The Water Dragon does NOT receive real-time WebSocket events, so HA polls
this controller periodically via the hub's async_track_time_interval scheduler.
"""

from __future__ import annotations

from pyadc.controllers.base import BaseController
from pyadc.models.water_meter import WaterMeter


class WaterMeterController(BaseController):
    """Controller for ADC Water Dragon water meter devices."""

    resource_type = "devices/waterMeters"
    model_class = WaterMeter
    _event_state_map = {}
