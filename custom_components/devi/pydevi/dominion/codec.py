from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import struct

from ..models import AwayInterval


@dataclass(slots=True)
class DominionPacket:
    msg_class: int
    msg_code: int
    payload: bytes = b""
    is_read_request: bool = False

    HEADER_SIZE = 4

    @classmethod
    def parse_stream(cls, data: bytes) -> list["DominionPacket"]:
        packets: list[DominionPacket] = []
        offset = 0
        while len(data) - offset >= cls.HEADER_SIZE:
            packet = cls.from_bytes(data[offset:])
            packets.append(packet)
            offset += packet.packet_length
        return packets

    @classmethod
    def from_bytes(cls, data: bytes) -> "DominionPacket":
        if len(data) < cls.HEADER_SIZE:
            raise ValueError("data too short for Dominion packet")

        msg_class = data[0]
        msg_code = struct.unpack_from("<H", data, 1)[0]
        payload_len = data[3]
        packet_len = cls.HEADER_SIZE + payload_len
        if len(data) < packet_len:
            raise ValueError("payload exceeds buffer length")
        return cls(msg_class=msg_class, msg_code=msg_code, payload=data[4:packet_len])

    @property
    def packet_length(self) -> int:
        return self.HEADER_SIZE + len(self.payload)

    def to_wire_bytes(self) -> bytes:
        prefix = b"\x01" if self.is_read_request or not self.payload else b"\x00"
        header = struct.pack("<BHB", self.msg_class, self.msg_code, len(self.payload))
        return prefix + header + self.payload

    @classmethod
    def read_request(cls, msg_class: int, msg_code: int) -> "DominionPacket":
        return cls(msg_class=msg_class, msg_code=msg_code, payload=b"", is_read_request=True)

    @classmethod
    def from_bool(cls, msg_class: int, msg_code: int, value: bool) -> "DominionPacket":
        return cls(msg_class=msg_class, msg_code=msg_code, payload=bytes([1 if value else 0]))

    @classmethod
    def from_byte(cls, msg_class: int, msg_code: int, value: int) -> "DominionPacket":
        return cls(msg_class=msg_class, msg_code=msg_code, payload=struct.pack("<B", value))

    @classmethod
    def from_short(cls, msg_class: int, msg_code: int, value: int) -> "DominionPacket":
        return cls(msg_class=msg_class, msg_code=msg_code, payload=struct.pack("<H", value))

    @classmethod
    def from_int(cls, msg_class: int, msg_code: int, value: int) -> "DominionPacket":
        return cls(msg_class=msg_class, msg_code=msg_code, payload=struct.pack("<I", value))

    @classmethod
    def from_decimal(cls, msg_class: int, msg_code: int, value_c: float) -> "DominionPacket":
        return cls(msg_class=msg_class, msg_code=msg_code, payload=struct.pack("<h", round(value_c * 100)))

    @classmethod
    def from_array(cls, msg_class: int, msg_code: int, payload: bytes) -> "DominionPacket":
        return cls(msg_class=msg_class, msg_code=msg_code, payload=bytes([len(payload)]) + payload)

    @classmethod
    def from_string(cls, msg_class: int, msg_code: int, value: str) -> "DominionPacket":
        return cls.from_array(msg_class, msg_code, value.encode())

    def get_byte(self) -> int:
        return self.payload[0]

    def get_short(self) -> int:
        return struct.unpack_from("<H", self.payload, 0)[0]

    def get_int(self) -> int:
        return struct.unpack_from("<I", self.payload, 0)[0]

    def get_bool(self) -> bool:
        return bool(self.get_byte())

    def get_decimal(self) -> float | None:
        value = struct.unpack_from("<h", self.payload, 0)[0]
        if value == -32768:
            return None
        return value / 100.0

    def get_array(self) -> bytes:
        if not self.payload:
            return b""
        length = self.payload[0]
        return self.payload[1 : 1 + length]

    def get_string(self) -> str:
        value = self.get_array()
        if value and value[0] == 0:
            return ""
        return value.decode(errors="ignore")

    def get_version(self) -> str:
        version = self.get_short()
        return f"{version >> 8}.{version & 0xFF}"

    def get_date(self, offset: int = 0) -> datetime:
        sec, minute, hour_raw, day_raw, month_raw, year_raw = struct.unpack_from("<6B", self.payload, offset)
        hour = hour_raw & 63
        day = day_raw & 31
        month = month_raw
        year = year_raw + 2000
        return datetime(year, month, day, hour, minute, sec, tzinfo=UTC)

    def get_away_interval(self) -> AwayInterval:
        # struct AwayInterval from opensdg: a size byte (14), then a validity flag and a 6-byte date for start and end.
        if len(self.payload) < 15 or self.payload[0] != 14:
            return AwayInterval(start=None, end=None)
        try:
            start = self.get_date(2) if self.payload[1] else None
            end = self.get_date(9) if self.payload[8] else None
        except ValueError:
            return AwayInterval(start=None, end=None)
        return AwayInterval(start=start, end=end)
