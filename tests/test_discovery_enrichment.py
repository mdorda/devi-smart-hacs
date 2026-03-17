import unittest

from pydevi.discovery import house_config_from_dict
from pydevi.dominion.codec import DominionPacket
from pydevi.models import ThermostatInfo
from pydevi.sdg.grid import DEVICEGLOBAL, DOMINION_SYSTEM, GLOBAL_SERIALNUMBER, SYSTEM_HOUSE_NAME, SYSTEM_ROOM_NAME, SYSTEM_ZONE_NAME, GridSession


class _FakeConnection:
    def __init__(self, responses: list[bytes]) -> None:
        self._responses = responses
        self.closed = False
        self.request_payloads: list[bytes] = []

    async def request_peer_data(self, payload: bytes, first_chunk_timeout: float = 5.0, idle_timeout: float = 1.0) -> list[bytes]:
        self.request_payloads.append(payload)
        return self._responses

    async def close(self) -> None:
        self.closed = True


class DiscoveryEnrichmentTest(unittest.IsolatedAsyncioTestCase):
    async def test_enrich_thermostat_info_updates_metadata(self) -> None:
        response = b"".join(
            (
                DominionPacket.from_int(DEVICEGLOBAL, GLOBAL_SERIALNUMBER, 12345678).to_wire_bytes(),
                DominionPacket.from_string(DOMINION_SYSTEM, SYSTEM_ROOM_NAME, "Bathroom").to_wire_bytes(),
                DominionPacket.from_string(DOMINION_SYSTEM, SYSTEM_HOUSE_NAME, "Home").to_wire_bytes(),
                DominionPacket.from_string(DOMINION_SYSTEM, SYSTEM_ZONE_NAME, "Upstairs").to_wire_bytes(),
            )
        )
        conn = _FakeConnection([response])
        session = object.__new__(GridSession)

        async def _connect_to_remote(peer_id_hex: str, protocol: str = "dominion-1.0"):
            self.assertEqual(peer_id_hex, "aa" * 32)
            self.assertEqual(protocol, "dominion-1.0")
            return conn

        session.connect_to_remote = _connect_to_remote  # type: ignore[method-assign]

        info = ThermostatInfo(peer_id="aa" * 32, room_name="Unknown")
        await session._enrich_thermostat_info(info)

        self.assertEqual(info.serial_number, "12345678")
        self.assertEqual(info.room_name, "Bathroom")
        self.assertEqual(info.zone, "Upstairs")
        self.assertEqual(info.metadata["house_name"], "Home")
        self.assertEqual(info.metadata["zone"], "Upstairs")
        self.assertTrue(conn.closed)
        self.assertEqual(len(conn.request_payloads), 1)

    def test_house_config_from_dict_keeps_zone_and_metadata(self) -> None:
        config = house_config_from_dict(
            {
                "house_id": "house-1",
                "user_name": "ha-user",
                "private_key": "ab" * 32,
                "house_name": "My House",
                "thermostats": [
                    {
                        "peer_id": "01" * 32,
                        "room_name": "Bathroom",
                        "serial_number": "111",
                        "zone": "Upstairs",
                        "metadata": {"floor": "1"},
                    }
                ],
            }
        )
        self.assertEqual(config.house_name, "My House")
        self.assertEqual(config.thermostats[0].zone, "Upstairs")
        self.assertEqual(config.thermostats[0].metadata["floor"], "1")


if __name__ == "__main__":
    unittest.main()
