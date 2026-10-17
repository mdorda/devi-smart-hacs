from __future__ import annotations

from types import SimpleNamespace

from custom_components.devi.climate import DeviClimateEntity
from custom_components.devi.coordinator import DeviThermostatCoordinator
from custom_components.devi.pydevi.models import ThermostatState


class _Client:
    def __init__(self) -> None:
        self.state = ThermostatState(available=True)
        self.fail = False

    async def refresh_state(self) -> ThermostatState:
        if self.fail:
            raise OSError("connection lost")
        return self.state


def _entity(hass) -> tuple[DeviClimateEntity, DeviThermostatCoordinator, _Client]:
    client = _Client()
    coordinator = DeviThermostatCoordinator(hass, client, "peer-1")
    entity = DeviClimateEntity(coordinator, SimpleNamespace(peer_id="peer-1", room_name="Bathroom", serial_number=None))
    return entity, coordinator, client


async def test_available_with_fresh_data(hass) -> None:
    entity, coordinator, _ = _entity(hass)
    await coordinator.async_refresh()

    assert entity.available


async def test_unavailable_before_first_data(hass) -> None:
    entity, _, _ = _entity(hass)

    assert not entity.available


async def test_unavailable_when_update_fails_despite_last_data(hass) -> None:
    entity, coordinator, client = _entity(hass)
    await coordinator.async_refresh()
    client.fail = True
    await coordinator.async_refresh()

    assert coordinator.data is not None and coordinator.data.available
    assert not entity.available


async def test_available_again_after_recovery(hass) -> None:
    entity, coordinator, client = _entity(hass)
    client.fail = True
    await coordinator.async_refresh()
    client.fail = False
    await coordinator.async_refresh()

    assert entity.available
