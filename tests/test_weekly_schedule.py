from datetime import UTC, datetime
import unittest

from pydevi.dominion import constants as c
from pydevi.dominion.client import STATE_PACKET_IDS, ThermostatClient
from pydevi.dominion.codec import DominionPacket
from pydevi.dominion.schedule import DAY_NAMES, encode_week_schedule


def parse_wire_packets(data: bytes) -> list[DominionPacket]:
    packets: list[DominionPacket] = []
    offset = 0
    while offset < len(data):
        body = data[offset + 1 :]
        packet = DominionPacket.from_bytes(body)
        packets.append(packet)
        offset += 1 + packet.packet_length
    return packets


class DummyPeer:
    def __init__(self) -> None:
        self.sent_payloads: list[bytes] = []

    async def send(self, payload: bytes) -> None:
        self.sent_payloads.append(payload)


class WeeklyScheduleTest(unittest.IsolatedAsyncioTestCase):
    def test_state_tracks_schedule_payloads(self) -> None:
        codes = {code for _, code in STATE_PACKET_IDS}
        self.assertIn(c.SCHEDULER_AWAY_ISPLANNED, codes)
        self.assertIn(c.SCHEDULER_AWAY, codes)
        self.assertIn(c.SCHEDULER_WEEK, codes)
        self.assertIn(c.SCHEDULER_WEEK_2, codes)

    def test_away_interval_is_read_in_the_opensdg_layout(self) -> None:
        # struct AwayInterval: size 14, then validity flag + sec, min, hour, day | dow << 5, month, year - 2000.
        payload = bytes([14, 1, 0, 30, 6, 17 | (2 << 5), 3, 26, 1, 0, 45, 8, 18 | (3 << 5), 3, 26])
        parsed = DominionPacket(c.DOMINION_SCHEDULER, c.SCHEDULER_AWAY, payload).get_away_interval()
        self.assertEqual(parsed.start, datetime(2026, 3, 17, 6, 30, tzinfo=UTC))
        self.assertEqual(parsed.end, datetime(2026, 3, 18, 8, 45, tzinfo=UTC))

    def test_away_interval_with_an_invalid_date_reads_as_empty(self) -> None:
        payload = bytes([14, 1, 0, 0, 0, 0, 0, 0, 0]) + bytes(6)
        parsed = DominionPacket(c.DOMINION_SCHEDULER, c.SCHEDULER_AWAY, payload).get_away_interval()
        self.assertIsNone(parsed.start)
        self.assertIsNone(parsed.end)

    def test_away_interval_parser_tolerates_empty_variant(self) -> None:
        packet = DominionPacket(c.DOMINION_SCHEDULER, c.SCHEDULER_AWAY, b"\x00\x00\x00\x00\x00\x00\x00\x00")
        parsed = packet.get_away_interval()
        self.assertIsNone(parsed.start)
        self.assertIsNone(parsed.end)

    async def test_set_weekly_schedule_clears_then_writes_like_the_reference(self) -> None:
        peer = DummyPeer()
        client = ThermostatClient(peer)
        week_1, week_2 = _schedule()

        await client.set_weekly_schedule(week_1, week_2)

        # ha-devi-mqtt: zero week 1, zero week 2, week 1, week 2, each in a message of its own.
        # The thermostat reports these fields as 29 and 43 bytes: a length byte, then the ranges.
        writes = [[(packet.msg_code, packet.payload) for packet in parse_wire_packets(payload)] for payload in peer.sent_payloads]
        self.assertEqual(
            writes,
            [
                [(c.SCHEDULER_WEEK, bytes([28]) + bytes(28))],
                [(c.SCHEDULER_WEEK_2, bytes([42]) + bytes(42))],
                [(c.SCHEDULER_WEEK, bytes([28]) + week_1)],
                [(c.SCHEDULER_WEEK_2, bytes([42]) + week_2)],
            ],
        )
        self.assertEqual(client.state.weekly_schedule.week_1_raw, bytes([28]) + week_1)
        self.assertEqual(client.state.weekly_schedule.week_2_raw, bytes([42]) + week_2)

    async def test_set_weekly_schedule_accepts_what_the_thermostat_reports(self) -> None:
        peer = DummyPeer()
        client = ThermostatClient(peer)
        week_1, week_2 = _schedule()

        await client.set_weekly_schedule(bytes([28]) + week_1, bytes([42]) + week_2)

        last_two = [parse_wire_packets(payload)[0].payload for payload in peer.sent_payloads[2:]]
        self.assertEqual(last_two, [bytes([28]) + week_1, bytes([42]) + week_2])

    async def test_set_weekly_schedule_refuses_wrong_lengths_and_contents(self) -> None:
        peer = DummyPeer()
        client = ThermostatClient(peer)
        week_1, week_2 = _schedule()

        for bad_1, bad_2 in (
            (week_1 + b"\x00", week_2),
            (week_1, week_2 + b"\x00"),
            (b"\x10\x20\x30", week_2),
            (bytes([27]) + week_1, week_2),
            (b"\xff" * 28, week_2),
        ):
            with self.assertRaises(ValueError):
                await client.set_weekly_schedule(bad_1, bad_2)
        self.assertEqual(peer.sent_payloads, [])

    def test_apply_packet_updates_schedule_state(self) -> None:
        client = ThermostatClient(DummyPeer())
        week_1 = b"\xAA\xBB"
        week_2 = b"\xCC\xDD\xEE"

        client.apply_packet(DominionPacket(c.DOMINION_SCHEDULER, c.SCHEDULER_WEEK, week_1))
        client.apply_packet(DominionPacket(c.DOMINION_SCHEDULER, c.SCHEDULER_WEEK_2, week_2))

        self.assertIsNotNone(client.state.weekly_schedule)
        self.assertEqual(client.state.weekly_schedule.week_1_raw, week_1)
        self.assertEqual(client.state.weekly_schedule.week_2_raw, week_2)


def _schedule() -> tuple[bytes, bytes]:
    """The ranges of both fields, without the length byte the encoder puts in front."""
    week_1, week_2 = encode_week_schedule({day: ["06:00-08:30", "16:00-22:30"] for day in DAY_NAMES})
    return week_1[1:], week_2[1:]


if __name__ == "__main__":
    unittest.main()
