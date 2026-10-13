import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from pydevi.sdg import control, grid
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


async def send_ping(self) -> None:
    self.ping_sequence += 1


def pong(sequence: int) -> tuple[int, bytes]:
    return control.MSG_PONG, control.encode_ping(sequence)


class PongCodecTest(unittest.TestCase):
    def test_pong_carries_the_ping_sequence(self) -> None:
        self.assertEqual(control.decode_pong(control.encode_ping(7, 12)), 7)


class GridKeepaliveTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.session = GridSession()
        self.conn = make_connection()
        self.session._grid_conn = self.conn
        patches = (
            patch.object(grid, "GRID_PING_INTERVAL_S", 0.01),
            patch.object(grid, "GRID_REPLY_TIMEOUT_S", 0.05),
            patch.object(_RemoteConnection, "_send_ping", send_ping),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self) -> None:
        await self.session.disconnect()

    async def test_pings_repeatedly_while_connection_is_alive(self) -> None:
        async def read_message(grid_conn):
            return pong(grid_conn.ping_sequence - 1)

        with patch.object(self.session, "_read_grid_message", read_message):
            self.session._start_keepalive(self.conn)
            await asyncio.sleep(0.1)
        self.assertGreaterEqual(self.conn.ping_sequence, 3)
        self.assertIs(self.session._grid_conn, self.conn)
        self.assertIsNotNone(self.conn.ping_delay)
        self.assertIsNone(self.conn._busy_since)

    async def test_stale_pong_does_not_end_the_ping(self) -> None:
        messages = [(control.MSG_REMOTE_REPLY, b""), pong(4), pong(5)]

        async def read_message(grid_conn):
            return messages.pop(0)

        self.conn.ping_sequence = 5
        with patch.object(self.session, "_read_grid_message", read_message):
            await self.session._ping(self.conn)
        self.assertEqual(messages, [])

    async def test_missing_pong_drops_grid_connection(self) -> None:
        async def read_message(grid_conn):
            await asyncio.sleep(10)

        with (
            patch.object(self.session, "_read_grid_message", read_message),
            self.assertLogs(grid.LOGGER, "WARNING") as logs,
        ):
            self.session._start_keepalive(self.conn)
            task = self.session._keepalive_task
            await asyncio.wait_for(task, 1)
        self.assertIn("SDG grid ping failed seq=0 error=TimeoutError", logs.output[0])
        self.assertFalse(task.cancelled())
        self.assertIsNone(self.session._grid_conn)
        self.assertIsNone(self.session._keepalive_task)
        self.conn.writer.close.assert_called_once()

    async def test_closed_connection_drops_grid_connection(self) -> None:
        self.conn.reader.feed_eof()
        with self.assertLogs(grid.LOGGER, "WARNING") as logs:
            self.session._start_keepalive(self.conn)
            await asyncio.wait_for(self.session._keepalive_task, 1)
        self.assertIn("SDG grid ping failed seq=0 error=IncompleteReadError", logs.output[-1])
        self.assertIsNone(self.session._grid_conn)

    async def test_ping_waits_for_running_grid_request(self) -> None:
        async def read_message(grid_conn):
            return pong(grid_conn.ping_sequence - 1)

        with patch.object(self.session, "_read_grid_message", read_message):
            async with self.session._request_lock:
                self.session._start_keepalive(self.conn)
                await asyncio.sleep(0.05)
                self.assertEqual(self.conn.ping_sequence, 0)
            await asyncio.sleep(0.05)
        self.assertGreaterEqual(self.conn.ping_sequence, 1)

    async def test_replaced_connection_stops_its_keepalive(self) -> None:
        self.session._start_keepalive(self.conn)
        task = self.session._keepalive_task
        self.session._grid_conn = make_connection()
        await asyncio.wait_for(task, 1)
        self.assertEqual(self.conn.ping_sequence, 0)

    async def test_disconnect_cancels_keepalive(self) -> None:
        self.session._start_keepalive(self.conn)
        task = self.session._keepalive_task
        await self.session.disconnect()
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.assertIsNone(self.session._keepalive_task)


if __name__ == "__main__":
    unittest.main()
