from __future__ import annotations

import asyncio
import logging

from .grid import GridSession, SDGProtocolError

LOGGER = logging.getLogger(__name__)


class PeerSession:
    def __init__(self, grid: GridSession, peer_id: str, protocol: str = "dominion-1.0") -> None:
        self.grid = grid
        self.peer_id = peer_id
        self.protocol = protocol
        self._connection = None
        self._op_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connection is not None and self._connection.connected

    async def connect(self) -> None:
        if self._connection is None or not self._connection.connected:
            self._connection = await self.grid.connect_to_remote(self.peer_id, self.protocol)

    async def disconnect(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                await connection.close()
            except (OSError, TimeoutError):
                LOGGER.debug("Ignore transport error while discarding peer connection", exc_info=True)

    async def send(self, payload: bytes) -> None:
        async with self._op_lock:
            await self._run_with_reconnect(lambda: self._connection.send_peer_data(payload))

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

            return await self._run_with_reconnect(read_response)

    async def _run_with_reconnect(self, action):
        for attempt in range(2):
            await self.connect()
            try:
                return await action()
            except (SDGProtocolError, ConnectionError, OSError, asyncio.IncompleteReadError):
                await self.disconnect()
                if attempt == 1:
                    raise
