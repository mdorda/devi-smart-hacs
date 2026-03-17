import unittest

from pydevi.dominion.codec import DominionPacket


class DominionCodecTest(unittest.TestCase):
    def test_decimal_roundtrip(self) -> None:
        packet = DominionPacket.from_decimal(6, 29299, 21.5)
        self.assertEqual(packet.get_decimal(), 21.5)

    def test_stream_parse(self) -> None:
        packet_a = DominionPacket.from_byte(7, 29329, 1)
        packet_b = DominionPacket.from_bool(5, 29240, True)
        stream = packet_a.to_wire_bytes()[1:] + packet_b.to_wire_bytes()[1:]
        packets = DominionPacket.parse_stream(stream)
        self.assertEqual(len(packets), 2)
        self.assertEqual(packets[0].msg_code, 29329)
        self.assertEqual(packets[1].msg_code, 29240)

    def test_string_roundtrip(self) -> None:
        packet = DominionPacket.from_string(5, 29250, "Living room")
        self.assertEqual(packet.get_string(), "Living room")


if __name__ == "__main__":
    unittest.main()
