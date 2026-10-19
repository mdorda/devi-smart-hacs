from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .pydevi.dominion.client import SETPOINT_LIMITS_C, SETPOINT_STEP_C, ThermostatClient

from . import DeviRuntimeEntryData
from .entity import DeviCoordinatorEntity


DESCRIPTIONS = (
    NumberEntityDescription(key="comfort_temperature", name="Comfort Temperature", native_unit_of_measurement=UnitOfTemperature.CELSIUS),
    NumberEntityDescription(key="economy_temperature", name="Economy Temperature", native_unit_of_measurement=UnitOfTemperature.CELSIUS),
    NumberEntityDescription(key="away_temperature", name="Away Temperature", native_unit_of_measurement=UnitOfTemperature.CELSIUS),
    NumberEntityDescription(
        key="frost_protection_temperature",
        name="Frost Protection Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    NumberEntityDescription(
        key="floor_comfort_temperature",
        name="Floor Comfort Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    NumberEntityDescription(key="max_floor_temperature", name="Max Floor Temperature", native_unit_of_measurement=UnitOfTemperature.CELSIUS),
)

STATE_ATTR = {
    "comfort_temperature": "comfort_temperature_c",
    "economy_temperature": "economy_temperature_c",
    "away_temperature": "away_temperature_c",
    "frost_protection_temperature": "frost_protection_temperature_c",
    "floor_comfort_temperature": "floor_comfort_temperature_c",
    "max_floor_temperature": "max_floor_temperature_c",
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime: DeviRuntimeEntryData = entry.runtime_data
    entities = []
    for thermostat in runtime.house.thermostats:
        coordinator = runtime.coordinators[thermostat.peer_id]
        for description in DESCRIPTIONS:
            entities.append(DeviNumberEntity(coordinator, thermostat, description))
    async_add_entities(entities)


class DeviNumberEntity(DeviCoordinatorEntity, NumberEntity):
    _attr_native_step = SETPOINT_STEP_C

    def __init__(self, coordinator, thermostat, description: NumberEntityDescription) -> None:
        super().__init__(coordinator, thermostat)
        self.entity_description = description
        _, code = ThermostatClient.NUMBER_SETTINGS[description.key]
        self._attr_native_min_value, self._attr_native_max_value = SETPOINT_LIMITS_C[code]
        self._attr_unique_id = f"{thermostat.peer_id}_{description.key}"
        self._attr_name = f"{thermostat.room_name} {description.name}"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return getattr(self.coordinator.data, STATE_ATTR[self.entity_description.key])

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_command(self.coordinator.client.set_number_setting(self.entity_description.key, value))
