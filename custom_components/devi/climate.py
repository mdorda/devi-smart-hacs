from __future__ import annotations

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .pydevi.models import HVACMode as DeviHVACMode, PresetMode

from . import DeviRuntimeEntryData
from .entity import DeviCoordinatorEntity


PRESET_TO_HA = {
    PresetMode.MANUAL: "manual",
    PresetMode.SCHEDULE: "schedule",
    PresetMode.OVERRIDE: "override",
    PresetMode.VACATION: "vacation",
    PresetMode.PAUSE: "pause",
    PresetMode.OFF: "off",
}

HA_TO_PRESET = {value: key for key, value in PRESET_TO_HA.items()}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime: DeviRuntimeEntryData = entry.runtime_data
    entities = []
    for thermostat in runtime.house.thermostats:
        coordinator = runtime.coordinators[thermostat.peer_id]
        entities.append(DeviClimateEntity(coordinator, thermostat))
    async_add_entities(entities)


class DeviClimateEntity(DeviCoordinatorEntity, ClimateEntity):
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_preset_modes = list(HA_TO_PRESET.keys())
    _attr_target_temperature_step = 0.5
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE

    def __init__(self, coordinator, thermostat) -> None:
        super().__init__(coordinator, thermostat)
        self._attr_unique_id = f"{thermostat.peer_id}_climate"
        self._attr_name = thermostat.room_name

    @property
    def available(self) -> bool:
        return bool(self.coordinator.data and self.coordinator.data.available)

    @property
    def current_temperature(self) -> float | None:
        return self.coordinator.data.current_temperature_c if self.coordinator.data else None

    @property
    def target_temperature(self) -> float | None:
        return self.coordinator.data.target_temperature_c if self.coordinator.data else None

    @property
    def hvac_mode(self) -> HVACMode:
        if not self.coordinator.data or self.coordinator.data.hvac_mode.value == "off":
            return HVACMode.OFF
        return HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction | None:
        if not self.coordinator.data or self.coordinator.data.heating_active is None:
            return None
        return HVACAction.HEATING if self.coordinator.data.heating_active else HVACAction.IDLE

    @property
    def preset_mode(self) -> str | None:
        preset = self.coordinator.data.preset_mode if self.coordinator.data else None
        return PRESET_TO_HA.get(preset) if preset else None

    async def async_set_temperature(self, **kwargs) -> None:
        if "temperature" in kwargs:
            await self.coordinator.client.set_target_temperature(kwargs["temperature"])
            await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.coordinator.client.set_hvac_mode(DeviHVACMode(hvac_mode.value))
        await self.coordinator.async_request_refresh()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self.coordinator.client.set_preset_mode(HA_TO_PRESET[preset_mode])
        await self.coordinator.async_request_refresh()
