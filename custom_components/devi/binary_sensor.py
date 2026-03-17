from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DeviRuntimeEntryData
from .const import (
    DEFAULT_ENABLE_DIAGNOSTIC_ENTITIES,
    OPT_ENABLE_DIAGNOSTIC_ENTITIES,
    option_enabled,
)
from .entity import DeviCoordinatorEntity


DESCRIPTIONS: tuple[tuple[BinarySensorEntityDescription, bool], ...] = (
    (BinarySensorEntityDescription(key="heating", name="Heating Active", device_class="heat"), False),
    (BinarySensorEntityDescription(key="cloud_connected", name="Cloud Connected"), True),
    (
        BinarySensorEntityDescription(
            key="floor_sensor_connected",
            name="Floor Sensor Connected",
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        True,
    ),
    (
        BinarySensorEntityDescription(
            key="window_open_active",
            name="Window Open",
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        True,
    ),
    (
        BinarySensorEntityDescription(
            key="warning_disconnected",
            name="Sensor Disconnected Alarm",
            device_class=BinarySensorDeviceClass.PROBLEM,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        True,
    ),
    (
        BinarySensorEntityDescription(
            key="warning_short_circuited",
            name="Sensor Short Circuit Alarm",
            device_class=BinarySensorDeviceClass.PROBLEM,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        True,
    ),
    (
        BinarySensorEntityDescription(
            key="warning_overheat",
            name="Overheat Alarm",
            device_class=BinarySensorDeviceClass.PROBLEM,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        True,
    ),
    (
        BinarySensorEntityDescription(
            key="warning_unrecoverable",
            name="Unrecoverable Alarm",
            device_class=BinarySensorDeviceClass.PROBLEM,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        True,
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime: DeviRuntimeEntryData = entry.runtime_data
    diagnostic_enabled = option_enabled(
        entry.options,
        OPT_ENABLE_DIAGNOSTIC_ENTITIES,
        DEFAULT_ENABLE_DIAGNOSTIC_ENTITIES,
    )
    entities = []
    for thermostat in runtime.house.thermostats:
        coordinator = runtime.coordinators[thermostat.peer_id]
        for description, requires_diagnostics in DESCRIPTIONS:
            if requires_diagnostics and not diagnostic_enabled:
                continue
            entities.append(DeviBinarySensorEntity(coordinator, thermostat, description))
    async_add_entities(entities)


class DeviBinarySensorEntity(DeviCoordinatorEntity, BinarySensorEntity):
    def __init__(self, coordinator, thermostat, description: BinarySensorEntityDescription) -> None:
        super().__init__(coordinator, thermostat)
        self.entity_description = description
        self._attr_unique_id = f"{thermostat.peer_id}_{description.key}"
        self._attr_name = f"{thermostat.room_name} {description.name}"

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        if self.entity_description.key == "heating":
            return self.coordinator.data.heating_active
        if self.entity_description.key == "cloud_connected":
            return self.coordinator.data.cloud_connected
        if self.entity_description.key == "floor_sensor_connected":
            return self.coordinator.data.floor_sensor_connected
        if self.entity_description.key == "window_open_active":
            return self.coordinator.data.window_open_active
        if self.entity_description.key == "warning_disconnected":
            return self.coordinator.data.warning_disconnected
        if self.entity_description.key == "warning_short_circuited":
            return self.coordinator.data.warning_short_circuited
        if self.entity_description.key == "warning_overheat":
            return self.coordinator.data.warning_overheat
        if self.entity_description.key == "warning_unrecoverable":
            return self.coordinator.data.warning_unrecoverable
        return None
