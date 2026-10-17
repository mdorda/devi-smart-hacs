from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.devi.binary_sensor import DeviLinkEntity
from custom_components.devi.coordinator import GRACE_PERIOD_S, DeviThermostatCoordinator
from custom_components.devi.pydevi.models import ThermostatState


class _Client:
    def __init__(self) -> None:
        self.state = ThermostatState(available=True)
        self.on_connection = None

    def start(self, on_update, on_connection) -> None:
        self.on_connection = on_connection
        on_connection(True)

    async def wait_ready(self, timeout: float) -> bool:
        return True

    async def stop(self) -> None:
        pass


async def _entity(hass) -> tuple[DeviLinkEntity, DeviThermostatCoordinator, _Client]:
    client = _Client()
    coordinator = DeviThermostatCoordinator(hass, client, "peer-1")
    await coordinator.async_start(ready_timeout=1)
    entity = DeviLinkEntity(coordinator, SimpleNamespace(peer_id="peer-1", room_name="Bathroom", serial_number=None))
    return entity, coordinator, client


async def test_on_while_connected(hass) -> None:
    entity, _, _ = await _entity(hass)

    assert entity.available
    assert entity.is_on


async def test_off_immediately_when_the_tunnel_drops(hass) -> None:
    entity, coordinator, client = await _entity(hass)
    updates = []
    coordinator.async_add_listener(lambda: updates.append(entity.is_on))

    client.on_connection(False)

    assert updates == [False]
    assert coordinator.last_update_success
    await coordinator.async_stop()


async def test_stays_available_after_the_grace_period(hass) -> None:
    entity, coordinator, client = await _entity(hass)
    client.on_connection(False)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=GRACE_PERIOD_S + 1))
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert entity.available
    assert not entity.is_on


async def test_on_again_after_reconnect(hass) -> None:
    entity, _, client = await _entity(hass)
    client.on_connection(False)
    client.on_connection(True)

    assert entity.is_on
