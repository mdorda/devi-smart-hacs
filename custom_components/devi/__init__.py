from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er, service
import voluptuous as vol

from .pydevi.dominion.client import PROBE_REQUEST, ThermostatClient
from .pydevi.dominion.schedule import decode_week_schedule
from .pydevi.models import HouseConfig, ThermostatInfo
from .pydevi.sdg.grid import GridSession
from .pydevi.sdg.link import PeerLink

from .const import (
    CONF_THERMOSTATS,
    DEFAULT_ENABLE_ADVANCED_ENTITIES,
    DOMAIN,
    OPT_ENABLE_ADVANCED_ENTITIES,
    PLATFORMS,
    SERVICE_GET_SCHEDULE,
    SERVICE_SET_WEEKLY_SCHEDULE,
    option_enabled,
)
from .coordinator import DeviThermostatCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# The thermostat sends its full state right after the tunnel connects, normally within two seconds.
READY_TIMEOUT_S = 30.0


@dataclass
class DeviRuntimeEntryData:
    house: HouseConfig
    grid: GridSession
    clients: dict[str, ThermostatClient]
    coordinators: dict[str, DeviThermostatCoordinator]
    platforms: list[str] | None = None


def _normalize_hex_payload(value: str) -> bytes:
    try:
        return bytes.fromhex("".join(value.split()))
    except ValueError as exc:
        raise ServiceValidationError(f"not a hex payload: {exc}") from exc


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
        state = runtime.clients[peer_id].state
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
    week_2 = _normalize_hex_payload(call.data["week_2_hex"])
    for _, runtime, peer_id in await _service_entities_for_call(hass, call):
        await runtime.coordinators[peer_id].async_command(runtime.clients[peer_id].set_weekly_schedule(week_1, week_2))


async def _async_register_services(hass: HomeAssistant) -> None:
    async def handle_get_schedule(call: ServiceCall) -> dict:
        return await _async_handle_get_schedule(hass, call)

    async def handle_set_weekly_schedule(call: ServiceCall) -> None:
        await _async_handle_set_weekly_schedule(hass, call)

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
                    vol.Required("week_2_hex"): cv.string,
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
        thermostat.peer_id: ThermostatClient(PeerLink(grid, thermostat.peer_id, probe=PROBE_REQUEST))
        for thermostat in thermostats
    }
    coordinators = {
        thermostat.peer_id: DeviThermostatCoordinator(hass, clients[thermostat.peer_id], thermostat.peer_id)
        for thermostat in thermostats
    }
    ready = await asyncio.gather(*(coordinator.async_start(READY_TIMEOUT_S) for coordinator in coordinators.values()))
    if not any(ready):
        # Nothing to show yet; Home Assistant retries the setup with its own backoff.
        await _async_stop(coordinators, grid)
        raise ConfigEntryNotReady("no DEVI thermostat sent its state")
    entry.runtime_data = DeviRuntimeEntryData(house=house, grid=grid, clients=clients, coordinators=coordinators)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async def _async_stop_on_shutdown(_event: Event) -> None:
        # Home Assistant does not unload entries when it stops, so the open tunnels are closed here.
        await _async_stop(coordinators, grid)

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop_on_shutdown))
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
        await _async_stop(entry.runtime_data.coordinators, entry.runtime_data.grid)
    return unload_ok


async def _async_stop(coordinators: dict[str, DeviThermostatCoordinator], grid: GridSession) -> None:
    for coordinator in coordinators.values():
        await coordinator.async_stop()
    await grid.disconnect()


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
