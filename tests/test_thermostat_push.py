import asyncio
import unittest
from unittest.mock import patch

from pydevi.dominion import client as client_module
from pydevi.dominion import constants as c
from pydevi.dominion.client import ThermostatClient
from pydevi.dominion.codec import DominionPacket
from pydevi.models import ThermostatState

ROOM_TEMPERATURE = (c.DOMINION_HEATING, c.HEATING_TEMPERATURE_ROOM)
HEATING_INFO = (c.DOMINION_SYSTEM, c.SYSTEM_HEATING_INFO)
COMFORT = (c.DOMINION_SCHEDULER, c.SCHEDULER_SETPOINT_COMFORT)
CLOCK = (c.DOMINION_SYSTEM, 29237)


def chunk(*packets: DominionPacket) -> bytes:
    # Pushed packets carry no read/write prefix byte.
    return b"".join(packet.to_wire_bytes()[1:] for packet in packets)


def room_temperature(value_c: float) -> DominionPacket:
    return DominionPacket.from_decimal(*ROOM_TEMPERATURE, value_c)


class FakeLink:
    peer_id = "peer-1"

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.on_chunk = None
        self.on_connection = None
        self.stopped = False

    def start(self, on_chunk, on_connection) -> None:
        self.on_chunk = on_chunk
        self.on_connection = on_connection

    async def stop(self) -> None:
        self.stopped = True

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)


class ThermostatPushTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        for name, value in (
            ("STATE_PACKET_IDS", (ROOM_TEMPERATURE, HEATING_INFO)),
            ("READY_TIMEOUT_S", 0.05),
            ("COMMAND_CONFIRM_S", 0.05),
        ):
            patcher = patch.object(client_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.link = FakeLink()
        self.client = ThermostatClient(self.link)
        self.updates: list[ThermostatState] = []
        self.connection: list[bool] = []
        self.client.start(self.updates.append, self.connection.append)
        self.addAsyncCleanup(self.client.stop)

    async def test_push_updates_state_and_notifies(self) -> None:
        self.link.on_connection(True)
        self.link.on_chunk(chunk(room_temperature(22.64)))

        self.assertEqual(self.client.state.room_temperature_c, 22.64)
        self.assertEqual(len(self.updates), 1)
        self.assertIs(self.updates[0], self.client.state)
        self.assertEqual(self.connection, [True])

    async def test_unchanged_values_do_not_notify(self) -> None:
        self.link.on_chunk(chunk(room_temperature(22.64)))
        self.link.on_chunk(chunk(room_temperature(22.64)))
        self.link.on_chunk(chunk(DominionPacket(*CLOCK, b"\x00" * 6)))

        self.assertEqual(len(self.updates), 1)

    async def test_changes_are_logged_without_names(self) -> None:
        with self.assertLogs(client_module.LOGGER, "DEBUG") as logs:
            self.link.on_chunk(chunk(
                room_temperature(21.5),
                DominionPacket.from_string(c.DOMINION_SYSTEM, c.SYSTEM_ROOM_NAME, "Koupelna"),
            ))

        changed = [line for line in logs.output if "push changed" in line]
        self.assertEqual(len(changed), 1)
        self.assertIn("room_temperature_c:None->21.5", changed[0])
        self.assertIn("room_name", changed[0])
        self.assertNotIn("Koupelna", changed[0])

    async def test_ready_once_every_state_packet_arrived(self) -> None:
        self.link.on_connection(True)
        self.link.on_chunk(chunk(room_temperature(22.0)))
        self.assertFalse(self.client.ready)

        self.link.on_chunk(chunk(DominionPacket.from_bool(*HEATING_INFO, True)))
        self.assertTrue(self.client.ready)
        self.assertTrue(await self.client.wait_ready(0.01))

    async def test_reconnect_waits_for_a_fresh_state(self) -> None:
        self.link.on_connection(True)
        self.link.on_chunk(chunk(room_temperature(22.0), DominionPacket.from_bool(*HEATING_INFO, True)))
        self.link.on_connection(False)
        self.assertFalse(self.client.ready)

        self.link.on_connection(True)
        self.link.on_chunk(chunk(room_temperature(22.0)))
        self.assertFalse(self.client.ready)
        self.assertEqual(self.connection, [True, False, True])

    async def test_missing_state_is_requested(self) -> None:
        self.link.on_connection(True)
        self.link.on_chunk(chunk(room_temperature(22.0)))

        with self.assertLogs(client_module.LOGGER, "WARNING") as logs:
            await asyncio.sleep(0.1)

        self.assertEqual(self.link.sent, [DominionPacket.read_request(*HEATING_INFO).to_wire_bytes()])
        self.assertIn("missing=5:29240", logs.output[0])

    async def test_complete_state_is_not_requested_again(self) -> None:
        self.link.on_connection(True)
        self.link.on_chunk(chunk(room_temperature(22.0), DominionPacket.from_bool(*HEATING_INFO, True)))
        await asyncio.sleep(0.1)

        self.assertEqual(self.link.sent, [])

    async def test_command_confirmed_by_push(self) -> None:
        with self.assertLogs(client_module.LOGGER, "DEBUG") as logs:
            await self.client.set_number_setting("comfort_temperature", 22.5)
            self.link.on_chunk(chunk(DominionPacket.from_decimal(*COMFORT, 22.5)))
            await asyncio.sleep(0.1)

        self.assertEqual(self.link.sent, [DominionPacket.from_decimal(*COMFORT, 22.5).to_wire_bytes()])
        self.assertTrue(any("command confirmed by push" in line and "id=7:29330" in line for line in logs.output))
        self.assertFalse(any("WARNING" in line for line in logs.output))

    async def test_unconfirmed_command_is_reported(self) -> None:
        with self.assertLogs(client_module.LOGGER, "WARNING") as logs:
            await self.client.set_number_setting("comfort_temperature", 22.5)
            await asyncio.sleep(0.1)

        self.assertIn("command not confirmed by push", logs.output[0])
        self.assertIn("ids=7:29330", logs.output[0])

    async def test_stop_stops_the_link(self) -> None:
        await self.client.stop()

        self.assertTrue(self.link.stopped)
