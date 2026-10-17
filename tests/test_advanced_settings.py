import unittest

from pydevi.dominion import constants as c
from pydevi.dominion.client import STATE_PACKET_IDS, ThermostatClient
from pydevi.dominion.codec import DominionPacket


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


class AdvancedSettingsTest(unittest.IsolatedAsyncioTestCase):
    def test_state_tracks_advanced_codes(self) -> None:
        codes = {code for _, code in STATE_PACKET_IDS}
        self.assertIn(c.SCHEDULER_SETPOINT_FLOOR_COMFORT, codes)
        self.assertIn(c.SCHEDULER_SETPOINT_FLOOR_COMFORT_ENABLED, codes)
        self.assertIn(c.SCHEDULER_SETPOINT_MAX_FLOOR, codes)
        self.assertIn(c.SYSTEM_WINDOW_OPEN, codes)
        self.assertIn(c.SYSTEM_INFO_FORECAST_ENABLED, codes)
        self.assertIn(c.SYSTEM_INFO_WINDOW_OPEN_DETECTION, codes)
        self.assertIn(c.SYSTEM_UI_SAFETY_LOCK, codes)

    async def test_set_number_setting_sends_decimal_packet(self) -> None:
        peer = DummyPeer()
        client = ThermostatClient(peer)

        await client.set_number_setting("max_floor_temperature", 28.5)

        self.assertEqual(len(peer.sent_payloads), 1)
        packet = parse_wire_packets(peer.sent_payloads[0])[0]
        self.assertEqual(packet.msg_code, c.SCHEDULER_SETPOINT_MAX_FLOOR)
        self.assertEqual(packet.get_decimal(), 28.5)
        self.assertEqual(client.state.max_floor_temperature_c, 28.5)

    async def test_set_switch_setting_sends_bool_packet(self) -> None:
        peer = DummyPeer()
        client = ThermostatClient(peer)

        await client.set_switch_setting("window_open_detection_enabled", True)

        self.assertEqual(len(peer.sent_payloads), 1)
        packet = parse_wire_packets(peer.sent_payloads[0])[0]
        self.assertEqual(packet.msg_code, c.SYSTEM_INFO_WINDOW_OPEN_DETECTION)
        self.assertTrue(packet.get_bool())
        self.assertTrue(client.state.window_open_detection_enabled)

    async def test_apply_packet_updates_advanced_state(self) -> None:
        client = ThermostatClient(DummyPeer())

        client.apply_packet(DominionPacket.from_bool(c.DOMINION_SYSTEM, c.SYSTEM_WINDOW_OPEN, True))
        client.apply_packet(DominionPacket.from_decimal(c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_FLOOR_COMFORT, 23.0))
        client.apply_packet(DominionPacket.from_decimal(c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_MAX_FLOOR, 29.5))
        client.apply_packet(DominionPacket.from_bool(c.DOMINION_SYSTEM, c.SYSTEM_INFO_FORECAST_ENABLED, True))
        client.apply_packet(DominionPacket.from_bool(c.DOMINION_SYSTEM, c.SYSTEM_UI_SAFETY_LOCK, True))

        self.assertTrue(client.state.window_open_active)
        self.assertEqual(client.state.floor_comfort_temperature_c, 23.0)
        self.assertEqual(client.state.max_floor_temperature_c, 29.5)
        self.assertTrue(client.state.forecast_heating_enabled)
        self.assertTrue(client.state.safety_lock_enabled)


if __name__ == "__main__":
    unittest.main()
