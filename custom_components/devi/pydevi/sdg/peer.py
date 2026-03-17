from __future__ import annotations

import asyncio

from .grid import GridSession, SDGProtocolError


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
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def send(self, payload: bytes) -> None:
        async with self._op_lock:
            await self._run_with_reconnect(lambda: self._connection.send_peer_data(payload))

    async def request(self, payload: bytes, timeout: float = 1.0) -> list[bytes]:
        async with self._op_lock:
            return await self._run_with_reconnect(
                lambda: self._connection.request_peer_data(
                    payload,
                    first_chunk_timeout=timeout,
                    idle_timeout=min(timeout, 1.0),
                )
            )

    async def _run_with_reconnect(self, action):
        await self.connect()
        try:
            return await action()
        except (SDGProtocolError, ConnectionError, OSError, asyncio.IncompleteReadError):
            await self.disconnect()
            await self.connect()
            return await action()
