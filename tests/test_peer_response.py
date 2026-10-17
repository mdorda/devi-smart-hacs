import asyncio
import struct
import unittest
from unittest.mock import Mock, patch

from pydevi.dominion.codec import DominionPacket
from pydevi.sdg import grid
from pydevi.sdg.grid import MODE_PEER, _RemoteConnection
from pydevi.sdg.transport import CMD_MESG, PacketHeader

ROOM_TEMPERATURE = (6, 29299)
HEATING_INFO = (5, 29240)
CLOCK = (5, 29237)


def response(packet_id: tuple[int, int], payload: bytes) -> bytes:
    return DominionPacket(*packet_id, payload).to_wire_bytes()[1:]


def frame(data: bytes) -> bytes:
    body = struct.pack(">H", len(data)) + data
    return struct.pack(">H", 8 + len(body)) + PacketHeader.MAGIC + CMD_MESG + body


def read_request(*packet_ids: tuple[int, int]) -> bytes:
    return b"".join(DominionPacket.read_request(*packet_id).to_wire_bytes() for packet_id in packet_ids)


class PeerResponseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.reader = asyncio.StreamReader()
        writer = Mock()
        writer.is_closing.return_value = False
        writer.get_extra_info.return_value = None
        self.conn = _RemoteConnection(
            reader=self.reader, writer=writer, client_public_key=b"", client_secret=b"",
            mode=MODE_PEER, protocol="dominion-1.0", connected=True,
        )
        # Frames here carry no protocol prefix byte to drop from the first chunk.
        self.conn._discard_first_bytes = 0
        self.reply: list[bytes] = []
        for patcher in (
            patch.object(_RemoteConnection, "_decrypt_mesg", lambda self, body, prefix: body),
            patch.object(_RemoteConnection, "_send_mesg_data", self._send),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _send(self, payload: bytes) -> None:
        for data in self.reply:
            self.reader.feed_data(frame(data))

    async def _push_clock_forever(self) -> None:
        # The thermostat pushes its clock every second, so the stream never goes idle.
        while True:
            await asyncio.sleep(0.01)
            self.reader.feed_data(frame(response(CLOCK, b"\x00" * 6)))

    async def test_stops_once_every_requested_packet_arrived(self) -> None:
        self.reply = [response(ROOM_TEMPERATURE, b"\xd6\x08") + response(HEATING_INFO, b"\x01")]
        pusher = asyncio.create_task(self._push_clock_forever())
        try:
            responses = await asyncio.wait_for(
                self.conn.request_peer_data(
                    read_request(ROOM_TEMPERATURE, HEATING_INFO),
                    first_chunk_timeout=1.0, idle_timeout=1.0, stop_when_answered=True,
                ),
                timeout=2.0,
            )
        finally:
            pusher.cancel()
        self.assertEqual(responses, self.reply)

    async def test_buffered_push_of_requested_packet_is_not_an_answer(self) -> None:
        self.reader.feed_data(frame(response(ROOM_TEMPERATURE, b"\xd0\x08")))
        self.reply = [response(CLOCK, b"\x00" * 6), response(ROOM_TEMPERATURE, b"\xd6\x08")]
        responses = await self.conn.request_peer_data(
            read_request(ROOM_TEMPERATURE), first_chunk_timeout=1.0, idle_timeout=1.0, stop_when_answered=True,
        )
        self.assertEqual(len(responses), 3)
        self.assertEqual(responses[-1], response(ROOM_TEMPERATURE, b"\xd6\x08"))

    async def test_gives_up_waiting_for_missing_packet(self) -> None:
        self.reply = [response(ROOM_TEMPERATURE, b"\xd6\x08")]
        pusher = asyncio.create_task(self._push_clock_forever())
        try:
            with patch.object(grid, "RESPONSE_MAX_DURATION_S", 0.1), self.assertLogs(grid.LOGGER, "WARNING") as logs:
                responses = await asyncio.wait_for(
                    self.conn.request_peer_data(
                        read_request(ROOM_TEMPERATURE, HEATING_INFO),
                        first_chunk_timeout=1.0, idle_timeout=1.0, stop_when_answered=True,
                    ),
                    timeout=2.0,
                )
        finally:
            pusher.cancel()
        self.assertEqual(responses[0], self.reply[0])
        self.assertIn("missing=5:29240", logs.output[-1])

    async def test_eof_marks_connection_disconnected(self) -> None:
        self.reply = [response(CLOCK, b"\x00" * 6)]
        original_send = self._send

        async def send_then_close(payload: bytes) -> None:
            await original_send(payload)
            self.reader.feed_eof()

        with patch.object(_RemoteConnection, "_send_mesg_data", lambda self, payload: send_then_close(payload)):
            responses = await self.conn.request_peer_data(
                read_request(ROOM_TEMPERATURE), first_chunk_timeout=1.0, idle_timeout=1.0, stop_when_answered=True,
            )
        self.assertEqual(len(responses), 1)
        self.assertFalse(self.conn.connected)
