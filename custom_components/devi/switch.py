from __future__ import annotations

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DeviRuntimeEntryData
from .entity import DeviCoordinatorEntity


DESCRIPTIONS = (
    SwitchEntityDescription(key="forecast_heating_enabled", name="Forecast Heating"),
    SwitchEntityDescription(key="window_open_detection_enabled", name="Window Open Detection"),
    SwitchEntityDescription(key="safety_lock_enabled", name="Safety Lock"),
    SwitchEntityDescription(key="floor_comfort_enabled", name="Floor Comfort"),
    SwitchEntityDescription(key="away_planned", name="Away Planned", entity_category=EntityCategory.CONFIG),
)

STATE_ATTR = {
    "forecast_heating_enabled": "forecast_heating_enabled",
    "window_open_detection_enabled": "window_open_detection_enabled",
    "safety_lock_enabled": "safety_lock_enabled",
    "floor_comfort_enabled": "floor_comfort_enabled",
    "away_planned": "away_planned",
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime: DeviRuntimeEntryData = entry.runtime_data
    entities = []
    for thermostat in runtime.house.thermostats:
        coordinator = runtime.coordinators[thermostat.peer_id]
        for description in DESCRIPTIONS:
            entities.append(DeviSwitchEntity(coordinator, thermostat, description))
    async_add_entities(entities)


class DeviSwitchEntity(DeviCoordinatorEntity, SwitchEntity):
    def __init__(self, coordinator, thermostat, description: SwitchEntityDescription) -> None:
        super().__init__(coordinator, thermostat)
        self.entity_description = description
        self._attr_unique_id = f"{thermostat.peer_id}_{description.key}"
        self._attr_name = f"{thermostat.room_name} {description.name}"

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        return getattr(self.coordinator.data, STATE_ATTR[self.entity_description.key])

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_command(self._set(True))

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_command(self._set(False))

    async def _set(self, enabled: bool) -> None:
        client = self.coordinator.client
        if self.entity_description.key == "away_planned":
            raise ServiceValidationError("Away Planned is read-only: use the vacation preset instead.")
        else:
            await client.set_switch_setting(self.entity_description.key, enabled)
