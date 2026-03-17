import unittest

from pydevi.dominion.schedule import (
    ScheduleCodecError,
    combine_schedule_parts,
    decode_time_range,
    decode_week_schedule,
    encode_time_range,
    encode_week_schedule,
    format_day_ranges_text,
    parse_day_ranges_text,
    split_schedule_parts,
)


class ScheduleCodecTest(unittest.TestCase):
    def test_encode_decode_time_range(self) -> None:
        self.assertEqual(encode_time_range("06:00-08:30"), "86c8")
        self.assertEqual(encode_time_range("16:00-18:30"), "90d2")
        self.assertEqual(decode_time_range("90d2"), "16:00-18:30")
        self.assertEqual(decode_time_range("87f6"), "07:00-22:30")

    def test_decode_live_schedule_payload(self) -> None:
        week_1 = bytes.fromhex("1c868890d6868890d6868890d6868890d6868890d687f6000087f60000")
        week_2 = bytes.fromhex("2a000000000000000000000000000000000000000000000000000000000000000000000000000000000000")
        plan = decode_week_schedule(week_1, week_2)
        self.assertEqual(plan.days["MON"], ["06:00-08:00", "16:00-22:30"])
        self.assertEqual(plan.days["SAT"], ["07:00-22:30"])
        self.assertEqual(plan.days["SUN"], ["07:00-22:30"])

    def test_encode_round_trip(self) -> None:
        days = {
            "MON": ["06:00-08:30", "16:00-22:30"],
            "TUE": ["06:00-08:30", "16:00-22:30"],
            "WED": ["06:00-08:30", "16:00-22:30"],
            "THU": ["06:00-08:30", "16:00-22:30"],
            "FRI": ["06:00-08:30", "16:00-22:30"],
            "SAT": ["07:00-22:30"],
            "SUN": ["07:00-22:30"],
        }
        week_1, week_2 = encode_week_schedule(days)
        decoded = decode_week_schedule(week_1, week_2)
        self.assertEqual(decoded.days, days)
        combined = combine_schedule_parts(week_1, week_2)
        self.assertEqual(split_schedule_parts(combined), (week_1, week_2))

    def test_parse_day_ranges_text(self) -> None:
        parsed = parse_day_ranges_text("06:00-08:30, 16:00-22:30")
        self.assertEqual(parsed, ["06:00-08:30", "16:00-22:30"])
        self.assertEqual(format_day_ranges_text(parsed), "06:00-08:30, 16:00-22:30")

    def test_reject_overlaps(self) -> None:
        with self.assertRaises(ScheduleCodecError):
            parse_day_ranges_text("06:00-08:30, 08:00-09:00")

    def test_reject_invalid_end(self) -> None:
        with self.assertRaises(ScheduleCodecError):
            parse_day_ranges_text("22:00-21:30")


if __name__ == "__main__":
    unittest.main()
