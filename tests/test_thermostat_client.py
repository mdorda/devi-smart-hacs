import unittest

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
        self.sent_payloads: list[bytes] = []
        self.request_payloads: list[bytes] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def send(self, payload: bytes) -> None:
        self.sent_payloads.append(payload)

    async def request(self, payload: bytes, timeout: float = 1.0) -> list[bytes]:
        self.request_payloads.append(payload)
        return []


class ThermostatClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_apply_control_state_updates_target_mapping(self) -> None:
        client = ThermostatClient(DummyPeer())
        client.apply_packet(DominionPacket.from_byte(c.DOMINION_SCHEDULER, c.SCHEDULER_CONTROL_INFO, c.CONTROL_STATE_AT_HOME_OVERRIDE))
        client.apply_packet(DominionPacket.from_decimal(c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_TEMPORARY, 23.0))
        self.assertEqual(client.state.target_temperature_c, 23.0)

    async def test_alarm_bits(self) -> None:
        client = ThermostatClient(DummyPeer())
        client.apply_packet(DominionPacket.from_byte(c.DOMINION_SYSTEM, c.SYSTEM_ALARM_INFO, c.ALARM_OVERHEAT | c.ALARM_UNRECOVERABLE))
        self.assertTrue(client.state.warning_overheat)
        self.assertTrue(client.state.warning_unrecoverable)

    async def test_refresh_state_requests_active_setpoints(self) -> None:
        peer = DummyPeer()
        client = ThermostatClient(peer)

        await client.refresh_state()

        self.assertEqual(len(peer.request_payloads), 1)
        payload = peer.request_payloads[0]
        packets = parse_wire_packets(payload)
        codes = {packet.msg_code for packet in packets}
        self.assertIn(c.SCHEDULER_SETPOINT_COMFORT, codes)
        self.assertIn(c.SCHEDULER_SETPOINT_ECONOMY, codes)
        self.assertIn(c.SCHEDULER_SETPOINT_MANUAL, codes)
        self.assertIn(c.SCHEDULER_SETPOINT_TEMPORARY, codes)
        self.assertIn(c.SCHEDULER_SETPOINT_AWAY, codes)
        self.assertIn(c.SCHEDULER_SETPOINT_FROST_PROTECTION, codes)

    async def test_set_target_temperature_in_schedule_uses_override_temp(self) -> None:
        peer = DummyPeer()
        client = ThermostatClient(peer)
        client.apply_packet(DominionPacket.from_byte(c.DOMINION_SCHEDULER, c.SCHEDULER_CONTROL_INFO, c.CONTROL_STATE_AT_HOME))

        await client.set_target_temperature(22.5)

        self.assertEqual(len(peer.sent_payloads), 2)
        mode_packet = parse_wire_packets(peer.sent_payloads[0])[0]
        setpoint_packet = parse_wire_packets(peer.sent_payloads[1])[0]
        self.assertEqual(mode_packet.msg_code, c.SCHEDULER_CONTROL_MODE)
        self.assertEqual(setpoint_packet.msg_code, c.SCHEDULER_SETPOINT_TEMPORARY)
        self.assertEqual(setpoint_packet.get_decimal(), 22.5)


if __name__ == "__main__":
    unittest.main()
