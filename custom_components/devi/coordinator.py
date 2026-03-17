from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .pydevi.dominion.client import ThermostatClient
from .pydevi.models import ThermostatState

from .const import DEFAULT_SCAN_INTERVAL_SECONDS


LOGGER = logging.getLogger(__name__)


class DeviThermostatCoordinator(DataUpdateCoordinator[ThermostatState]):
    def __init__(
        self,
        hass: HomeAssistant,
        client: ThermostatClient,
        peer_id: str,
        scan_interval_seconds: int = DEFAULT_SCAN_INTERVAL_SECONDS,
    ) -> None:
        self.client = client
        super().__init__(
            hass,
            logger=LOGGER,
            name=f"devi-{peer_id}",
            update_interval=timedelta(seconds=scan_interval_seconds),
        )

    async def _async_update_data(self) -> ThermostatState:
        try:
            return await self.client.refresh_state()
        except Exception as exc:
            raise UpdateFailed(f"failed to refresh thermostat state: {exc}") from exc
