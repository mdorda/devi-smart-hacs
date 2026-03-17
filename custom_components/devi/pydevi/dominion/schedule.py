from __future__ import annotations

from dataclasses import dataclass
import re

DAY_NAMES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
FIRST_PART_LENGTH = 4
SECOND_PART_LENGTH = 6
DAY_BYTE_LENGTH = FIRST_PART_LENGTH + SECOND_PART_LENGTH
RANGES_PER_DAY = 5
FULL_SCHEDULE_LENGTH = len(DAY_NAMES) * DAY_BYTE_LENGTH
TIME_RANGE_RE = re.compile(r"^\s*(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})\s*$")


class ScheduleCodecError(ValueError):
    """Raised when a weekly schedule cannot be parsed or encoded."""


@dataclass(slots=True, frozen=True)
class WeeklySchedulePlan:
    days: dict[str, list[str]]


def combine_schedule_parts(week_1_raw: bytes, week_2_raw: bytes | None) -> bytes:
    week_1 = _strip_optional_length_prefix(week_1_raw, FIRST_PART_LENGTH * len(DAY_NAMES))
    second = _strip_optional_length_prefix(week_2_raw or b"", SECOND_PART_LENGTH * len(DAY_NAMES))
    if len(week_1_raw) != FIRST_PART_LENGTH * len(DAY_NAMES):
        if len(week_1) != FIRST_PART_LENGTH * len(DAY_NAMES):
            raise ScheduleCodecError("week_1 payload must be 28 bytes")
    if len(second) != SECOND_PART_LENGTH * len(DAY_NAMES):
        raise ScheduleCodecError("week_2 payload must be 42 bytes")
    combined = bytearray(FULL_SCHEDULE_LENGTH)
    for day_index in range(len(DAY_NAMES)):
        src_first = day_index * FIRST_PART_LENGTH
        src_second = day_index * SECOND_PART_LENGTH
        dst = day_index * DAY_BYTE_LENGTH
        combined[dst : dst + FIRST_PART_LENGTH] = week_1[src_first : src_first + FIRST_PART_LENGTH]
        combined[dst + FIRST_PART_LENGTH : dst + DAY_BYTE_LENGTH] = second[src_second : src_second + SECOND_PART_LENGTH]
    return bytes(combined)


def split_schedule_parts(combined: bytes) -> tuple[bytes, bytes]:
    if len(combined) != FULL_SCHEDULE_LENGTH:
        raise ScheduleCodecError("combined weekly schedule must be 70 bytes")
    first = bytearray(FIRST_PART_LENGTH * len(DAY_NAMES))
    second = bytearray(SECOND_PART_LENGTH * len(DAY_NAMES))
    for day_index in range(len(DAY_NAMES)):
        src = day_index * DAY_BYTE_LENGTH
        dst_first = day_index * FIRST_PART_LENGTH
        dst_second = day_index * SECOND_PART_LENGTH
        first[dst_first : dst_first + FIRST_PART_LENGTH] = combined[src : src + FIRST_PART_LENGTH]
        second[dst_second : dst_second + SECOND_PART_LENGTH] = combined[src + FIRST_PART_LENGTH : src + DAY_BYTE_LENGTH]
    return bytes([len(first)]) + bytes(first), bytes([len(second)]) + bytes(second)


def decode_week_schedule(week_1_raw: bytes, week_2_raw: bytes | None) -> WeeklySchedulePlan:
    combined = combine_schedule_parts(week_1_raw, week_2_raw)
    days: dict[str, list[str]] = {}
    for day_index, day_name in enumerate(DAY_NAMES):
        start = day_index * DAY_BYTE_LENGTH
        slots: list[str] = []
        for period in range(RANGES_PER_DAY):
            range_bytes = combined[start + period * 2 : start + period * 2 + 2]
            if range_bytes == b"\x00\x00":
                continue
            slots.append(decode_time_range(range_bytes.hex()))
        days[day_name] = slots
    return WeeklySchedulePlan(days=days)


def encode_week_schedule(days: dict[str, list[str]]) -> tuple[bytes, bytes]:
    combined = bytearray(FULL_SCHEDULE_LENGTH)
    for day_index, day_name in enumerate(DAY_NAMES):
        if day_name not in days:
            raise ScheduleCodecError(f"missing schedule day {day_name}")
        ranges = list(days[day_name])
        if len(ranges) > RANGES_PER_DAY:
            raise ScheduleCodecError(f"too many ranges for {day_name}; max {RANGES_PER_DAY}")
        _validate_day_ranges(ranges)
        day_start = day_index * DAY_BYTE_LENGTH
        for period, time_range in enumerate(ranges):
            combined[day_start + period * 2 : day_start + period * 2 + 2] = bytes.fromhex(encode_time_range(time_range))
    return split_schedule_parts(bytes(combined))


def parse_day_ranges_text(value: str) -> list[str]:
    if not value.strip():
        return []
    ranges = [item.strip() for item in value.split(",") if item.strip()]
    for time_range in ranges:
        _normalize_time_range(time_range)
    _validate_day_ranges(ranges)
    return ranges


def format_day_ranges_text(ranges: list[str]) -> str:
    return ", ".join(ranges)


def encode_time_range(time_range: str) -> str:
    start, end = _normalize_time_range(time_range)
    return _encode_part(start) + _encode_part(end)


def decode_time_range(hex_code: str) -> str:
    if len(hex_code) != 4:
        raise ScheduleCodecError("time range must be exactly 4 hex characters")
    if hex_code == "0000":
        return "00:00-00:00"
    return f"{_decode_part(hex_code[:2])}-{_decode_part(hex_code[2:])}"


def _validate_day_ranges(ranges: list[str]) -> None:
    bit_masks: list[int] = []
    for time_range in ranges:
        start, end = _normalize_time_range(time_range)
        start_bit = _time_to_bit_index(start)
        end_bit = _time_to_bit_index(end)
        if end_bit <= start_bit:
            raise ScheduleCodecError("schedule range end must be after start")
        bit_mask = 0
        for index in range(start_bit, end_bit):
            bit_mask |= 1 << index
        for existing in bit_masks:
            if existing & bit_mask:
                raise ScheduleCodecError("schedule ranges overlap or are duplicated")
        bit_masks.append(bit_mask)


def _normalize_time_range(time_range: str) -> tuple[str, str]:
    match = TIME_RANGE_RE.match(time_range)
    if not match:
        raise ScheduleCodecError(f"invalid time range: {time_range}")
    start = _normalize_time(match.group(1))
    end = _normalize_time(match.group(2))
    return start, end


def _normalize_time(value: str) -> str:
    hours, minutes = (int(part) for part in value.split(":"))
    if minutes not in (0, 30):
        raise ScheduleCodecError("schedule supports only 30-minute intervals")
    if hours < 0 or hours > 24:
        raise ScheduleCodecError("schedule hour out of range")
    if hours == 24 and minutes != 0:
        raise ScheduleCodecError("24:00 is the latest supported time")
    return f"{hours:02}:{minutes:02}"


def _encode_part(value: str) -> str:
    hours, minutes = (int(part) for part in value.split(":"))
    if hours < 16:
        prefix = "8" if minutes == 0 else "c"
        return f"{prefix}{hours:x}"
    prefix = "9" if minutes == 0 else "d"
    return f"{prefix}{hours - 16:x}"


def _decode_part(hex_part: str) -> str:
    prefix = hex_part[0]
    if prefix in {"a", "b"}:
        prefix = chr(ord(prefix) - 1)
    if prefix in {"e", "f"}:
        prefix = chr(ord(prefix) - 2)
    if prefix not in {"8", "9", "c", "d"}:
        raise ScheduleCodecError(f"invalid schedule hex part: {hex_part}")
    minutes = "00" if prefix in {"8", "9"} else "30"
    hours = int(hex_part[1], 16)
    if prefix in {"9", "d"}:
        hours += 16
    return f"{hours:02}:{minutes}"


def _time_to_bit_index(value: str) -> int:
    hours, minutes = (int(part) for part in value.split(":"))
    return hours * 2 + (1 if minutes == 30 else 0)


def _strip_optional_length_prefix(payload: bytes, expected_length: int) -> bytes:
    if not payload:
        return bytes(expected_length)
    if len(payload) == expected_length:
        return payload
    if len(payload) == expected_length + 1 and payload[0] == expected_length:
        return payload[1:]
    return payload
