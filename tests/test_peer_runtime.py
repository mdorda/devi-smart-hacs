import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from pydevi.sdg.grid import SDGProtocolError, _RemoteConnection, MODE_PEER
from pydevi.sdg.peer import PeerSession


class FakeConnection:
    def __init__(self) -> None:
        self.connected = True
        self.closed = False
        self.fail_send_once = False
        self.fail_request_once = False
        self.max_in_flight = 0
        self._in_flight = 0

    async def close(self) -> None:
        self.closed = True
        self.connected = False

    async def send_peer_data(self, payload: bytes) -> None:
        if self.fail_send_once:
            self.fail_send_once = False
            self.connected = False
            raise OSError("socket closed")

    async def request_peer_data(self, payload: bytes, first_chunk_timeout: float, idle_timeout: float) -> list[bytes]:
        if self.fail_request_once:
            self.fail_request_once = False
            self.connected = False
            raise SDGProtocolError("stale connection")
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        await asyncio.sleep(0)
        self._in_flight -= 1
        return [b"ok"]


class FakeGrid:
    def __init__(self, connections: list[FakeConnection]) -> None:
        self._connections = connections
        self.calls = 0

    async def connect_to_remote(self, peer_id: str, protocol: str):
        self.calls += 1
        return self._connections[self.calls - 1]


class PeerRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_survives_broken_pipe_during_close(self) -> None:
        first = FakeConnection()
        first.fail_request_once = True
        first.close = AsyncMock(side_effect=BrokenPipeError("broken pipe on close"))
        second = FakeConnection()
        grid = FakeGrid([first, second])
        peer = PeerSession(grid, "peer")
        self.assertEqual(await peer.request(b"read"), [b"ok"])
        self.assertEqual(grid.calls, 2)

    async def test_empty_response_reconnects(self) -> None:
        first = FakeConnection()
        first.request_peer_data = AsyncMock(return_value=[])
        second = FakeConnection()
        grid = FakeGrid([first, second])
        peer = PeerSession(grid, "peer")
        self.assertEqual(await peer.request(b"read"), [b"ok"])
        self.assertTrue(first.closed)
        self.assertEqual(grid.calls, 2)

    async def test_failed_retry_discards_connection_for_next_poll(self) -> None:
        first, second, third = FakeConnection(), FakeConnection(), FakeConnection()
        first.fail_request_once = second.fail_request_once = True
        grid = FakeGrid([first, second, third])
        peer = PeerSession(grid, "peer")
        with self.assertRaises(SDGProtocolError):
            await peer.request(b"read")
        self.assertIsNone(peer._connection)
        self.assertTrue(second.closed)
        self.assertEqual(await peer.request(b"read"), [b"ok"])

    async def test_remote_close_marks_disconnected_even_if_wait_closed_fails(self) -> None:
        writer = Mock()
        writer.wait_closed = AsyncMock(side_effect=BrokenPipeError("closed socket"))
        conn = _RemoteConnection(
            reader=Mock(), writer=writer, client_public_key=b"", client_secret=b"",
            mode=MODE_PEER, protocol="dominion-1.0", connected=True,
        )
        await conn.close()
        self.assertFalse(conn.connected)
        writer.close.assert_called_once()

    async def test_send_reconnects_once_on_connection_error(self) -> None:
        first = FakeConnection()
        first.fail_send_once = True
        second = FakeConnection()
        grid = FakeGrid([first, second])
        peer = PeerSession(grid, "peer")

        await peer.send(b"payload")

        self.assertEqual(grid.calls, 2)
        self.assertTrue(first.closed)
        self.assertFalse(second.closed)

    async def test_request_reconnects_once_on_protocol_error(self) -> None:
        first = FakeConnection()
        first.fail_request_once = True
        second = FakeConnection()
        grid = FakeGrid([first, second])
        peer = PeerSession(grid, "peer")

        response = await peer.request(b"payload", timeout=1.0)

        self.assertEqual(response, [b"ok"])
        self.assertEqual(grid.calls, 2)
        self.assertTrue(first.closed)

    async def test_request_operations_are_serialized(self) -> None:
        connection = FakeConnection()
        grid = FakeGrid([connection])
        peer = PeerSession(grid, "peer")

        await asyncio.gather(
            peer.request(b"a", timeout=1.0),
            peer.request(b"b", timeout=1.0),
        )

        self.assertEqual(connection.max_in_flight, 1)


if __name__ == "__main__":
    unittest.main()
