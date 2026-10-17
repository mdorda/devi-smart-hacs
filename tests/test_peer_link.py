import asyncio
import unittest
from unittest.mock import patch

from pydevi.sdg import link
from pydevi.sdg.grid import SDGProtocolError
from pydevi.sdg.link import PeerLink

PROBE = b"probe"


class FakeTunnel:
    def __init__(self, answer_probe: bool = False) -> None:
        self.chunks: asyncio.Queue = asyncio.Queue()
        self.sent: list[bytes] = []
        self.closed = False
        self.monitor_stopped = False
        self.answer_probe = answer_probe

    async def read_peer_chunk(self) -> bytes:
        item = await self.chunks.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_peer_data(self, payload: bytes) -> None:
        self.sent.append(payload)
        if self.answer_probe and payload == PROBE:
            self.chunks.put_nowait(b"answer")

    async def close(self) -> None:
        self.closed = True

    def stop_monitor(self) -> None:
        self.monitor_stopped = True

    def diagnostic_context(self) -> str:
        return "context"

    def drop(self, error: BaseException | None = None) -> None:
        self.chunks.put_nowait(error or asyncio.IncompleteReadError(b"", 2))


class FakeGrid:
    """Hands out the given tunnels in order; an exception is raised, "hang" never returns."""

    def __init__(self, *tunnels) -> None:
        self.tunnels = list(tunnels)
        self.calls = 0
        self.disconnects = 0
        self.release = asyncio.Event()
        self.release.set()

    async def connect_to_remote(self, peer_id: str, protocol: str):
        await self.release.wait()
        self.calls += 1
        item = self.tunnels.pop(0) if self.tunnels else "hang"
        if item == "hang":
            await asyncio.Event().wait()
        if isinstance(item, BaseException):
            raise item
        return item

    async def disconnect(self) -> None:
        self.disconnects += 1


async def eventually(condition, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


class PeerLinkTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        for name, value in (
            ("WATCHDOG_INTERVAL_S", 0.01),
            ("WATCHDOG_PROBE_S", 0.05),
            ("WATCHDOG_TIMEOUT_S", 0.1),
            ("RECONNECT_BACKOFF_S", (0.0, 0.02, 0.04)),
            ("BACKOFF_RESET_S", 60.0),
            ("SEND_WAIT_S", 0.2),
        ):
            patcher = patch.object(link, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.chunks: list[bytes] = []
        self.events: list[bool] = []

    def start(self, grid: FakeGrid) -> PeerLink:
        peer_link = PeerLink(grid, "peer-1", probe=PROBE)
        peer_link.start(self.chunks.append, self.events.append)
        self.addAsyncCleanup(peer_link.stop)
        return peer_link

    async def test_delivers_pushed_chunks(self) -> None:
        tunnel = FakeTunnel()
        peer_link = self.start(FakeGrid(tunnel))
        await eventually(lambda: peer_link.connected)
        tunnel.chunks.put_nowait(b"one")
        tunnel.chunks.put_nowait(b"two")

        await eventually(lambda: len(self.chunks) == 2)
        self.assertEqual(self.chunks, [b"one", b"two"])
        self.assertEqual(self.events, [True])
        self.assertTrue(tunnel.monitor_stopped)

    async def test_remote_close_reconnects_immediately_after_a_long_life(self) -> None:
        first, second = FakeTunnel(), FakeTunnel()
        grid = FakeGrid(first, second)
        with patch.object(link, "BACKOFF_RESET_S", 0.0):
            peer_link = self.start(grid)
            await eventually(lambda: peer_link.connected)
            with self.assertLogs(link.LOGGER, "DEBUG") as logs:
                first.drop()
                await eventually(lambda: grid.calls == 2 and peer_link.connected)

        self.assertTrue(first.closed)
        self.assertEqual(self.events, [True, False, True])
        self.assertTrue(any("DEVI link lost" in line and "reason=eof" in line for line in logs.output))
        self.assertFalse(any("reconnect" in line for line in logs.output))

    async def test_reset_is_reported_as_reset(self) -> None:
        first = FakeTunnel()
        peer_link = self.start(FakeGrid(first, FakeTunnel()))
        await eventually(lambda: peer_link.connected)
        with self.assertLogs(link.LOGGER, "INFO") as logs:
            first.drop(ConnectionResetError(54, "reset"))
            await eventually(lambda: self.events == [True, False, True])

        self.assertTrue(any("reason=reset" in line for line in logs.output))

    async def test_unexpected_read_error_reconnects(self) -> None:
        first = FakeTunnel()
        grid = FakeGrid(first, FakeTunnel())
        peer_link = self.start(grid)
        await eventually(lambda: peer_link.connected)
        with self.assertLogs(link.LOGGER, "INFO") as logs:
            first.drop(ValueError("cannot decrypt"))
            await eventually(lambda: grid.calls == 2 and peer_link.connected)

        self.assertTrue(any("reason=error error=ValueError" in line for line in logs.output))

    async def test_unexpected_connect_error_is_retried(self) -> None:
        tunnel = FakeTunnel()
        grid = FakeGrid(RuntimeError("bug"), tunnel)
        peer_link = self.start(grid)

        await eventually(lambda: peer_link.connected)
        self.assertEqual(grid.calls, 2)

    async def test_short_lived_tunnels_back_off(self) -> None:
        first, second = FakeTunnel(), FakeTunnel()
        grid = FakeGrid(first, second)
        peer_link = self.start(grid)
        await eventually(lambda: peer_link.connected)
        with self.assertLogs(link.LOGGER, "DEBUG") as logs:
            first.drop()
            await eventually(lambda: grid.calls == 2 and peer_link.connected)

        self.assertTrue(any("reconnect peer=peer-1 attempt=1 delay_s=0" in line for line in logs.output))

    async def test_silent_tunnel_is_probed_then_replaced(self) -> None:
        first, second = FakeTunnel(), FakeTunnel(answer_probe=True)
        grid = FakeGrid(first, second)
        with self.assertLogs(link.LOGGER, "DEBUG") as logs:
            self.start(grid)
            await eventually(lambda: grid.calls == 2)

        self.assertEqual(first.sent, [PROBE])
        self.assertTrue(first.closed)
        self.assertTrue(any("reason=watchdog" in line for line in logs.output))

    async def test_answered_probe_keeps_the_tunnel(self) -> None:
        tunnel = FakeTunnel(answer_probe=True)
        grid = FakeGrid(tunnel)
        with self.assertLogs(link.LOGGER, "DEBUG") as logs:
            self.start(grid)
            await asyncio.sleep(0.3)

        self.assertEqual(grid.calls, 1)
        self.assertGreaterEqual(len(tunnel.sent), 2)
        self.assertEqual(self.chunks[0], b"answer")
        self.assertTrue(any("probe answered" in line for line in logs.output))

    async def test_failed_connect_is_retried(self) -> None:
        tunnel = FakeTunnel()
        grid = FakeGrid(OSError("unreachable"), tunnel)
        peer_link = self.start(grid)

        await eventually(lambda: peer_link.connected)
        self.assertEqual(grid.calls, 2)
        self.assertEqual(self.events, [True])

    async def test_hung_connect_resets_the_grid(self) -> None:
        tunnel = FakeTunnel()
        grid = FakeGrid("hang", tunnel)
        with patch.object(link, "CONNECT_TIMEOUT_S", 0.05):
            peer_link = self.start(grid)
            await eventually(lambda: peer_link.connected)

        self.assertEqual(grid.disconnects, 1)

    async def test_send_waits_for_the_tunnel(self) -> None:
        tunnel = FakeTunnel()
        grid = FakeGrid(tunnel)
        grid.release.clear()
        peer_link = self.start(grid)
        send = asyncio.create_task(peer_link.send(b"command"))
        await asyncio.sleep(0.05)
        self.assertFalse(send.done())

        grid.release.set()
        await send
        self.assertEqual(tunnel.sent, [b"command"])

    async def test_send_fails_when_no_tunnel_comes(self) -> None:
        grid = FakeGrid("hang")
        peer_link = self.start(grid)

        with self.assertRaises(SDGProtocolError):
            await peer_link.send(b"command")

    async def test_stop_closes_the_tunnel_quietly(self) -> None:
        tunnel = FakeTunnel()
        peer_link = self.start(FakeGrid(tunnel))
        await eventually(lambda: peer_link.connected)

        with self.assertNoLogs(link.LOGGER, "INFO"):
            await peer_link.stop()

        self.assertTrue(tunnel.closed)
        self.assertFalse(peer_link.connected)
        self.assertEqual(self.events, [True, False])

    async def test_failing_chunk_handler_keeps_the_tunnel(self) -> None:
        tunnel = FakeTunnel()
        grid = FakeGrid(tunnel)
        received: list[bytes] = []

        def handler(data: bytes) -> None:
            received.append(data)
            if data == b"bad":
                raise ValueError("broken handler")

        peer_link = PeerLink(grid, "peer-1", probe=PROBE)
        peer_link.start(handler, self.events.append)
        self.addAsyncCleanup(peer_link.stop)
        await eventually(lambda: peer_link.connected)
        with self.assertLogs(link.LOGGER, "ERROR"):
            tunnel.chunks.put_nowait(b"bad")
            tunnel.chunks.put_nowait(b"good")
            await eventually(lambda: received == [b"bad", b"good"])

        self.assertEqual(grid.calls, 1)
