from __future__ import annotations

from datetime import datetime
import logging

from homeassistant.components.datetime import DateTimeEntity, DateTimeEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .pydevi.models import AwayInterval

from . import DeviRuntimeEntryData
from .entity import DeviCoordinatorEntity

_LOGGER = logging.getLogger(__name__)


DESCRIPTIONS = (
    DateTimeEntityDescription(
        key="away_start",
        name="Away Start",
        entity_category=EntityCategory.CONFIG,
    ),
    DateTimeEntityDescription(
        key="away_end",
        name="Away End",
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    _LOGGER.debug("Setting up DEVI datetime platform for entry %s", entry.entry_id)
    runtime: DeviRuntimeEntryData = entry.runtime_data
    entities = []
    for thermostat in runtime.house.thermostats:
        coordinator = runtime.coordinators[thermostat.peer_id]
        for description in DESCRIPTIONS:
            entities.append(DeviAwayDateTimeEntity(coordinator, thermostat, description))
    async_add_entities(entities)


class DeviAwayDateTimeEntity(DeviCoordinatorEntity, DateTimeEntity):
    def __init__(self, coordinator, thermostat, description: DateTimeEntityDescription) -> None:
        super().__init__(coordinator, thermostat)
        self.entity_description = description
        self._attr_unique_id = f"{thermostat.peer_id}_{description.key}"
        self._attr_name = f"{thermostat.room_name} {description.name}"

    @property
    def native_value(self) -> datetime | None:
        away = self.coordinator.client.state.away_interval
        if away is None:
            return None
        if self.entity_description.key == "away_start":
            return away.start
        return away.end

    async def async_set_value(self, value: datetime) -> None:
        current = self.coordinator.client.state.away_interval or AwayInterval(start=None, end=None)
        if self.entity_description.key == "away_start":
            away = AwayInterval(start=value, end=current.end)
        else:
            away = AwayInterval(start=current.start, end=value)
        if away.start and away.end and away.start >= away.end:
            raise ValueError("away start must be before away end")
        await self.coordinator.client.set_away_interval(
            away,
            is_planned=bool(self.coordinator.client.state.away_planned),
        )
        await self.coordinator.async_request_refresh()
