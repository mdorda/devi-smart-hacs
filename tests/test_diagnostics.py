import unittest
import struct

from pydevi.dominion import constants as c
from pydevi.dominion.client import ThermostatClient
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
        self.request_payloads: list[bytes] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def send(self, payload: bytes) -> None:
        return None

    async def request(self, payload: bytes, timeout: float = 1.0) -> list[bytes]:
        self.request_payloads.append(payload)
        return []


class ThermostatDiagnosticsTest(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_state_requests_metadata_and_alarm_codes(self) -> None:
        peer = DummyPeer()
        client = ThermostatClient(peer)

        await client.refresh_state()

        self.assertEqual(len(peer.request_payloads), 1)
        packets = parse_wire_packets(peer.request_payloads[0])
        codes = {packet.msg_code for packet in packets}
        self.assertIn(c.SYSTEM_ALARM_INFO, codes)
        self.assertIn(c.SYSTEM_INFO_FLOOR_SENSOR_CONNECTED, codes)
        self.assertIn(c.GLOBAL_HARDWAREREVISION, codes)
        self.assertIn(c.GLOBAL_SOFTWAREREVISION, codes)
        self.assertIn(c.GLOBAL_SOFTWAREBUILDREVISION, codes)
        self.assertIn(c.GLOBAL_SERIALNUMBER, codes)
        self.assertIn(c.GLOBAL_PRODUCTIONDATE, codes)
        self.assertIn(c.SYSTEM_ROOM_NAME, codes)
        self.assertIn(c.SYSTEM_HOUSE_NAME, codes)

    async def test_apply_packet_parses_alarm_and_metadata_fields(self) -> None:
        client = ThermostatClient(DummyPeer())

        client.apply_packet(
            DominionPacket.from_byte(
                c.DOMINION_SYSTEM,
                c.SYSTEM_ALARM_INFO,
                c.ALARM_DISCONNECTED | c.ALARM_SHORT_CIRCUITED | c.ALARM_OVERHEAT,
            )
        )
        client.apply_packet(DominionPacket.from_bool(c.DOMINION_SYSTEM, c.SYSTEM_INFO_FLOOR_SENSOR_CONNECTED, True))
        client.apply_packet(
            DominionPacket(c.DEVICEGLOBAL, c.GLOBAL_HARDWAREREVISION, struct.pack("<H", (2 << 8) | 7))
        )
        client.apply_packet(
            DominionPacket(c.DEVICEGLOBAL, c.GLOBAL_SOFTWAREREVISION, struct.pack("<H", (5 << 8) | 4))
        )
        client.apply_packet(DominionPacket.from_short(c.DEVICEGLOBAL, c.GLOBAL_SOFTWAREBUILDREVISION, 999))
        client.apply_packet(DominionPacket.from_int(c.DEVICEGLOBAL, c.GLOBAL_SERIALNUMBER, 12345678))
        client.apply_packet(
            DominionPacket(
                c.DEVICEGLOBAL,
                c.GLOBAL_PRODUCTIONDATE,
                bytes([0, 30, 9, (5 << 5) | 2, 8, 24]),
            )
        )
        client.apply_packet(DominionPacket.from_string(c.DOMINION_SYSTEM, c.SYSTEM_ROOM_NAME, "Bathroom"))
        client.apply_packet(DominionPacket.from_string(c.DOMINION_SYSTEM, c.SYSTEM_HOUSE_NAME, "Home"))

        self.assertTrue(client.state.warning_disconnected)
        self.assertTrue(client.state.warning_shorted)
        self.assertTrue(client.state.warning_short_circuited)
        self.assertTrue(client.state.warning_overheat)
        self.assertFalse(client.state.warning_unrecoverable)
        self.assertTrue(client.state.floor_sensor_connected)
        self.assertEqual(client.state.hardware_version, "2.7")
        self.assertEqual(client.state.firmware_version, "5.4.999")
        self.assertEqual(client.state.serial_number, "12345678")
        self.assertIsNotNone(client.state.production_date)
        self.assertEqual(client.state.room_name, "Bathroom")
        self.assertEqual(client.state.house_name, "Home")


if __name__ == "__main__":
    unittest.main()
