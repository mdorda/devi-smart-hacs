import asyncio
import socket
import struct
import unittest
from unittest.mock import AsyncMock, Mock, patch

from pydevi.sdg import grid
from pydevi.sdg.grid import MODE_GRID, _RemoteConnection, _tcp_diagnostics


def make_connection(reader: asyncio.StreamReader) -> _RemoteConnection:
    writer = Mock()
    writer.is_closing.return_value = False
    writer.get_extra_info.return_value = None
    writer.wait_closed = AsyncMock()
    return _RemoteConnection(
        reader=reader, writer=writer, client_public_key=b"", client_secret=b"",
        mode=MODE_GRID, protocol="grid", connected=True,
    )


class ConnectionMonitorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        patcher = patch.object(grid, "MONITOR_INTERVAL_S", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_reports_remote_close_while_idle(self) -> None:
        reader = asyncio.StreamReader()
        conn = make_connection(reader)
        conn.start_monitor()
        reader.feed_eof()
        with self.assertLogs(grid.LOGGER, "WARNING") as logs:
            await asyncio.sleep(0.05)
        await conn.close()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("SDG monitor detected closed connection", logs.output[0])
        self.assertIn("eof=True", logs.output[0])

    async def test_reports_remote_close_with_unread_data(self) -> None:
        reader = asyncio.StreamReader()
        conn = make_connection(reader)
        conn.start_monitor()
        reader.feed_data(b"\x00\x05hello")
        reader.feed_eof()
        with self.assertLogs(grid.LOGGER, "DEBUG") as logs:
            await asyncio.sleep(0.05)
        await conn.close()
        output = "\n".join(logs.output)
        self.assertIn("SDG monitor unread data while idle bytes=7", output)
        self.assertIn("SDG monitor detected closed connection", output)

    async def test_reports_reset_error(self) -> None:
        reader = asyncio.StreamReader()
        conn = make_connection(reader)
        conn.start_monitor()
        reader.set_exception(ConnectionResetError(104, "reset"))
        with self.assertLogs(grid.LOGGER, "WARNING") as logs:
            await asyncio.sleep(0.05)
        await conn.close()
        self.assertIn("reader_error=ConnectionResetError(errno=104)", logs.output[0])

    async def test_reports_stuck_operation_once(self) -> None:
        reader = asyncio.StreamReader()
        conn = make_connection(reader)
        conn.start_monitor()
        conn._mark_busy("grid_request")
        conn._busy_since -= grid.MONITOR_STUCK_OPERATION_S + 1
        with self.assertLogs(grid.LOGGER, "WARNING") as logs:
            await asyncio.sleep(0.05)
        await conn.close()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("SDG monitor operation stuck operation=grid_request", logs.output[0])

    async def test_close_stops_monitor(self) -> None:
        conn = make_connection(asyncio.StreamReader())
        conn.start_monitor()
        task = conn._monitor_task
        await conn.close()
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())

    async def test_request_marks_connection_idle_after_failure(self) -> None:
        reader = asyncio.StreamReader()
        conn = make_connection(reader)
        conn.writer.write.side_effect = OSError("closed")
        with self.assertRaises(OSError):
            await conn.request_peer_data(b"read")
        self.assertIsNone(conn._busy_since)


class TcpDiagnosticsTest(unittest.TestCase):
    def test_parses_linux_tcp_info(self) -> None:
        values = [0] * 25
        values[0] = 8  # CLOSE_WAIT
        values[2] = 3  # consecutive retransmits
        values[12] = 2  # unacked
        values[17] = 50  # last data sent
        values[19] = 1234  # last data received
        values[20] = 60  # last ack received
        values[23] = 12500  # rtt in microseconds
        sock = Mock()
        sock.getsockopt.return_value = struct.pack(grid._TCP_INFO_FORMAT, *values)
        with patch.object(grid.sys, "platform", "linux"), patch.object(socket, "TCP_INFO", 11, create=True):
            result = _tcp_diagnostics(sock)
        self.assertEqual(
            result,
            "tcp_state=CLOSE_WAIT tcp_last_recv_ms=1234 tcp_last_send_ms=50 tcp_last_ack_recv_ms=60 "
            "tcp_unacked=2 tcp_retransmits=3 tcp_probes=0 tcp_backoff=0 tcp_retrans=0 tcp_rtt_ms=12.5",
        )

    def test_unavailable_socket(self) -> None:
        self.assertEqual(_tcp_diagnostics(None), "tcp=n/a")
        with patch.object(grid.sys, "platform", "linux"), patch.object(socket, "TCP_INFO", 11, create=True):
            self.assertEqual(_tcp_diagnostics(Mock()), "tcp=n/a")
