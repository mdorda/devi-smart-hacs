from __future__ import annotations

from collections.abc import MutableMapping

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .pydevi.discovery import DiscoverySession
from .pydevi.sdg.grid import PairingRefusedError

from .const import (
    CONF_CLIENT_NAME,
    CONF_HOUSE_ID,
    CONF_HOUSE_NAME,
    CONF_PEER_ID,
    CONF_PRIVATE_KEY,
    CONF_ROOM_NAME,
    CONF_SERIAL_NUMBER,
    CONF_THERMOSTATS,
    CONF_USER_NAME,
    DEFAULT_ENABLE_ADVANCED_ENTITIES,
    DEFAULT_ENABLE_DIAGNOSTIC_ENTITIES,
    DOMAIN,
    OPT_ENABLE_ADVANCED_ENTITIES,
    OPT_ENABLE_DIAGNOSTIC_ENTITIES,
    option_enabled,
)


class DeviConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovery: DiscoverySession | None = None

    def _session_store(self) -> MutableMapping[str, DiscoverySession]:
        domain_data = self.hass.data.setdefault(DOMAIN, {})
        return domain_data.setdefault("discovery_sessions", {})

    async def _get_discovery_session(self, client_name: str) -> DiscoverySession:
        store = self._session_store()
        session = store.get(self.flow_id)
        if session is None or session.client_name != client_name:
            if session is not None:
                await session.disconnect()
            session = DiscoverySession(client_name=client_name)
            store[self.flow_id] = session
        self._discovery = session
        return session

    async def _clear_discovery_session(self) -> None:
        store = self._session_store()
        session = store.pop(self.flow_id, None)
        if session is not None:
            await session.disconnect()
        self._discovery = None

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                discovery = await self._get_discovery_session(user_input[CONF_CLIENT_NAME])
                house = await discovery.pair_and_discover(user_input["otp"])
            except PairingRefusedError:
                errors["base"] = "invalid_otp"
            except NotImplementedError:
                errors["base"] = "transport_not_ready"
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                await self._clear_discovery_session()
                return self.async_create_entry(
                    title=house.house_name or house.user_name,
                    data={
                        CONF_USER_NAME: house.user_name,
                        CONF_PRIVATE_KEY: house.private_key,
                        CONF_HOUSE_ID: house.house_id,
                        CONF_HOUSE_NAME: house.house_name,
                        CONF_THERMOSTATS: [
                            {
                                CONF_PEER_ID: thermostat.peer_id,
                                CONF_ROOM_NAME: thermostat.room_name,
                                CONF_SERIAL_NUMBER: thermostat.serial_number,
                                "zone": thermostat.zone,
                                "metadata": thermostat.metadata,
                            }
                            for thermostat in house.thermostats
                        ],
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_CLIENT_NAME, default="ha-user"): str,
                vol.Required("otp"): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> "DeviOptionsFlow":
        return DeviOptionsFlow(config_entry)


class DeviOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self._config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    OPT_ENABLE_ADVANCED_ENTITIES,
                    default=option_enabled(options, OPT_ENABLE_ADVANCED_ENTITIES, DEFAULT_ENABLE_ADVANCED_ENTITIES),
                ): bool,
                vol.Required(
                    OPT_ENABLE_DIAGNOSTIC_ENTITIES,
                    default=option_enabled(
                        options,
                        OPT_ENABLE_DIAGNOSTIC_ENTITIES,
                        DEFAULT_ENABLE_DIAGNOSTIC_ENTITIES,
                    ),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
