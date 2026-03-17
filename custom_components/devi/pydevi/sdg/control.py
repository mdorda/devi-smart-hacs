from __future__ import annotations

from dataclasses import dataclass
import struct


MSG_FORWARD_REMOTE = 0
MSG_PROTOCOL_VERSION = 1
MSG_FORWARD_HOLD = 1
MSG_FORWARD_REPLY = 2
MSG_FORWARD_ERROR = 3
MSG_PING = 4
MSG_PONG = 5
MSG_CALL_REMOTE = 10
MSG_REMOTE_REPLY = 11
MSG_INCOMING_CALL = 12
MSG_INCOMING_CALL_REPLY = 13
MSG_PAIR_REMOTE = 32
MSG_PAIR_REMOTE_REPLY = 33

PROTOCOL_VERSION_MAGIC = 0xF09D8CA8
PROTOCOL_VERSION_MAJOR = 1
PROTOCOL_VERSION_MINOR = 0
FORWARD_REMOTE_MAGIC = 0xF09D8C95
FORWARD_REMOTE_SIGNATURE = "Mdg-NaCl/binary"

FORWARD_SERVER_ERROR = 1
FORWARD_PEER_TIMEOUT = 4


@dataclass(slots=True)
class Endpoint:
    host: str
    port: int


@dataclass(slots=True)
class PeerInfo:
    peer_id: str
    server: Endpoint
    tunnel_id: bytes


@dataclass(slots=True)
class PeerReply:
    id: int
    result: int
    peer: PeerInfo | None = None


@dataclass(slots=True)
class ProtocolVersion:
    magic: int = PROTOCOL_VERSION_MAGIC
    major: int = PROTOCOL_VERSION_MAJOR
    minor: int = PROTOCOL_VERSION_MINOR


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        to_write = value & 0x7F
        value >>= 7
        if value:
            out.append(to_write | 0x80)
        else:
            out.append(to_write)
            return bytes(out)


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, offset
        shift += 7


def _field_key(field_number: int, wire_type: int) -> bytes:
    return _encode_varint((field_number << 3) | wire_type)


def _encode_uint32(field_number: int, value: int) -> bytes:
    return _field_key(field_number, 0) + _encode_varint(value)


def _encode_fixed32(field_number: int, value: int) -> bytes:
    return _field_key(field_number, 5) + struct.pack("<I", value)


def _encode_bytes(field_number: int, value: bytes) -> bytes:
    return _field_key(field_number, 2) + _encode_varint(len(value)) + value


def _encode_string(field_number: int, value: str) -> bytes:
    return _encode_bytes(field_number, value.encode())


def _parse_message(data: bytes) -> dict[int, list[tuple[int, object]]]:
    fields: dict[int, list[tuple[int, object]]] = {}
    offset = 0
    while offset < len(data):
        key, offset = _decode_varint(data, offset)
        field_no = key >> 3
        wire_type = key & 0x07
        if wire_type == 0:
            value, offset = _decode_varint(data, offset)
        elif wire_type == 2:
            length, offset = _decode_varint(data, offset)
            value = data[offset : offset + length]
            offset += length
        elif wire_type == 5:
            value = struct.unpack_from("<I", data, offset)[0]
            offset += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        fields.setdefault(field_no, []).append((wire_type, value))
    return fields


def encode_protocol_version(version: ProtocolVersion) -> bytes:
    return b"".join(
        (
            _encode_fixed32(1, version.magic),
            _encode_uint32(2, version.major),
            _encode_uint32(3, version.minor),
        )
    )


def decode_protocol_version(data: bytes) -> ProtocolVersion:
    fields = _parse_message(data)
    return ProtocolVersion(
        magic=fields[1][0][1],
        major=fields[2][0][1],
        minor=fields[3][0][1],
    )


def encode_ping(seq: int, delay: int | None = None) -> bytes:
    payload = _encode_uint32(1, seq)
    if delay is not None:
        payload += _encode_uint32(2, delay)
    return payload


def decode_pong(data: bytes) -> int:
    return _parse_message(data)[1][0][1]


def encode_connect_to_peer(request_id: int, peer_id: str, protocol: str) -> bytes:
    return b"".join(
        (
            _encode_uint32(1, request_id),
            _encode_string(2, peer_id),
            _encode_string(3, protocol),
        )
    )


def encode_pair_remote(request_id: int, otp_server_part: str) -> bytes:
    return b"".join((_encode_uint32(1, request_id), _encode_string(2, otp_server_part)))


def encode_incoming_call_reply(request_id: int, result: int) -> bytes:
    return b"".join((_encode_uint32(1, request_id), _encode_uint32(2, result)))


def encode_forward_remote(tunnel_id: bytes) -> bytes:
    return b"".join(
        (
            _encode_fixed32(1, FORWARD_REMOTE_MAGIC),
            _encode_uint32(2, PROTOCOL_VERSION_MAJOR),
            _encode_uint32(3, PROTOCOL_VERSION_MINOR),
            _encode_bytes(4, tunnel_id),
            _encode_string(5, FORWARD_REMOTE_SIGNATURE),
        )
    )


def decode_forward_reply(data: bytes) -> str:
    fields = _parse_message(data)
    return fields[1][0][1].decode()


def decode_forward_error(data: bytes) -> int:
    return _parse_message(data)[1][0][1]


def _decode_endpoint(data: bytes) -> Endpoint:
    fields = _parse_message(data)
    return Endpoint(host=fields[1][0][1].decode(), port=fields[2][0][1])


def _decode_peer_info(data: bytes) -> PeerInfo:
    fields = _parse_message(data)
    return PeerInfo(
        peer_id=fields[3][0][1].decode(),
        server=_decode_endpoint(fields[4][0][1]),
        tunnel_id=fields[5][0][1],
    )


def decode_peer_reply(data: bytes) -> PeerReply:
    fields = _parse_message(data)
    peer = _decode_peer_info(fields[3][0][1]) if 3 in fields else None
    return PeerReply(id=fields[1][0][1], result=fields[2][0][1], peer=peer)
