from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DeviRuntimeEntryData
from .const import (
    DEFAULT_ENABLE_ADVANCED_ENTITIES,
    DEFAULT_ENABLE_DIAGNOSTIC_ENTITIES,
    OPT_ENABLE_ADVANCED_ENTITIES,
    OPT_ENABLE_DIAGNOSTIC_ENTITIES,
    option_enabled,
)
from .entity import DeviCoordinatorEntity


DESCRIPTIONS: tuple[tuple[SensorEntityDescription, bool, bool], ...] = (
    (
        SensorEntityDescription(key="room_temperature", name="Room Temperature", native_unit_of_measurement=UnitOfTemperature.CELSIUS),
        False,
        False,
    ),
    (
        SensorEntityDescription(key="floor_temperature", name="Floor Temperature", native_unit_of_measurement=UnitOfTemperature.CELSIUS),
        True,
        False,
    ),
    (
        SensorEntityDescription(key="firmware_version", name="Firmware Version", entity_category=EntityCategory.DIAGNOSTIC),
        False,
        True,
    ),
    (
        SensorEntityDescription(key="hardware_version", name="Hardware Version", entity_category=EntityCategory.DIAGNOSTIC),
        False,
        True,
    ),
    (
        SensorEntityDescription(key="serial_number", name="Serial Number", entity_category=EntityCategory.DIAGNOSTIC),
        False,
        True,
    ),
    (
        SensorEntityDescription(key="production_date", name="Production Date", entity_category=EntityCategory.DIAGNOSTIC),
        False,
        True,
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime: DeviRuntimeEntryData = entry.runtime_data
    advanced_enabled = option_enabled(entry.options, OPT_ENABLE_ADVANCED_ENTITIES, DEFAULT_ENABLE_ADVANCED_ENTITIES)
    diagnostic_enabled = option_enabled(
        entry.options,
        OPT_ENABLE_DIAGNOSTIC_ENTITIES,
        DEFAULT_ENABLE_DIAGNOSTIC_ENTITIES,
    )
    entities = []
    for thermostat in runtime.house.thermostats:
        coordinator = runtime.coordinators[thermostat.peer_id]
        for description, requires_advanced, requires_diagnostics in DESCRIPTIONS:
            if requires_advanced and not advanced_enabled:
                continue
            if requires_diagnostics and not diagnostic_enabled:
                continue
            entities.append(DeviSensorEntity(coordinator, thermostat, description))
    async_add_entities(entities)


class DeviSensorEntity(DeviCoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, thermostat, description: SensorEntityDescription) -> None:
        super().__init__(coordinator, thermostat)
        self.entity_description = description
        self._attr_unique_id = f"{thermostat.peer_id}_{description.key}"
        self._attr_name = f"{thermostat.room_name} {description.name}"

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        if self.entity_description.key == "room_temperature":
            return self.coordinator.data.room_temperature_c
        if self.entity_description.key == "floor_temperature":
            return self.coordinator.data.floor_temperature_c
        if self.entity_description.key == "firmware_version":
            return self.coordinator.data.firmware_version
        if self.entity_description.key == "hardware_version":
            return self.coordinator.data.hardware_version
        if self.entity_description.key == "serial_number":
            return self.coordinator.data.serial_number
        if self.entity_description.key == "production_date":
            return self.coordinator.data.production_date
        return None
