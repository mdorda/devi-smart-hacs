from __future__ import annotations

from collections.abc import Awaitable
import logging
from time import monotonic

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .pydevi.dominion.client import ThermostatClient
from .pydevi.models import ThermostatState
from .pydevi.sdg.grid import SDGProtocolError


LOGGER = logging.getLogger(__name__)

# A dropped tunnel is normally back within seconds; entities keep their last state that long
# instead of flapping to unavailable on every reconnect.
GRACE_PERIOD_S = 60.0


class DeviThermostatCoordinator(DataUpdateCoordinator[ThermostatState]):
    """Feeds Home Assistant from what the thermostat pushes; it never polls."""

    def __init__(self, hass: HomeAssistant, client: ThermostatClient, peer_id: str) -> None:
        self.client = client
        self.peer_id = peer_id
        self.connected = False
        self._cancel_grace: CALLBACK_TYPE | None = None
        self._unavailable_since: float | None = None
        self._disconnected_at: float | None = None
        super().__init__(hass, logger=LOGGER, name=f"devi-{peer_id}", update_interval=None)

    async def async_start(self, ready_timeout: float) -> bool:
        """Connect and wait for the full state; False leaves the entities unavailable until it arrives."""
        self.client.start(self._handle_update, self._handle_connection)
        if await self.client.wait_ready(ready_timeout):
            self.async_set_updated_data(self.client.state)
            return True
        self._mark_unavailable()
        return False

    async def async_stop(self) -> None:
        self._cancel_grace_timer()
        await self.client.stop()

    async def async_command(self, command: Awaitable[None]) -> None:
        """Send a command and show its expected result; the thermostat's push confirms or corrects it."""
        try:
            await command
        except (SDGProtocolError, OSError) as exc:
            raise HomeAssistantError(f"DEVI thermostat command failed: {exc}") from exc
        except ValueError as exc:
            raise ServiceValidationError(f"DEVI thermostat command refused: {exc}") from exc
        self.async_set_updated_data(self.client.state)

    async def _async_update_data(self) -> ThermostatState:
        # Only a manual refresh gets here; the state is kept current by pushes.
        if self._unavailable_since is not None:
            raise UpdateFailed("thermostat is not connected")
        return self.client.state

    @callback
    def _handle_update(self, state: ThermostatState) -> None:
        if self._unavailable_since is not None:
            self._mark_available()
        self.async_set_updated_data(state)

    @callback
    def _handle_connection(self, connected: bool) -> None:
        self.connected = connected
        if connected:
            self._disconnected_at = None
            self._cancel_grace_timer()
            # Data from before the outage is still current enough to show; the fresh state follows right away.
            if self._unavailable_since is not None and self.client.state.available:
                self._mark_available()
                self.async_set_updated_data(self.client.state)
                return
        elif self._unavailable_since is None and self._cancel_grace is None:
            self._disconnected_at = monotonic()
            self._cancel_grace = async_call_later(self.hass, GRACE_PERIOD_S, self._grace_expired)
        self.async_update_listeners()

    @callback
    def _grace_expired(self, _now) -> None:
        self._cancel_grace = None
        self._mark_unavailable()

    def _cancel_grace_timer(self) -> None:
        if self._cancel_grace is not None:
            self._cancel_grace()
            self._cancel_grace = None

    def _mark_unavailable(self) -> None:
        if self._unavailable_since is not None:
            return
        # Downtime counts from the drop, not from the end of the grace period.
        self._unavailable_since = self._disconnected_at or monotonic()
        LOGGER.info("DEVI entities unavailable peer=%s grace_s=%.0f", self.peer_id, GRACE_PERIOD_S)
        # async_set_update_error would log an error for what is an expected, self-healing outage.
        self.last_exception = UpdateFailed("thermostat is not connected")
        self.last_update_success = False
        self.async_update_listeners()

    def _mark_available(self) -> None:
        LOGGER.info(
            "DEVI entities available again peer=%s downtime_s=%.0f",
            self.peer_id, monotonic() - self._unavailable_since,
        )
        self._unavailable_since = None
