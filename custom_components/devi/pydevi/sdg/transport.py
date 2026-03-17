from __future__ import annotations

from dataclasses import dataclass
import struct

from .crypto import PACKET_MAGIC


CMD_TELL = b"TELL"
CMD_WELC = b"WELC"
CMD_HELO = b"HELO"
CMD_COOK = b"COOK"
CMD_VOCH = b"VOCH"
CMD_REDY = b"REDY"
CMD_MESG = b"MESG"


@dataclass(slots=True)
class PacketHeader:
    size: int
    magic: bytes
    command: bytes

    STRUCT = struct.Struct(">H")
    MAGIC = b"\xF0\x9F\x90\x9F"

    @classmethod
    def parse(cls, data: bytes) -> "PacketHeader":
        if len(data) < 10:
            raise ValueError("buffer too short for packet header")
        size = cls.STRUCT.unpack_from(data, 0)[0]
        return cls(size=size, magic=data[2:6], command=data[6:10])

    @property
    def packet_size(self) -> int:
        return self.size + 2

    def to_bytes(self) -> bytes:
        return self.STRUCT.pack(self.size) + self.magic + self.command


def build_header(command: bytes, size: int) -> bytes:
    return struct.pack(">H", size - 2) + PacketHeader.MAGIC + command


def build_data_packet(data_type: int, payload: bytes) -> bytes:
    size = len(payload) + 1
    return struct.pack(">H", size) + bytes([data_type]) + payload
