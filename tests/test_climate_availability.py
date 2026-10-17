from __future__ import annotations

from types import SimpleNamespace

from custom_components.devi.climate import DeviClimateEntity
from custom_components.devi.coordinator import DeviThermostatCoordinator
from custom_components.devi.pydevi.models import ThermostatState


class _Client:
    def __init__(self) -> None:
        self.state = ThermostatState(available=True)

    def start(self, on_update, on_connection) -> None:
        pass

    async def wait_ready(self, timeout: float) -> bool:
        return True


def _entity(hass) -> tuple[DeviClimateEntity, DeviThermostatCoordinator]:
    coordinator = DeviThermostatCoordinator(hass, _Client(), "peer-1")
    entity = DeviClimateEntity(coordinator, SimpleNamespace(peer_id="peer-1", room_name="Bathroom", serial_number=None))
    return entity, coordinator


async def test_available_with_data(hass) -> None:
    entity, coordinator = _entity(hass)
    await coordinator.async_start(ready_timeout=1)

    assert entity.available


async def test_unavailable_before_first_data(hass) -> None:
    entity, _ = _entity(hass)

    assert not entity.available


async def test_unavailable_when_coordinator_is_despite_last_data(hass) -> None:
    entity, coordinator = _entity(hass)
    await coordinator.async_start(ready_timeout=1)
    coordinator.last_update_success = False

    assert coordinator.data is not None and coordinator.data.available
    assert not entity.available
