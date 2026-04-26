import pytest

from CTS_Scoreboard import hex_to_digit, _format_lane_time


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
            0x0F: "0", 0x0E: "1", 0x0D: "2", 0x0C: "3",
            0x0B: "4", 0x0A: "5", 0x09: "6", 0x08: "7",
            0x07: "8", 0x06: "9",
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
