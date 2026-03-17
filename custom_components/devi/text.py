from __future__ import annotations

import logging

from homeassistant.components.text import TextEntity, TextEntityDescription, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .pydevi.dominion.schedule import DAY_NAMES, decode_week_schedule, encode_week_schedule, format_day_ranges_text, parse_day_ranges_text

from . import DeviRuntimeEntryData
from .entity import DeviCoordinatorEntity

_LOGGER = logging.getLogger(__name__)


DESCRIPTIONS = tuple(
    TextEntityDescription(
        key=f"schedule_{day.lower()}",
        name=f"Schedule {day.title()}",
        entity_category=EntityCategory.CONFIG,
    )
    for day in DAY_NAMES
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    _LOGGER.debug("Setting up DEVI text platform for entry %s", entry.entry_id)
    runtime: DeviRuntimeEntryData = entry.runtime_data
    entities = []
    for thermostat in runtime.house.thermostats:
        coordinator = runtime.coordinators[thermostat.peer_id]
        for description in DESCRIPTIONS:
            entities.append(DeviScheduleTextEntity(coordinator, thermostat, description))
    _LOGGER.debug(
        "Prepared DEVI text entities: %s",
        [entity._attr_unique_id for entity in entities],
    )
    async_add_entities(entities)


class DeviScheduleTextEntity(DeviCoordinatorEntity, TextEntity):
    _attr_mode = TextMode.TEXT

    def __init__(self, coordinator, thermostat, description: TextEntityDescription) -> None:
        super().__init__(coordinator, thermostat)
        self.entity_description = description
        self._attr_unique_id = f"{thermostat.peer_id}_{description.key}"
        self._attr_name = f"{thermostat.room_name} {description.name}"

    @property
    def native_value(self) -> str:
        schedule = self.coordinator.client.state.weekly_schedule
        if schedule is None:
            return ""
        day_name = self.entity_description.key.removeprefix("schedule_").upper()
        plan = decode_week_schedule(schedule.week_1_raw, schedule.week_2_raw)
        return format_day_ranges_text(plan.days[day_name])

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        schedule = self.coordinator.client.state.weekly_schedule
        if schedule is None:
            return {}
        day_name = self.entity_description.key.removeprefix("schedule_").upper()
        plan = decode_week_schedule(schedule.week_1_raw, schedule.week_2_raw)
        day_index = DAY_NAMES.index(day_name)
        combined = schedule.week_1_raw + (schedule.week_2_raw or b"")
        return {
            "day": day_name,
            "slot_count": len(plan.days[day_name]),
            "example": "06:00-08:30, 16:00-22:30",
            "raw_hex": decode_week_schedule(schedule.week_1_raw, schedule.week_2_raw).days[day_name],
            "day_index": day_index,
            "editor_format": "comma-separated HH:MM-HH:MM ranges",
        }

    async def async_set_value(self, value: str) -> None:
        current = self.coordinator.client.state.weekly_schedule
        if current is None:
            await self.coordinator.async_request_refresh()
            current = self.coordinator.client.state.weekly_schedule
        if current is None:
            raise ValueError("schedule is not available yet")
        plan = decode_week_schedule(current.week_1_raw, current.week_2_raw).days
        day_name = self.entity_description.key.removeprefix("schedule_").upper()
        plan[day_name] = parse_day_ranges_text(value)
        week_1, week_2 = encode_week_schedule(plan)
        await self.coordinator.client.set_weekly_schedule(week_1, week_2)
        await self.coordinator.async_request_refresh()
