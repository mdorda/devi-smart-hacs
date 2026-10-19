from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from homeassistant.components.switch import SwitchEntityDescription
from homeassistant.exceptions import ServiceValidationError
import pytest

from custom_components.devi.climate import DeviClimateEntity
from custom_components.devi.coordinator import DeviThermostatCoordinator
from custom_components.devi.datetime import DESCRIPTIONS as DATETIME_DESCRIPTIONS, DeviAwayDateTimeEntity
from custom_components.devi.number import DESCRIPTIONS as NUMBER_DESCRIPTIONS, DeviNumberEntity
from custom_components.devi.pydevi.models import ThermostatState
from custom_components.devi.switch import DeviSwitchEntity


class _Client:
    def __init__(self) -> None:
        self.state = ThermostatState(available=True)

    def start(self, on_update, on_connection) -> None:
        pass


THERMOSTAT = SimpleNamespace(peer_id="peer-1", room_name="Bathroom", serial_number=None)


def _coordinator(hass) -> DeviThermostatCoordinator:
    return DeviThermostatCoordinator(hass, _Client(), "peer-1")


async def test_away_planned_switch_refuses_writes(hass) -> None:
    entity = DeviSwitchEntity(_coordinator(hass), THERMOSTAT, SwitchEntityDescription(key="away_planned", name="Away Planned"))

    with pytest.raises(ServiceValidationError):
        await entity.async_turn_on()
    with pytest.raises(ServiceValidationError):
        await entity.async_turn_off()


async def test_away_dates_refuse_writes(hass) -> None:
    for description in DATETIME_DESCRIPTIONS:
        entity = DeviAwayDateTimeEntity(_coordinator(hass), THERMOSTAT, description)

        with pytest.raises(ServiceValidationError):
            await entity.async_set_value(datetime(2026, 11, 1, 8, 0, tzinfo=UTC))


async def test_number_ranges_match_the_thermostat(hass) -> None:
    ranges = {
        description.key: (entity.native_min_value, entity.native_max_value)
        for description in NUMBER_DESCRIPTIONS
        for entity in [DeviNumberEntity(_coordinator(hass), THERMOSTAT, description)]
    }

    assert ranges == {
        "comfort_temperature": (5.0, 28.0),
        "economy_temperature": (5.0, 28.0),
        "away_temperature": (10.0, 28.0),
        "frost_protection_temperature": (5.0, 9.0),
        "floor_comfort_temperature": (7.0, 28.0),
        "max_floor_temperature": (7.0, 33.0),
    }


async def test_climate_range_matches_the_thermostat(hass) -> None:
    entity = DeviClimateEntity(_coordinator(hass), THERMOSTAT)

    assert (entity.min_temp, entity.max_temp, entity.target_temperature_step) == (5.0, 28.0, 0.5)
