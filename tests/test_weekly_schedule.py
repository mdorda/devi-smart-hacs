from datetime import UTC, datetime
import unittest

from pydevi.dominion import constants as c
from pydevi.dominion.client import STATE_PACKET_IDS, ThermostatClient
from pydevi.dominion.codec import DominionPacket
from pydevi.models import AwayInterval


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

    def test_away_interval_packet_roundtrip(self) -> None:
        start = datetime(2026, 3, 17, 6, 30, 0, tzinfo=UTC)
        end = datetime(2026, 3, 18, 8, 45, 0, tzinfo=UTC)
        packet = DominionPacket.from_away_interval(
            c.DOMINION_SCHEDULER,
            c.SCHEDULER_AWAY,
            AwayInterval(start=start, end=end),
        )
        parsed = packet.get_away_interval()
        self.assertIsNotNone(parsed.start)
        self.assertIsNotNone(parsed.end)
        self.assertEqual(parsed.start, start)
        self.assertEqual(parsed.end, end)

    def test_away_interval_parser_tolerates_empty_variant(self) -> None:
        packet = DominionPacket(c.DOMINION_SCHEDULER, c.SCHEDULER_AWAY, b"\x00\x00\x00\x00\x00\x00\x00\x00")
        parsed = packet.get_away_interval()
        self.assertIsNone(parsed.start)
        self.assertIsNone(parsed.end)

    async def test_set_weekly_schedule_sends_week_packets(self) -> None:
        peer = DummyPeer()
        client = ThermostatClient(peer)
        week_1 = b"\x10\x20\x30"
        week_2 = b"\x40\x50"

        await client.set_weekly_schedule(week_1, week_2)

        self.assertEqual(len(peer.sent_payloads), 1)
        packets = parse_wire_packets(peer.sent_payloads[0])
        self.assertEqual(len(packets), 2)
        self.assertEqual(packets[0].msg_code, c.SCHEDULER_WEEK)
        self.assertEqual(packets[0].payload, week_1)
        self.assertEqual(packets[1].msg_code, c.SCHEDULER_WEEK_2)
        self.assertEqual(packets[1].payload, week_2)
        self.assertIsNotNone(client.state.weekly_schedule)
        self.assertEqual(client.state.weekly_schedule.week_1_raw, week_1)
        self.assertEqual(client.state.weekly_schedule.week_2_raw, week_2)

    async def test_set_away_interval_sends_away_and_flag(self) -> None:
        peer = DummyPeer()
        client = ThermostatClient(peer)
        away = AwayInterval(
            start=datetime(2026, 3, 17, 7, 0, 0, tzinfo=UTC),
            end=datetime(2026, 3, 17, 9, 0, 0, tzinfo=UTC),
        )

        await client.set_away_interval(away, is_planned=True)

        self.assertEqual(len(peer.sent_payloads), 1)
        packets = parse_wire_packets(peer.sent_payloads[0])
        self.assertEqual(len(packets), 2)
        self.assertEqual(packets[0].msg_code, c.SCHEDULER_AWAY)
        self.assertEqual(packets[1].msg_code, c.SCHEDULER_AWAY_ISPLANNED)
        self.assertTrue(packets[1].get_bool())
        self.assertEqual(packets[0].get_away_interval().start, away.start)
        self.assertEqual(packets[0].get_away_interval().end, away.end)

    def test_apply_packet_updates_schedule_state(self) -> None:
        client = ThermostatClient(DummyPeer())
        week_1 = b"\xAA\xBB"
        week_2 = b"\xCC\xDD\xEE"

        client.apply_packet(DominionPacket(c.DOMINION_SCHEDULER, c.SCHEDULER_WEEK, week_1))
        client.apply_packet(DominionPacket(c.DOMINION_SCHEDULER, c.SCHEDULER_WEEK_2, week_2))

        self.assertIsNotNone(client.state.weekly_schedule)
        self.assertEqual(client.state.weekly_schedule.week_1_raw, week_1)
        self.assertEqual(client.state.weekly_schedule.week_2_raw, week_2)


if __name__ == "__main__":
    unittest.main()
