from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.devi.const import DOMAIN
from custom_components.devi.pydevi.dominion.client import ThermostatClient

PEER_ID = "aa" * 32


class FakeLink:
    instances: list["FakeLink"] = []

    def __init__(self, grid, peer_id: str, probe: bytes) -> None:
        self.peer_id = peer_id
        self.started = False
        self.stopped = False
        FakeLink.instances.append(self)

    def start(self, on_chunk, on_connection) -> None:
        self.started = True
        on_connection(True)

    async def stop(self) -> None:
        self.stopped = True

    async def send(self, payload: bytes) -> None:
        pass


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    FakeLink.instances.clear()
    with patch("custom_components.devi.PeerLink", FakeLink):
        yield


def config_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "user_name": "ha-user",
            "private_key": "11" * 32,
            "thermostats": [{"peer_id": PEER_ID, "room_name": "Bathroom"}],
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_starts_the_link_without_polling(hass) -> None:
    entry = config_entry(hass)
    with patch.object(ThermostatClient, "wait_ready", return_value=True):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert FakeLink.instances[0].started
    coordinator = entry.runtime_data.coordinators[PEER_ID]
    assert coordinator.update_interval is None
    assert hass.states.async_entity_ids("climate")


async def test_setup_is_retried_when_no_thermostat_sends_its_state(hass) -> None:
    entry = config_entry(hass)
    with patch.object(ThermostatClient, "wait_ready", return_value=False):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert FakeLink.instances[0].stopped


async def test_unload_stops_the_link(hass) -> None:
    entry = config_entry(hass)
    with patch.object(ThermostatClient, "wait_ready", return_value=True):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert FakeLink.instances[0].stopped


async def test_home_assistant_stop_closes_the_link(hass) -> None:
    entry = config_entry(hass)
    with patch.object(ThermostatClient, "wait_ready", return_value=True):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert FakeLink.instances[0].stopped
