import unittest

from pydevi.dominion import constants as c
from pydevi.dominion.client import STATE_PACKET_IDS, ThermostatClient
from pydevi.dominion.codec import DominionPacket
from pydevi.models import HVACMode, PresetMode


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


class ThermostatClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_apply_control_state_updates_target_mapping(self) -> None:
        client = ThermostatClient(DummyPeer())
        client.apply_packet(DominionPacket.from_byte(c.DOMINION_SCHEDULER, c.SCHEDULER_CONTROL_INFO, c.CONTROL_STATE_AT_HOME_OVERRIDE))
        client.apply_packet(DominionPacket.from_decimal(c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_TEMPORARY, 23.0))
        self.assertEqual(client.state.target_temperature_c, 23.0)

    async def test_current_temperature_follows_the_room(self) -> None:
        client = ThermostatClient(DummyPeer())
        # The initial dump sends the floor before the room.
        client.apply_packet(DominionPacket.from_decimal(c.DOMINION_HEATING, c.HEATING_TEMPERATURE_FLOOR, 19.01))
        client.apply_packet(DominionPacket.from_decimal(c.DOMINION_HEATING, c.HEATING_TEMPERATURE_ROOM, 23.03))
        client.apply_packet(DominionPacket.from_decimal(c.DOMINION_HEATING, c.HEATING_TEMPERATURE_ROOM, 23.08))
        client.apply_packet(DominionPacket.from_decimal(c.DOMINION_HEATING, c.HEATING_TEMPERATURE_FLOOR, 18.98))

        self.assertEqual(client.state.current_temperature_c, 23.08)
        self.assertEqual(client.state.floor_temperature_c, 18.98)

    async def test_alarm_bits(self) -> None:
        client = ThermostatClient(DummyPeer())
        client.apply_packet(DominionPacket.from_byte(c.DOMINION_SYSTEM, c.SYSTEM_ALARM_INFO, c.ALARM_OVERHEAT | c.ALARM_UNRECOVERABLE))
        self.assertTrue(client.state.warning_overheat)
        self.assertTrue(client.state.warning_unrecoverable)

    def test_state_tracks_active_setpoints(self) -> None:
        codes = {code for _, code in STATE_PACKET_IDS}
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


    async def test_leaving_a_special_state_cancels_it_first(self) -> None:
        # The thermostat ignores other mode commands until Off, Pause, Override or Vacation is cancelled.
        cases = (
            (c.CONTROL_STATE_OFF, [mode(c.CONTROL_MODE_OFF_STATE_OFF)]),
            (c.CONTROL_STATE_PAUSE, [mode(c.CONTROL_MODE_FROST_PROTECTION_OFF)]),
            (c.CONTROL_STATE_AT_HOME_OVERRIDE, [mode(c.CONTROL_MODE_TEMPORARY_HOME_OFF)]),
            (c.CONTROL_STATE_VACATION, [EMPTY_AWAY, (c.SCHEDULER_AWAY_ISPLANNED, b"\x00")]),
        )
        for state, leave in cases:
            with self.subTest(state=state):
                peer, client = client_in(state)

                await client.set_preset_mode(PresetMode.SCHEDULE)

                self.assertEqual(sent(peer), [leave + [mode(c.CONTROL_MODE_WEEKLY_SCHEDULE_ON)]])

    async def test_schedule_states_switch_directly(self) -> None:
        for state in (c.CONTROL_STATE_MANUAL, c.CONTROL_STATE_AT_HOME, c.CONTROL_STATE_AWAY):
            with self.subTest(state=state):
                peer, client = client_in(state)

                await client.set_preset_mode(PresetMode.PAUSE)

                self.assertEqual(sent(peer), [[mode(c.CONTROL_MODE_FROST_PROTECTION_ON)]])

    async def test_each_preset_sends_what_the_reference_sends(self) -> None:
        cases = (
            (PresetMode.MANUAL, [mode(c.CONTROL_MODE_WEEKLY_SCHEDULE_OFF)]),
            (PresetMode.SCHEDULE, [mode(c.CONTROL_MODE_WEEKLY_SCHEDULE_ON)]),
            (PresetMode.OVERRIDE, [mode(c.CONTROL_MODE_TEMPORARY_HOME_ON)]),
            (PresetMode.PAUSE, [mode(c.CONTROL_MODE_FROST_PROTECTION_ON)]),
            (PresetMode.OFF, [mode(c.CONTROL_MODE_OFF_STATE_ON)]),
            (PresetMode.VACATION, [EMPTY_AWAY, (c.SCHEDULER_AWAY_ISPLANNED, b"\x01")]),
        )
        for preset, packets in cases:
            with self.subTest(preset=preset):
                peer, client = client_in(c.CONTROL_STATE_MANUAL)

                await client.set_preset_mode(preset)

                self.assertEqual(sent(peer), [packets])

    async def test_vacation_while_off_cancels_off_first(self) -> None:
        # The case that left the thermostat stuck: vacation sent while it was off.
        peer, client = client_in(c.CONTROL_STATE_OFF)

        await client.set_preset_mode(PresetMode.VACATION)

        self.assertEqual(
            sent(peer),
            [[mode(c.CONTROL_MODE_OFF_STATE_OFF), EMPTY_AWAY, (c.SCHEDULER_AWAY_ISPLANNED, b"\x01")]],
        )

    async def test_asking_for_the_current_special_state_sends_nothing(self) -> None:
        cases = (
            (c.CONTROL_STATE_OFF, PresetMode.OFF),
            (c.CONTROL_STATE_PAUSE, PresetMode.PAUSE),
            (c.CONTROL_STATE_AT_HOME_OVERRIDE, PresetMode.OVERRIDE),
            (c.CONTROL_STATE_VACATION, PresetMode.VACATION),
        )
        for state, preset in cases:
            with self.subTest(state=state):
                peer, client = client_in(state)

                await client.set_preset_mode(preset)

                self.assertEqual(peer.sent_payloads, [])

    async def test_no_mode_change_while_configuring_or_fatal(self) -> None:
        for state in (c.CONTROL_STATE_CONFIGURING, c.CONTROL_STATE_FATAL):
            with self.subTest(state=state):
                peer, client = client_in(state)

                with self.assertRaises(ValueError):
                    await client.set_preset_mode(PresetMode.SCHEDULE)
                self.assertEqual(peer.sent_payloads, [])

    async def test_heat_is_the_weekly_schedule_as_in_the_reference(self) -> None:
        cases = (
            (c.CONTROL_STATE_OFF, [mode(c.CONTROL_MODE_OFF_STATE_OFF), mode(c.CONTROL_MODE_WEEKLY_SCHEDULE_ON)]),
            (c.CONTROL_STATE_MANUAL, [mode(c.CONTROL_MODE_WEEKLY_SCHEDULE_ON)]),
        )
        for state, packets in cases:
            with self.subTest(state=state):
                peer, client = client_in(state)

                await client.set_hvac_mode(HVACMode.HEAT)

                self.assertEqual(sent(peer), [packets])

    async def test_off_is_the_off_state(self) -> None:
        peer, client = client_in(c.CONTROL_STATE_AT_HOME)

        await client.set_hvac_mode(HVACMode.OFF)

        self.assertEqual(sent(peer), [[mode(c.CONTROL_MODE_OFF_STATE_ON)]])

    async def test_target_outside_the_active_setpoint_range_is_refused_before_any_mode_change(self) -> None:
        cases = (
            (c.CONTROL_STATE_PAUSE, 22.0),  # frost protection 5–9
            (c.CONTROL_STATE_VACATION, 9.5),  # away 10–28
            (c.CONTROL_STATE_AT_HOME, 28.5),  # temporary 5–28, would first switch to override
            (c.CONTROL_STATE_MANUAL, 21.2),  # not a 0.5 step
        )
        for state, value in cases:
            with self.subTest(state=state):
                peer, client = client_in(state)

                with self.assertRaises(ValueError):
                    await client.set_target_temperature(value)
                self.assertEqual(peer.sent_payloads, [])

    async def test_no_target_while_off_or_unknown(self) -> None:
        # ha-devi-mqtt has no setpoint to write while off; neither do we, and we do not switch heating on.
        for state in (c.CONTROL_STATE_OFF, c.CONTROL_STATE_CONFIGURING, c.CONTROL_STATE_FATAL, None):
            with self.subTest(state=state):
                peer = DummyPeer()
                client = ThermostatClient(peer)
                if state is not None:
                    client.apply_packet(DominionPacket.from_byte(c.DOMINION_SCHEDULER, c.SCHEDULER_CONTROL_INFO, state))

                with self.assertRaises(ValueError):
                    await client.set_target_temperature(21.0)
                self.assertEqual(peer.sent_payloads, [])

    async def test_target_writes_the_setpoint_of_the_current_state(self) -> None:
        cases = (
            (c.CONTROL_STATE_MANUAL, c.SCHEDULER_SETPOINT_MANUAL),
            (c.CONTROL_STATE_AT_HOME_OVERRIDE, c.SCHEDULER_SETPOINT_TEMPORARY),
            (c.CONTROL_STATE_VACATION, c.SCHEDULER_SETPOINT_AWAY),
        )
        for state, code in cases:
            with self.subTest(state=state):
                peer, client = client_in(state)

                await client.set_target_temperature(21.0)

                self.assertEqual(sent(peer), [[(code, DominionPacket.from_decimal(0, 0, 21.0).payload)]])

    async def test_target_in_the_schedule_starts_a_temporary_override(self) -> None:
        # The one intended difference from ha-devi-mqtt, which would change comfort or economy instead.
        for state in (c.CONTROL_STATE_AT_HOME, c.CONTROL_STATE_AWAY):
            with self.subTest(state=state):
                peer, client = client_in(state)

                await client.set_target_temperature(21.0)

                self.assertEqual(
                    sent(peer),
                    [
                        [mode(c.CONTROL_MODE_TEMPORARY_HOME_ON)],
                        [(c.SCHEDULER_SETPOINT_TEMPORARY, DominionPacket.from_decimal(0, 0, 21.0).payload)],
                    ],
                )

    async def test_target_in_pause_writes_frost_protection(self) -> None:
        peer, client = client_in(c.CONTROL_STATE_PAUSE)

        await client.set_target_temperature(7.0)

        self.assertEqual(sent(peer), [[(c.SCHEDULER_SETPOINT_FROST_PROTECTION, DominionPacket.from_decimal(0, 0, 7.0).payload)]])


# The reference's AwayPacket(null, null).
EMPTY_AWAY = (c.SCHEDULER_AWAY, bytes(14))


def mode(value: int) -> tuple[int, bytes]:
    return (c.SCHEDULER_CONTROL_MODE, bytes([value]))


def client_in(control_state: int) -> tuple[DummyPeer, ThermostatClient]:
    peer = DummyPeer()
    client = ThermostatClient(peer)
    client.apply_packet(DominionPacket.from_byte(c.DOMINION_SCHEDULER, c.SCHEDULER_CONTROL_INFO, control_state))
    return peer, client


def sent(peer: DummyPeer) -> list[list[tuple[int, bytes]]]:
    """Each write as (code, payload) pairs, with every packet checked to be a write to the scheduler."""
    writes = []
    for payload in peer.sent_payloads:
        packets = parse_wire_packets(payload)
        assert all(packet.msg_class == c.DOMINION_SCHEDULER for packet in packets)
        writes.append([(packet.msg_code, bytes(packet.payload)) for packet in packets])
    return writes


if __name__ == "__main__":
    unittest.main()
