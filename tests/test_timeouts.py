import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from pydevi.sdg import grid
from pydevi.sdg.control import PeerReply
from pydevi.sdg.grid import MODE_GRID, GridSession, _RemoteConnection


def make_connection() -> _RemoteConnection:
    writer = Mock()
    writer.is_closing.return_value = False
    writer.get_extra_info.return_value = None
    writer.wait_closed = AsyncMock()
    return _RemoteConnection(
        reader=asyncio.StreamReader(), writer=writer, client_public_key=b"", client_secret=b"",
        mode=MODE_GRID, protocol="grid", connected=True,
    )


async def hang(*args, **kwargs):
    await asyncio.sleep(10)


class HandshakeTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_hung_handshake_times_out_and_closes_socket(self) -> None:
        conn = make_connection()
        with (
            patch.object(grid, "HANDSHAKE_TIMEOUT_S", 0.05),
            patch.object(_RemoteConnection, "run_handshake", hang),
            self.assertLogs(grid.LOGGER, "WARNING") as logs,
        ):
            with self.assertRaises(TimeoutError):
                await conn.run_monitored_handshake("peer_handshake")
        self.assertIn("SDG handshake timeout operation=peer_handshake", logs.output[0])
        self.assertFalse(conn.connected)
        conn.writer.close.assert_called_once()
        self.assertIsNone(conn._monitor_task)
        self.assertIsNone(conn._busy_since)


class WriteTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_write_raises_os_error(self) -> None:
        conn = make_connection()
        conn.writer.drain = hang
        with patch.object(grid, "WRITE_TIMEOUT_S", 0.05), self.assertLogs(grid.LOGGER, "DEBUG"):
            with self.assertRaises(OSError):
                await conn.send_raw(b"data")


class GridReplyTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_grid_reply_resets_connection_and_retries(self) -> None:
        session = GridSession()
        connections = [make_connection(), make_connection()]

        async def connect() -> None:
            if session._grid_conn is None:
                session._grid_conn = connections.pop(0)

        reply = PeerReply(id=0, result=0, peer=None)
        calls = 0

        async def read_reply(grid_conn, request_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                await hang()
            return reply

        with (
            patch.object(grid, "GRID_REPLY_TIMEOUT_S", 0.05),
            patch.object(session, "connect", connect),
            patch.object(session, "_read_grid_reply", read_reply),
            patch.object(_RemoteConnection, "_send_mesg", AsyncMock()),
            self.assertLogs(grid.LOGGER, "WARNING") as logs,
        ):
            self.assertIs(await session._peer_request(0, 10, b""), reply)
        self.assertIn("SDG grid request failed id=0 attempt=1 error=TimeoutError", logs.output[0])
        self.assertEqual(connections, [])
