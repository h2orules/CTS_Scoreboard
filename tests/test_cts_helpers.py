import CTS_Scoreboard
from CTS_Scoreboard import hex_to_digit
from sim import _format_lane_time


class TestHexToDigit:
    def test_digit_zero(self):
        # (0x0F & 0x0F) ^ 0x0F = 0 → '0'
        assert hex_to_digit(0x0F) == "0"

    def test_digit_one(self):
        # (0x0E & 0x0F) ^ 0x0F = 1 → '1'
        assert hex_to_digit(0x0E) == "1"

    def test_digit_five(self):
        # (0x0A & 0x0F) ^ 0x0F = 5 → '5'
        assert hex_to_digit(0x0A) == "5"

    def test_digit_nine(self):
        # (0x06 & 0x0F) ^ 0x0F = 9 → '9'
        assert hex_to_digit(0x06) == "9"

    def test_space_for_value_above_nine(self):
        # (0x05 & 0x0F) ^ 0x0F = 10 > 9 → ' '
        assert hex_to_digit(0x05) == " "

    def test_space_for_zero_input(self):
        # (0x00 & 0x0F) ^ 0x0F = 15 > 9 → ' '
        assert hex_to_digit(0x00) == " "

    def test_upper_nibble_masked(self):
        # Upper nibble should be ignored
        assert hex_to_digit(0xF6) == "9"
        assert hex_to_digit(0xFF) == "0"
        assert hex_to_digit(0xAE) == "1"

    def test_all_digits(self):
        expected = {
            0x0F: "0",
            0x0E: "1",
            0x0D: "2",
            0x0C: "3",
            0x0B: "4",
            0x0A: "5",
            0x09: "6",
            0x08: "7",
            0x07: "8",
            0x06: "9",
        }
        for val, digit in expected.items():
            assert hex_to_digit(val) == digit


class TestFormatLaneTime:
    def test_sub_minute_final(self):
        result = _format_lane_time(25.43)
        assert result.strip() == "25.43"
        assert len(result) == 8

    def test_over_minute_final(self):
        result = _format_lane_time(65.50)
        assert "1:" in result
        assert "5.50" in result

    def test_running_mode(self):
        result = _format_lane_time(25.4, final=False)
        assert result.strip() == "25.4"
        assert len(result) == 8

    def test_running_mode_no_hundredths(self):
        result = _format_lane_time(5.23, final=False)
        # Running mode shows only tenths
        assert "." in result.strip()
        parts = result.strip().split(".")
        assert len(parts[1]) == 1  # Only tenths digit

    def test_zero_seconds(self):
        result = _format_lane_time(0.0)
        assert result.strip() == "0.00"

    def test_exactly_60_seconds(self):
        result = _format_lane_time(60.0)
        assert "1:" in result
        assert "0.00" in result

    def test_right_justified_8_chars(self):
        result = _format_lane_time(5.23)
        assert len(result) == 8
        assert result[0] == " "

    def test_large_time(self):
        result = _format_lane_time(125.99)
        assert result.strip().startswith("2:")

    def test_single_digit_seconds(self):
        result = _format_lane_time(9.99)
        assert len(result) == 8
        assert result.strip() == "9.99"

    def test_over_minute_running(self):
        result = _format_lane_time(75.3, final=False)
        assert "1:" in result
        assert len(result) == 8


def _digit_byte(slot, digit):
    """Build a CTS time_info byte: high nibble = slot, low nibble = inverted digit."""
    # hex_to_digit(c): (c & 0xF) ^ 0xF; "0".."9" → low nibble = 0xF..0x6
    return (slot << 4) | ((0x0F - digit) & 0x0F)


def _blank_byte(slot):
    return (slot << 4) | 0x00  # low nibble 0 → hex_to_digit returns " "


def _call_parse_line_running_time(time_info_bytes):
    """Run parse_line with a single running-time frame (channel 0, BE-style)
    and return the resulting CTS_Scoreboard.running_time global."""
    # Build the frame: byte 0 has channel bits set for channel 0 + running.
    # ((c & 0x3E) >> 1) ^ 0x1F == 0  →  c & 0x3E == 0x3E. Add the 0x80
    # high bit observed in real CTS traces; bit 6 (0x40) marks "running".
    frame = [0xBE] + list(time_info_bytes)
    CTS_Scoreboard.time_info = [0] * 16
    CTS_Scoreboard.running_time = "        "
    # parse_line broadcasts (and clears update) when running_time is set,
    # so read the module-level running_time global it writes en route.
    CTS_Scoreboard.parse_line(frame)
    return CTS_Scoreboard.running_time


class TestRunningTimeFormat:
    def test_tod_clock_no_trailing_period(self):
        """When CTS sends HH:MM with blank SS (TOD clock in Blank/idle),
        the formatted running_time must not include a stray '.' between
        the minutes and the blank seconds slot."""
        # HH=12, MM=34, SS blank
        bytes_ = [
            _digit_byte(2, 1),
            _digit_byte(3, 2),
            _digit_byte(4, 3),
            _digit_byte(5, 4),
            _blank_byte(6),
            _blank_byte(7),
        ]
        rt = _call_parse_line_running_time(bytes_)
        assert rt is not None
        assert rt == "12:34   "
        assert "." not in rt

    def test_full_time_has_period(self):
        """Sanity: when seconds digits are present, the '.' separator
        between MM and SS is still emitted."""
        # 1:23.45 — first slot blank, then 1, 2, 3, 4, 5
        bytes_ = [
            _blank_byte(2),
            _digit_byte(3, 1),
            _digit_byte(4, 2),
            _digit_byte(5, 3),
            _digit_byte(6, 4),
            _digit_byte(7, 5),
        ]
        rt = _call_parse_line_running_time(bytes_)
        assert rt == " 1:23.45"
