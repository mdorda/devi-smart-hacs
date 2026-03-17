import unittest

from pydevi.sdg import control


class ControlProtocolTest(unittest.TestCase):
    def test_protocol_version_roundtrip(self) -> None:
        version = control.ProtocolVersion()
        encoded = control.encode_protocol_version(version)
        decoded = control.decode_protocol_version(encoded)
        self.assertEqual(decoded.magic, version.magic)
        self.assertEqual(decoded.major, version.major)
        self.assertEqual(decoded.minor, version.minor)

    def test_peer_reply_roundtrip(self) -> None:
        peer_info = (
            control._encode_string(3, "abcd" * 16)
            + control._encode_bytes(
                4,
                control._encode_string(1, "example.com") + control._encode_uint32(2, 443),
            )
            + control._encode_bytes(5, b"tunnel")
        )
        payload = control._encode_uint32(1, 7) + control._encode_uint32(2, 0) + control._encode_bytes(3, peer_info)
        reply = control.decode_peer_reply(payload)
        self.assertEqual(reply.id, 7)
        self.assertEqual(reply.peer.server.host, "example.com")
        self.assertEqual(reply.peer.server.port, 443)
        self.assertEqual(reply.peer.tunnel_id, b"tunnel")


if __name__ == "__main__":
    unittest.main()
