from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv, entity_registry as er, service
import voluptuous as vol

from .pydevi.dominion.client import ThermostatClient
from .pydevi.dominion.schedule import decode_week_schedule
from .pydevi.models import AwayInterval, HouseConfig, ThermostatInfo
from .pydevi.sdg.grid import GridSession
from .pydevi.sdg.peer import PeerSession

from .const import (
    CONF_THERMOSTATS,
    DEFAULT_ENABLE_ADVANCED_ENTITIES,
    DOMAIN,
    OPT_ENABLE_ADVANCED_ENTITIES,
    PLATFORMS,
    SERVICE_GET_SCHEDULE,
    SERVICE_SET_AWAY_INTERVAL,
    SERVICE_SET_WEEKLY_SCHEDULE,
    option_scan_interval_seconds,
    option_enabled,
)
from .coordinator import DeviThermostatCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class DeviRuntimeEntryData:
    house: HouseConfig
    grid: GridSession
    clients: dict[str, ThermostatClient]
    coordinators: dict[str, DeviThermostatCoordinator]
    platforms: list[str] | None = None


def _normalize_hex_payload(value: str) -> bytes:
    return bytes.fromhex("".join(value.split()))


def _normalize_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _service_entities_for_call(hass: HomeAssistant, call: ServiceCall) -> list[tuple[str, DeviRuntimeEntryData, str]]:
    registry = er.async_get(hass)
    entity_ids = await service.async_extract_entity_ids(hass, call)
    resolved: list[tuple[str, DeviRuntimeEntryData, str]] = []
    seen: set[tuple[str, str]] = set()
    for entity_id in entity_ids:
        entry = registry.async_get(entity_id)
        if entry is None or entry.platform != DOMAIN:
            continue
        peer_id = entry.unique_id.split("_", 1)[0]
        for config_entry in hass.config_entries.async_entries(DOMAIN):
            runtime = getattr(config_entry, "runtime_data", None)
            if runtime is None or peer_id not in runtime.clients:
                continue
            key = (config_entry.entry_id, peer_id)
            if key in seen:
                break
            seen.add(key)
            resolved.append((entity_id, runtime, peer_id))
            break
    return resolved


async def _async_handle_get_schedule(hass: HomeAssistant, call: ServiceCall) -> dict:
    result: dict[str, dict[str, object | None]] = {}
    for entity_id, runtime, peer_id in await _service_entities_for_call(hass, call):
        coordinator = runtime.coordinators[peer_id]
        await coordinator.async_request_refresh()
        state = coordinator.data or runtime.clients[peer_id].state
        schedule = state.weekly_schedule
        away = state.away_interval
        result[entity_id] = {
            "week_1_hex": schedule.week_1_raw.hex() if schedule else None,
            "week_2_hex": schedule.week_2_raw.hex() if schedule and schedule.week_2_raw is not None else None,
            "days": decode_week_schedule(schedule.week_1_raw, schedule.week_2_raw).days if schedule else None,
            "away_planned": state.away_planned,
            "away_start": away.start.isoformat() if away and away.start else None,
            "away_end": away.end.isoformat() if away and away.end else None,
        }
    return {"schedules": result}


async def _async_handle_set_weekly_schedule(hass: HomeAssistant, call: ServiceCall) -> None:
    week_1 = _normalize_hex_payload(call.data["week_1_hex"])
    week_2 = _normalize_hex_payload(call.data["week_2_hex"]) if call.data.get("week_2_hex") else None
    for _, runtime, peer_id in await _service_entities_for_call(hass, call):
        await runtime.clients[peer_id].set_weekly_schedule(week_1, week_2)
        await runtime.coordinators[peer_id].async_request_refresh()


async def _async_handle_set_away_interval(hass: HomeAssistant, call: ServiceCall) -> None:
    start = _normalize_datetime(call.data.get("start"))
    end = _normalize_datetime(call.data.get("end"))
    away = None if start is None and end is None else AwayInterval(start=start, end=end)
    is_planned = call.data.get("is_planned", True)
    for _, runtime, peer_id in await _service_entities_for_call(hass, call):
        await runtime.clients[peer_id].set_away_interval(away, is_planned=is_planned)
        await runtime.coordinators[peer_id].async_request_refresh()


async def _async_register_services(hass: HomeAssistant) -> None:
    async def handle_get_schedule(call: ServiceCall) -> dict:
        return await _async_handle_get_schedule(hass, call)

    async def handle_set_weekly_schedule(call: ServiceCall) -> None:
        await _async_handle_set_weekly_schedule(hass, call)

    async def handle_set_away_interval(call: ServiceCall) -> None:
        await _async_handle_set_away_interval(hass, call)

    if not hass.services.has_service(DOMAIN, SERVICE_GET_SCHEDULE):
        _LOGGER.debug("Registering service %s.%s", DOMAIN, SERVICE_GET_SCHEDULE)
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_SCHEDULE,
            handle_get_schedule,
            schema=vol.Schema({vol.Required("entity_id"): cv.entity_ids}),
            supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SET_WEEKLY_SCHEDULE):
        _LOGGER.debug("Registering service %s.%s", DOMAIN, SERVICE_SET_WEEKLY_SCHEDULE)
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_WEEKLY_SCHEDULE,
            handle_set_weekly_schedule,
            schema=vol.Schema(
                {
                    vol.Required("entity_id"): cv.entity_ids,
                    vol.Required("week_1_hex"): cv.string,
                    vol.Optional("week_2_hex"): cv.string,
                }
            ),
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SET_AWAY_INTERVAL):
        _LOGGER.debug("Registering service %s.%s", DOMAIN, SERVICE_SET_AWAY_INTERVAL)
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_AWAY_INTERVAL,
            handle_set_away_interval,
            schema=vol.Schema(
                {
                    vol.Required("entity_id"): cv.entity_ids,
                    vol.Optional("start"): vol.Any(None, cv.string),
                    vol.Optional("end"): vol.Any(None, cv.string),
                    vol.Optional("is_planned", default=True): bool,
                }
            ),
        )


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    await _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    thermostats = [
        ThermostatInfo(
            peer_id=item["peer_id"],
            room_name=item["room_name"],
            serial_number=item.get("serial_number"),
            zone=item.get("zone"),
            metadata=item.get("metadata", {}),
        )
        for item in entry.data[CONF_THERMOSTATS]
    ]
    house = HouseConfig(
        house_id=entry.data.get("house_id"),
        house_name=entry.data.get("house_name"),
        user_name=entry.data["user_name"],
        private_key=entry.data["private_key"],
        thermostats=thermostats,
    )
    grid = GridSession(private_key_hex=entry.data["private_key"], user_name=entry.data["user_name"])
    clients = {
        thermostat.peer_id: ThermostatClient(PeerSession(grid, thermostat.peer_id))
        for thermostat in thermostats
    }
    coordinators = {
        thermostat.peer_id: DeviThermostatCoordinator(
            hass,
            clients[thermostat.peer_id],
            thermostat.peer_id,
            scan_interval_seconds=option_scan_interval_seconds(entry.options),
        )
        for thermostat in thermostats
    }
    entry.runtime_data = DeviRuntimeEntryData(house=house, grid=grid, clients=clients, coordinators=coordinators)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await _async_register_services(hass)
    platforms = list(PLATFORMS)
    if option_enabled(entry.options, OPT_ENABLE_ADVANCED_ENTITIES, DEFAULT_ENABLE_ADVANCED_ENTITIES):
        platforms.extend(["number", "switch", "text", "datetime"])
    _LOGGER.debug("Setting up DEVI entry %s with platforms=%s", entry.entry_id, platforms)
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    entry.runtime_data.platforms = platforms
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    platforms = getattr(entry.runtime_data, "platforms", PLATFORMS)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unload_ok and entry.runtime_data:
        for client in entry.runtime_data.clients.values():
            await client.disconnect()
        await entry.runtime_data.grid.disconnect()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
