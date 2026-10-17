from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from time import monotonic

from .grid import GridSession, SDGProtocolError

LOGGER = logging.getLogger(__name__)

# The thermostat pushes its clock every second, so a few seconds without data means trouble.
WATCHDOG_INTERVAL_S = 1.0
# Silence after which one read request checks whether the tunnel still answers.
WATCHDOG_PROBE_S = 5.0
# Silence after which the tunnel counts as dead and is replaced.
WATCHDOG_TIMEOUT_S = 10.0
# Delays before successive reconnect attempts; the last one repeats.
RECONNECT_BACKOFF_S = (0.0, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
# A tunnel that lived this long resets the backoff, so a drop after hours reconnects immediately.
BACKOFF_RESET_S = 60.0
# Bounds establishing a tunnel including a new grid connection; the steps inside have their own timeouts.
CONNECT_TIMEOUT_S = 120.0
# How long a command waits for a tunnel that is being reconnected.
SEND_WAIT_S = 10.0
LINK_STATS_INTERVAL_S = 300.0


class PeerLink:
    """A tunnel to one thermostat that stays open, delivers every chunk it receives and reconnects itself."""

    def __init__(self, grid: GridSession, peer_id: str, probe: bytes, protocol: str = "dominion-1.0") -> None:
        self.grid = grid
        self.peer_id = peer_id
        self.protocol = protocol
        # A read request whose answer proves the tunnel is alive; the answer arrives like any push.
        self._probe = probe
        self._connection = None
        self._connected = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._on_chunk: Callable[[bytes], None] | None = None
        self._on_connection: Callable[[bool], None] | None = None
        self._connections = 0
        self._last_rx_at = 0.0
        self._probe_sent_at: float | None = None
        self._stats_chunks = 0
        self._stats_bytes = 0
        self._stats_max_gap = 0.0
        self._stats_since = 0.0

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self, on_chunk: Callable[[bytes], None], on_connection: Callable[[bool], None]) -> None:
        self._on_chunk = on_chunk
        self._on_connection = on_connection
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def wait_connected(self, timeout: float) -> bool:
        try:
            async with asyncio.timeout(timeout):
                await self._connected.wait()
        except TimeoutError:
            return False
        return True

    async def send(self, payload: bytes) -> None:
        if not await self.wait_connected(SEND_WAIT_S):
            raise SDGProtocolError("thermostat is not connected")
        connection = self._connection
        if connection is None:
            raise SDGProtocolError("thermostat disconnected")
        await connection.send_peer_data(payload)

    async def _run(self) -> None:
        attempt = 0
        while True:
            delay = RECONNECT_BACKOFF_S[min(attempt, len(RECONNECT_BACKOFF_S) - 1)]
            if attempt:
                LOGGER.debug("DEVI link reconnect peer=%s attempt=%d delay_s=%.0f", self.peer_id, attempt, delay)
                await asyncio.sleep(delay)
            try:
                connection = await self._connect()
                if connection is None:
                    attempt += 1
                    continue
                lifetime = await self._serve(connection)
            except Exception:
                # Whatever went wrong, the link must keep trying or the thermostat stays offline for good.
                LOGGER.exception("DEVI link failed unexpectedly peer=%s", self.peer_id)
                attempt += 1
                continue
            attempt = 0 if lifetime >= BACKOFF_RESET_S else attempt + 1

    async def _connect(self):
        started_at = monotonic()
        deadline = asyncio.timeout(CONNECT_TIMEOUT_S)
        try:
            async with deadline:
                connection = await self.grid.connect_to_remote(self.peer_id, self.protocol)
        except Exception as exc:
            LOGGER.warning(
                "DEVI link connect failed peer=%s elapsed_s=%.2f error=%s errno=%s",
                self.peer_id, monotonic() - started_at, type(exc).__name__, getattr(exc, "errno", None),
            )
            if isinstance(exc, TimeoutError) and deadline.expired():
                # A cancelled read may stop mid-frame, so the grid connection cannot be trusted any more.
                await self.grid.disconnect()
            return None
        # The connection monitor judges an idle tunnel by its unread buffer, which the reader keeps empty.
        stop_monitor = getattr(connection, "stop_monitor", None)
        if stop_monitor is not None:
            stop_monitor()
        self._connections += 1
        LOGGER.info(
            "DEVI link connected peer=%s connection_no=%d elapsed_s=%.2f",
            self.peer_id, self._connections, monotonic() - started_at,
        )
        return connection

    async def _serve(self, connection) -> float:
        """Run the tunnel until it fails and return how long it lived."""
        started_at = monotonic()
        self._last_rx_at = started_at
        self._probe_sent_at = None
        self._reset_stats(started_at)
        self._connection = connection
        self._connected.set()
        self._notify_connection(True)
        reader = asyncio.create_task(self._read_loop(connection))
        watchdog = asyncio.create_task(self._watchdog(connection))
        reason, error = "stopped", None
        try:
            done, _ = await asyncio.wait({reader, watchdog}, return_when=asyncio.FIRST_COMPLETED)
            try:
                reason, error = done.pop().result()
            except Exception as exc:
                # E.g. a frame that fails to decrypt: the stream cannot be trusted any more.
                reason, error = "error", type(exc).__name__
        finally:
            for task in (reader, watchdog):
                task.cancel()
            await asyncio.gather(reader, watchdog, return_exceptions=True)
            self._connected.clear()
            self._connection = None
            lifetime = monotonic() - started_at
            if reason != "stopped":
                diagnostic_context = getattr(connection, "diagnostic_context", None)
                LOGGER.info(
                    "DEVI link lost peer=%s reason=%s error=%s lifetime_s=%.1f last_rx_ago_s=%.1f %s",
                    self.peer_id, reason, error, lifetime, monotonic() - self._last_rx_at,
                    diagnostic_context() if diagnostic_context is not None else "",
                )
            try:
                await connection.close()
            except (OSError, TimeoutError):
                LOGGER.debug("Ignore transport error while closing peer link", exc_info=True)
            self._notify_connection(False)
        return lifetime

    async def _read_loop(self, connection) -> tuple[str, str | None]:
        while True:
            try:
                data = await connection.read_peer_chunk()
            except asyncio.IncompleteReadError:
                return "eof", None
            except ConnectionResetError as exc:
                return "reset", type(exc).__name__
            except (OSError, SDGProtocolError) as exc:
                return "error", type(exc).__name__
            now = monotonic()
            self._stats_max_gap = max(self._stats_max_gap, now - self._last_rx_at)
            self._stats_chunks += 1
            self._stats_bytes += len(data)
            self._last_rx_at = now
            if self._probe_sent_at is not None:
                LOGGER.debug("DEVI link probe answered peer=%s after_s=%.2f", self.peer_id, now - self._probe_sent_at)
                self._probe_sent_at = None
            if self._on_chunk is not None:
                try:
                    self._on_chunk(data)
                except Exception:
                    LOGGER.exception("DEVI link failed to handle a chunk peer=%s", self.peer_id)

    async def _watchdog(self, connection) -> tuple[str, str | None]:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            now = monotonic()
            silent = now - self._last_rx_at
            if silent >= WATCHDOG_TIMEOUT_S:
                return "watchdog", f"silent_s={silent:.1f}"
            if silent >= WATCHDOG_PROBE_S and self._probe_sent_at is None:
                LOGGER.debug("DEVI link probe peer=%s silent_s=%.1f", self.peer_id, silent)
                self._probe_sent_at = now
                try:
                    await connection.send_peer_data(self._probe)
                except (OSError, SDGProtocolError) as exc:
                    return "error", type(exc).__name__
            if now - self._stats_since >= LINK_STATS_INTERVAL_S:
                LOGGER.debug(
                    "DEVI link stats peer=%s interval_s=%.0f chunks=%d bytes=%d max_gap_s=%.2f",
                    self.peer_id, now - self._stats_since, self._stats_chunks, self._stats_bytes, self._stats_max_gap,
                )
                self._reset_stats(now)

    def _reset_stats(self, now: float) -> None:
        self._stats_since = now
        self._stats_chunks = 0
        self._stats_bytes = 0
        self._stats_max_gap = 0.0

    def _notify_connection(self, connected: bool) -> None:
        if self._on_connection is not None:
            try:
                self._on_connection(connected)
            except Exception:
                LOGGER.exception("DEVI link failed to report connection state peer=%s", self.peer_id)
