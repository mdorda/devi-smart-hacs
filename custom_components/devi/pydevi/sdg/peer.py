from __future__ import annotations

import asyncio
import logging
from time import monotonic

from .grid import GridSession, SDGProtocolError

LOGGER = logging.getLogger(__name__)


class PeerSession:
    def __init__(self, grid: GridSession, peer_id: str, protocol: str = "dominion-1.0") -> None:
        self.grid = grid
        self.peer_id = peer_id
        self.protocol = protocol
        self._connection = None
        self._op_lock = asyncio.Lock()
        self._connections = 0

    @property
    def connected(self) -> bool:
        return self._connection is not None and self._connection.connected

    async def connect(self) -> None:
        if self._connection is None or not self._connection.connected:
            LOGGER.debug(
                "DEVI peer connecting peer=%s reason=%s",
                self.peer_id, "initial" if self._connections == 0 else "reconnect",
            )
            started_at = monotonic()
            try:
                self._connection = await self.grid.connect_to_remote(self.peer_id, self.protocol)
            except Exception as exc:
                LOGGER.warning(
                    "DEVI peer connect failed peer=%s elapsed_s=%.2f error=%s errno=%s",
                    self.peer_id, monotonic() - started_at, type(exc).__name__, getattr(exc, "errno", None),
                )
                raise
            self._connections += 1
            start_monitor = getattr(self._connection, "start_monitor", None)
            if start_monitor is not None:
                start_monitor()
            LOGGER.info(
                "DEVI peer connected peer=%s connection_no=%d elapsed_s=%.2f",
                self.peer_id, self._connections, monotonic() - started_at,
            )

    async def disconnect(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            diagnostic_context = getattr(connection, "diagnostic_context", None)
            LOGGER.debug(
                "DEVI peer discard connection peer=%s %s",
                self.peer_id, diagnostic_context() if diagnostic_context is not None else "",
            )
            try:
                await connection.close()
            except (OSError, TimeoutError):
                LOGGER.debug("Ignore transport error while discarding peer connection", exc_info=True)

    async def send(self, payload: bytes) -> None:
        async with self._op_lock:
            await self._run_with_reconnect(lambda: self._connection.send_peer_data(payload), operation="send")

    async def request(self, payload: bytes, timeout: float = 1.0) -> list[bytes]:
        async with self._op_lock:
            async def read_response():
                responses = await self._connection.request_peer_data(
                    payload,
                    first_chunk_timeout=timeout,
                    idle_timeout=min(timeout, 1.0),
                )
                if not responses:
                    raise SDGProtocolError("peer returned no response")
                return responses

            return await self._run_with_reconnect(read_response, operation="request")

    async def _run_with_reconnect(self, action, operation: str):
        for attempt in range(2):
            await self.connect()
            started_at = monotonic()
            try:
                result = await action()
                if attempt:
                    LOGGER.info("DEVI peer recovered peer=%s operation=%s", self.peer_id, operation)
                return result
            except (SDGProtocolError, ConnectionError, OSError, asyncio.IncompleteReadError) as exc:
                LOGGER.warning(
                    "DEVI peer operation failed peer=%s operation=%s attempt=%d elapsed_s=%.2f error=%s errno=%s recovery=%s",
                    self.peer_id, operation, attempt + 1, monotonic() - started_at,
                    type(exc).__name__, getattr(exc, "errno", None),
                    "reconnect" if attempt == 0 else "next_poll",
                )
                await self.disconnect()
                if attempt == 1:
                    raise
