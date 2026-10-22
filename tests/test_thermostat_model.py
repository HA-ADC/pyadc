"""Thermostat mode parsing from the REST resource.

``state`` is the configured mode in the web ThermostatStatusEnum
(Off=1, Heat=2, Cool=3, Auto=4, AuxHeat=5). ``inferredState`` is the direction
ADC infers the thermostat is working in and must not be used as the mode.
"""
from __future__ import annotations

import pytest

from pyadc.const import ThermostatTemperatureMode as Mode
from pyadc.models.thermostat import Thermostat


def resource(**attrs) -> dict:
    return {
        "id": "104878280-2200",
        "type": "devices/thermostat",
        "attributes": {"description": "Hallway", "ambientTemp": 77, **attrs},
    }


def test_auto_thermostat_that_is_cooling_reports_auto():
    # Captured from a real thermostat in Auto (ADC UI: "Hallway, Auto",
    # "Auto Cool to 78°"). Previously parsed as COOL from inferredState.
    t = Thermostat.from_json_api(
        resource(state=4, desiredState=4, inferredState=2, heatSetpoint=74, coolSetpoint=78)
    )
    assert t.state is Mode.AUTO
    assert (t.target_temperature_heat, t.target_temperature_cool) == (74, 78)


@pytest.mark.parametrize(
    ("status", "mode"),
    [(1, Mode.OFF), (2, Mode.HEAT), (3, Mode.COOL), (4, Mode.AUTO), (5, Mode.AUX_HEAT)],
)
def test_state_uses_web_status_enum(status, mode):
    assert Thermostat.from_json_api(resource(state=status, inferredState=0)).state is mode


@pytest.mark.parametrize("state", [None, 0, 99, "junk"])
def test_falls_back_to_inferred_state_when_state_unusable(state):
    attrs = {"inferredState": 2}
    if state is not None:
        attrs["state"] = state
    assert Thermostat.from_json_api(resource(**attrs)).state is Mode.COOL


def test_defaults_to_off_when_nothing_usable():
    assert Thermostat.from_json_api(resource()).state is Mode.OFF
