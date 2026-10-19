from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.devi import coordinator as coordinator_module
from custom_components.devi.coordinator import GRACE_PERIOD_S, DeviThermostatCoordinator
from custom_components.devi.pydevi.models import ThermostatState
from custom_components.devi.pydevi.sdg.grid import SDGProtocolError


class FakeClient:
    def __init__(self, ready: bool = True) -> None:
        self.state = ThermostatState()
        self.ready = ready
        self.on_update = None
        self.on_connection = None
        self.stopped = False

    def start(self, on_update, on_connection) -> None:
        self.on_update = on_update
        self.on_connection = on_connection
        on_connection(True)
        if self.ready:
            self.state.available = True
            self.state.room_temperature_c = 21.0

    async def wait_ready(self, timeout: float) -> bool:
        return self.ready

    async def stop(self) -> None:
        self.stopped = True

    def push(self, room_temperature_c: float) -> None:
        self.state.available = True
        self.state.room_temperature_c = room_temperature_c
        self.on_update(self.state)


async def started(hass, ready: bool = True) -> tuple[DeviThermostatCoordinator, FakeClient]:
    client = FakeClient(ready)
    coordinator = DeviThermostatCoordinator(hass, client, "peer-1")
    await coordinator.async_start(ready_timeout=1)
    return coordinator, client


def pass_time(hass, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))


async def test_ready_thermostat_starts_available(hass) -> None:
    coordinator, client = await started(hass)

    assert coordinator.last_update_success
    assert coordinator.data is client.state
    assert coordinator.connected


async def test_push_updates_data(hass) -> None:
    coordinator, client = await started(hass)
    updates = []
    coordinator.async_add_listener(lambda: updates.append(coordinator.data.room_temperature_c))

    client.push(22.5)

    assert updates == [22.5]


async def test_short_outage_keeps_entities_available(hass) -> None:
    coordinator, client = await started(hass)
    client.on_connection(False)
    pass_time(hass, GRACE_PERIOD_S / 2)
    client.on_connection(True)
    pass_time(hass, GRACE_PERIOD_S * 2)
    await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert coordinator.connected


async def test_long_outage_makes_entities_unavailable_without_an_error(hass, caplog) -> None:
    coordinator, client = await started(hass)
    caplog.set_level(logging.INFO, logger=coordinator_module.__name__)

    client.on_connection(False)
    pass_time(hass, GRACE_PERIOD_S - 1)
    await hass.async_block_till_done()
    assert coordinator.last_update_success

    pass_time(hass, GRACE_PERIOD_S + 1)
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    assert "DEVI entities unavailable peer=peer-1" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


async def test_reconnect_after_long_outage_restores_last_state(hass, caplog) -> None:
    coordinator, client = await started(hass)
    client.on_connection(False)
    pass_time(hass, GRACE_PERIOD_S + 1)
    await hass.async_block_till_done()
    caplog.set_level(logging.INFO, logger=coordinator_module.__name__)

    client.on_connection(True)

    assert coordinator.last_update_success
    assert coordinator.data.room_temperature_c == 21.0
    assert "DEVI entities available again peer=peer-1" in caplog.text


async def test_repeated_drops_keep_the_first_grace_deadline(hass) -> None:
    coordinator, client = await started(hass)
    client.on_connection(False)
    pass_time(hass, GRACE_PERIOD_S / 2)
    client.on_connection(False)
    pass_time(hass, GRACE_PERIOD_S + 1)
    await hass.async_block_till_done()

    assert not coordinator.last_update_success


async def test_thermostat_without_state_starts_unavailable(hass) -> None:
    coordinator, client = await started(hass, ready=False)

    assert not coordinator.last_update_success

    client.push(20.0)
    assert coordinator.last_update_success
    assert coordinator.data.room_temperature_c == 20.0


async def test_reconnect_without_any_state_waits_for_data(hass) -> None:
    coordinator, client = await started(hass, ready=False)
    client.on_connection(False)
    client.on_connection(True)

    assert not coordinator.last_update_success


async def test_manual_refresh_while_unavailable_keeps_entities_unavailable(hass) -> None:
    coordinator, _ = await started(hass, ready=False)

    await coordinator.async_refresh()

    assert not coordinator.last_update_success


async def test_command_publishes_the_expected_state(hass) -> None:
    coordinator, client = await started(hass)
    updates = []
    coordinator.async_add_listener(lambda: updates.append(coordinator.data.room_temperature_c))

    async def command() -> None:
        client.state.room_temperature_c = 23.0

    await coordinator.async_command(command())

    assert updates == [23.0]


@pytest.mark.parametrize("error", [SDGProtocolError("thermostat is not connected"), OSError("broken pipe")])
async def test_failed_command_raises_a_home_assistant_error(hass, error) -> None:
    coordinator, _ = await started(hass)

    async def command() -> None:
        raise error

    with pytest.raises(HomeAssistantError):
        await coordinator.async_command(command())


async def test_stop_stops_the_client(hass) -> None:
    coordinator, client = await started(hass)
    client.on_connection(False)

    await coordinator.async_stop()
    pass_time(hass, GRACE_PERIOD_S + 1)
    await hass.async_block_till_done()

    assert client.stopped
    assert coordinator.last_update_success


async def test_refused_command_is_a_validation_error(hass) -> None:
    coordinator, _ = await started(hass)

    async def refused() -> None:
        raise ValueError("a planned vacation needs both a start and an end")

    with pytest.raises(ServiceValidationError, match="refused"):
        await coordinator.async_command(refused())
