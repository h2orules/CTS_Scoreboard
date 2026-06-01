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


def _call_parse_line_lane_time(channel, time_info_bytes):
    """Run parse_line with a finished-lane frame and return update[lane_timeN].

    Channel encoding (decoded): channel_bits = ((c & 0x3E) >> 1) ^ 0x1F.
    For channel N: c = ((0x1F ^ N) << 1) & 0x3E, OR'd with bits 6/7 to
    indicate the high-bit framing used by real CTS frames. Bit 0 = 0
    (finish, not running), bit 6 = 0 (finished).
    """
    chan_bits = ((0x1F ^ channel) << 1) & 0x3E
    first = 0x80 | chan_bits  # high bit observed in real frames
    frame = [first] + list(time_info_bytes)
    CTS_Scoreboard.lane_info[channel] = [0] * 16
    CTS_Scoreboard.update.clear()
    CTS_Scoreboard.parse_line(frame)
    return CTS_Scoreboard.update.get("lane_time%d" % channel)


class TestLaneTimeFormat:
    def test_lane3_tod_clock_no_trailing_period(self):
        """Blank-state large clock is fed from lane 3. CTS sends HH:MM
        with blank SS in Blank/idle; the formatted lane_time must not
        include a stray '.' between MM and the blank SS slot.
        Regression for the "9:14." artifact seen on the Blank screen."""
        # Lane byte slots 0,1 hold lane/place; time digits live in 2..7.
        # Lane=blank, Place=blank, HH= 9, MM=14, SS blank.
        bytes_ = [
            _blank_byte(0),
            _blank_byte(1),
            _blank_byte(2),
            _digit_byte(3, 9),
            _digit_byte(4, 1),
            _digit_byte(5, 4),
            _blank_byte(6),
            _blank_byte(7),
        ]
        lt = _call_parse_line_lane_time(3, bytes_)
        assert lt is not None
        assert "." not in lt
        assert lt.strip() == "9:14"

    def test_lane_full_finish_time_has_period(self):
        """Sanity: a finished swim time like 1:23.45 still emits the '.'."""
        bytes_ = [
            _digit_byte(0, 1),  # lane
            _digit_byte(1, 1),  # place
            _blank_byte(2),
            _digit_byte(3, 1),
            _digit_byte(4, 2),
            _digit_byte(5, 3),
            _digit_byte(6, 4),
            _digit_byte(7, 5),
        ]
        lt = _call_parse_line_lane_time(1, bytes_)
        assert lt == " 1:23.45"


def _lane_running_frame(channel):
    """Build a CTS frame for a lane channel transitioning to 'running'.

    Decoded channel bits: ((c & 0x3E) >> 1) ^ 0x1F == channel.
    Bit 6 (0x40) marks the running flag; bit 0 (0x01) is format_display
    (must be 0 for the parser to treat this as a lane channel).
    """
    chan_bits = ((0x1F ^ channel) << 1) & 0x3E
    first = 0x80 | 0x40 | chan_bits
    # Payload: lane and place set, time slots blank (running clock owns time).
    return [
        first,
        _digit_byte(0, channel),  # lane
        _digit_byte(1, channel),  # place
        _blank_byte(2),
        _blank_byte(3),
        _blank_byte(4),
        _blank_byte(5),
        _blank_byte(6),
        _blank_byte(7),
    ]


class TestRaceStartClockReset:
    """When the operator starts a race, the CTS clock state still holds
    the prior race's MM/SS digits until the next .0 boundary delivers a
    full BE frame. parse_line must blank time_info[2..7] and running_time
    on the first lane->running edge so the bridge tenths-only frames
    don't render stale digits (e.g. "30:08.4" instead of "0.4")."""

    def _reset_state(self):
        # Mimic state right before a race starts: prior clock was at
        # "30:08" (running) and tenths just ticked.
        CTS_Scoreboard.time_info = [0] * 16
        # MM = 30, SS = 08
        CTS_Scoreboard.time_info[2] = _digit_byte(2, 3)
        CTS_Scoreboard.time_info[3] = _digit_byte(3, 0)
        CTS_Scoreboard.time_info[4] = _digit_byte(4, 0)
        CTS_Scoreboard.time_info[5] = _digit_byte(5, 8)
        CTS_Scoreboard.time_info[6] = _digit_byte(6, 4)
        CTS_Scoreboard.time_info[7] = _blank_byte(7)
        CTS_Scoreboard.running_time = "30:08.4 "
        CTS_Scoreboard.channel_running = [False] * 10

    def test_first_lane_running_blanks_time_info_and_running_time(self, monkeypatch):
        self._reset_state()
        CTS_Scoreboard.update.clear()
        # parse_line broadcasts + clears `update` in its finally block
        # when running_time is queued, so capture the broadcast payload.
        captured = {}
        monkeypatch.setattr(
            CTS_Scoreboard, "broadcast_scoreboard", lambda u: captured.update(u)
        )
        CTS_Scoreboard.parse_line(_lane_running_frame(1))
        # MM, seconds-tens, and tenths/hundredths blank; seconds-ones
        # seeded with the digit "0" so the bridge renders as "0.X".
        for slot in (2, 3, 4, 6, 7):
            assert (CTS_Scoreboard.time_info[slot] & 0x0F) == 0, (
                "slot %d not blanked: 0x%02X" % (slot, CTS_Scoreboard.time_info[slot])
            )
        assert (CTS_Scoreboard.time_info[5] & 0x0F) == 0xF
        assert CTS_Scoreboard.running_time == "    0   "
        # The reset running_time should have gone out on the wire.
        assert captured.get("running_time") == "    0   "

    def test_subsequent_lane_does_not_reset(self):
        """Once one lane is already running, a second lane joining must
        NOT re-blank the clock (which by then may hold real race data)."""
        self._reset_state()
        # Pretend lane 1 already went running on a prior frame.
        CTS_Scoreboard.channel_running[0] = True
        CTS_Scoreboard.running_time = "     1.2"
        prior_time_info = list(CTS_Scoreboard.time_info)
        CTS_Scoreboard.update.clear()
        CTS_Scoreboard.parse_line(_lane_running_frame(2))
        assert CTS_Scoreboard.running_time == "     1.2"
        assert CTS_Scoreboard.time_info == prior_time_info
        assert "running_time" not in CTS_Scoreboard.update

    def test_tenths_only_frame_after_reset_renders_blank_mmss(self):
        """The frame the user actually sees right after race-start is a
        tenths-only BE update. After our reset it should render with
        blank MM/SS, not the stale prior clock."""
        self._reset_state()
        CTS_Scoreboard.update.clear()
        CTS_Scoreboard.parse_line(_lane_running_frame(1))
        # Tenths-only BE frame carrying ".9" (slot 6 = tenths digit,
        # slot 7 = hundredths blank).
        CTS_Scoreboard.parse_line(
            [0xBE, _digit_byte(6, 9), _blank_byte(7)]
        )
        rt = CTS_Scoreboard.running_time
        # No "30:08" or any prior-MM/SS digits should be in the output.
        assert "30" not in rt and "08" not in rt
        # Should render as "0.9" with a trailing blank hundredths slot.
        assert "0.9" in rt
