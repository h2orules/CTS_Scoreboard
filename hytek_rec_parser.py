"""
Parser for HyTek Meet Manager swim records files (.rec).

Usage as library:
    from hytek_rec_parser import parse_rec_file

    rec = parse_rec_file("BChampRecord-y.rec")
    print(rec.header.course)
    for r in rec.records:
        print(f"{r.swimmer_name} - {r.stroke} {r.distance} - {r.time_formatted}")
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

EPOCH = date(1970, 1, 1)

RECORD_LENGTH = 120

STROKES = {
    1: "Freestyle",
    2: "Backstroke",
    3: "Breaststroke",
    4: "Butterfly",
    5: "IM",
    6: "Freestyle Relay",
    7: "Medley Relay",
}

SEXES = {
    0: "Mixed",
    1: "Male",
    2: "Female",
}

COURSES = {
    "Y": "SCY",
    "S": "SCM",
    "L": "LCM",
}


@dataclass
class RecHeader:
    course: str
    course_code: str
    record_set_name: str
    software_version: str
    record_count: int | None
    export_date: date


@dataclass
class SwimRecord:
    sex: str
    sex_code: int
    stroke: str
    stroke_code: int
    distance: int
    age_group_min: int | None
    age_group_max: int | None
    event_type: str
    swimmer_name: str
    team: str
    relay_names: str | None
    record_date: date
    time_seconds: float
    time_formatted: str
    record_team: str
    entry_type: str


@dataclass
class RecFile:
    header: RecHeader
    records: list[SwimRecord]


def _mbf_single_to_float(data: bytes) -> float:
    """Convert a 4-byte Microsoft Binary Format single-precision float to a Python float.

    MBF Single layout (4 bytes):
        Byte 0: mantissa LSB
        Byte 1: mantissa middle
        Byte 2: sign (bit 7) + mantissa MSB (bits 6-0)
        Byte 3: exponent (biased by 128)

    Value = (-1)^sign * (0.5 + mantissa/2^24) * 2^(exponent - 128)
    """
    if len(data) != 4:
        raise ValueError(f"MBF single requires exactly 4 bytes, got {len(data)}")

    exponent = data[3]
    if exponent == 0:
        return 0.0

    sign = (data[2] >> 7) & 1
    mantissa_23 = ((data[2] & 0x7F) << 16) | (data[1] << 8) | data[0]
    mantissa_24 = 0x800000 | mantissa_23  # add hidden bit

    value = (mantissa_24 / (1 << 24)) * (2 ** (exponent - 128))
    if sign:
        value = -value

    return value


def _format_time(seconds: float) -> str:
    """Format a time in seconds as a swim time string.

    Returns "M:SS.HH" if >= 60 seconds, otherwise "SS.HH".
    """
    if seconds <= 0.0:
        return ""

    if seconds < 0:
        return f"-{_format_time(-seconds)}"

    hundredths = round(seconds * 100)
    mins, remainder = divmod(hundredths, 6000)
    secs, hunds = divmod(remainder, 100)

    if mins > 0:
        return f"{mins}:{secs:02d}.{hunds:02d}"
    else:
        return f"{secs}.{hunds:02d}"


def _yy_to_yyyy(yy: int) -> int:
    """Convert a 2-digit year to 4-digit using a sliding window.

    Years 0-68 map to 2000-2068, years 69-99 map to 1969-1999.
    """
    return 2000 + yy if yy <= 68 else 1900 + yy


def _parse_date(raw: str) -> date:
    """Parse a 6-char MMDDYY date field into a date object.

    The field can contain:
      - Full MMDDYY (e.g. "071424" -> date(2024, 7, 14))
      - MMYY with no day (e.g. "  0724" -> date(2024, 7, 1))
      - Year only, right-justified (e.g. "    19" -> date(2019, 1, 1))
      - Blank/empty -> EPOCH (treated as "no date")

    If the year portion is missing/blank, returns EPOCH regardless of
    whether month or day are present.
    """
    if len(raw) < 6:
        raw = raw.rjust(6)

    mm_raw = raw[0:2].strip()
    dd_raw = raw[2:4].strip()
    yy_raw = raw[4:6].strip()

    # Year is required — without it there's no meaningful date
    if not yy_raw or not yy_raw.isdigit():
        return EPOCH

    year = _yy_to_yyyy(int(yy_raw))

    # Parse month (0 or blank means unknown)
    month = int(mm_raw) if mm_raw.isdigit() and int(mm_raw) >= 1 else 0

    # Parse day (0 or blank means unknown)
    day = int(dd_raw) if dd_raw.isdigit() and int(dd_raw) >= 1 else 0

    # If we have day but no month, treat as year-only
    if day and not month:
        return date(year, 1, 1)

    if not month:
        return date(year, 1, 1)
    if not day:
        return date(year, month, 1)
    return date(year, month, day)


def format_record_date(d: date) -> str:
    """Format a record date for display.

    - EPOCH (no date) -> ""
    - Year-only (month=1, day=1) -> "2019"
    - Month+year (day=1) -> "July 2024"
    - Full date -> "07/14/2024"
    """
    if d == EPOCH:
        return ""
    if d.month == 1 and d.day == 1:
        return str(d.year)
    if d.day == 1:
        return d.strftime("%B %Y")
    return d.strftime("%m/%d/%Y")


def _parse_header(data: bytes) -> RecHeader:
    """Parse a 120-byte header record."""
    identifier = data[0:3].decode("ascii", errors="replace").strip()
    if identifier != "REC":
        raise ValueError(f"Expected header identifier 'REC', got '{identifier}'")

    # Byte 6: record count (ASCII digit(s))
    count_raw = data[6:7].decode("ascii", errors="replace").strip()
    record_count = int(count_raw) if count_raw.isdigit() else None

    # Bytes 7-12: export date (MMDDYY)
    export_date = _parse_date(data[7:13].decode("ascii", errors="replace"))

    course_code = data[14:15].decode("ascii", errors="replace").strip()
    course = COURSES.get(course_code, f"Unknown ({course_code})")

    record_set_name = data[15:30].decode("ascii", errors="replace").strip()
    software_version = data[30:45].decode("ascii", errors="replace").strip()

    return RecHeader(
        course=course,
        course_code=course_code,
        record_set_name=record_set_name,
        software_version=software_version,
        record_count=record_count,
        export_date=export_date,
    )


def _parse_record(data: bytes) -> SwimRecord:
    """Parse a 120-byte data record."""
    # Event info (offsets 0-10)
    sex_code = int(chr(data[0]))
    stroke_code = int(chr(data[1]))
    distance = int(data[2:6].decode("ascii", errors="replace").strip())

    # Age group: offsets 6-7 = min age, 8-9 = max age
    age_min_raw = data[6:8].decode("ascii", errors="replace").strip()
    age_max_raw = data[8:10].decode("ascii", errors="replace").strip()
    age_group_min = int(age_min_raw) if age_min_raw else None
    age_group_max = int(age_max_raw) if age_max_raw else None

    event_type_code = chr(data[10])
    is_relay = event_type_code == "R"
    event_type = "Individual" if event_type_code == "I" else "Relay" if is_relay else event_type_code

    # Record holder info (offsets 11-90, 80 chars)
    if is_relay:
        # Relay: offsets 11-40 = team name, offsets 41-90 = relay member names
        swimmer_name = data[11:41].decode("ascii", errors="replace").strip()
        team = swimmer_name  # for relays, the "name" field IS the team
        relay_names = data[41:91].decode("ascii", errors="replace").strip()
    else:
        # Individual: offsets 11-40 = swimmer name, offsets 41-56 = team (16 chars)
        swimmer_name = data[11:41].decode("ascii", errors="replace").strip()
        team = data[41:57].decode("ascii", errors="replace").strip()
        relay_names = None

    # Record date (offsets 91-96, MMDDYY)
    record_date = _parse_date(data[91:97].decode("ascii", errors="replace"))

    # Time as MBF single-precision float (offsets 97-100)
    mbf_bytes = data[97:101]
    time_seconds = _mbf_single_to_float(mbf_bytes)
    time_formatted = _format_time(time_seconds)

    # Record team (offsets 103-107, 5 chars)
    record_team = data[103:108].decode("ascii", errors="replace").strip()

    # Entry type sentinel (offsets 108-110, e.g. "A20")
    entry_type = data[108:111].decode("ascii", errors="replace").strip()

    return SwimRecord(
        sex=SEXES.get(sex_code, f"Unknown ({sex_code})"),
        sex_code=sex_code,
        stroke=STROKES.get(stroke_code, f"Unknown ({stroke_code})"),
        stroke_code=stroke_code,
        distance=distance,
        age_group_min=age_group_min,
        age_group_max=age_group_max,
        event_type=event_type,
        swimmer_name=swimmer_name,
        team=team,
        relay_names=relay_names,
        record_date=record_date,
        time_seconds=time_seconds,
        time_formatted=time_formatted,
        record_team=record_team,
        entry_type=entry_type,
    )


def parse_rec_file(filepath: str | Path) -> RecFile:
    """Parse a HyTek .rec file and return structured data.

    Args:
        filepath: Path to the .rec file.

    Returns:
        A RecFile containing the header and list of swim records.
    """
    filepath = Path(filepath)
    raw = filepath.read_bytes()

    if len(raw) < RECORD_LENGTH:
        raise ValueError(f"File too small ({len(raw)} bytes), expected at least {RECORD_LENGTH}")

    if len(raw) % RECORD_LENGTH != 0:
        raise ValueError(
            f"File size ({len(raw)} bytes) is not a multiple of record length ({RECORD_LENGTH})"
        )

    num_records = len(raw) // RECORD_LENGTH

    header = _parse_header(raw[0:RECORD_LENGTH])

    records = []
    for i in range(1, num_records):
        offset = i * RECORD_LENGTH
        record_data = raw[offset : offset + RECORD_LENGTH]
        records.append(_parse_record(record_data))

    return RecFile(header=header, records=records)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <file.rec>")
        sys.exit(1)

    rec = parse_rec_file(sys.argv[1])

    print(f"Record Set:  {rec.header.record_set_name}")
    print(f"Course:      {rec.header.course}")
    print(f"Software:    {rec.header.software_version}")
    print(f"Export Date: {format_record_date(rec.header.export_date) or 'N/A'}")
    print(f"Records:     {len(rec.records)}")
    print()

    for r in rec.records:
        if r.age_group_min and r.age_group_max:
            age = f"{r.age_group_min}-{r.age_group_max}"
        elif r.age_group_max:
            age = f"{r.age_group_max} & Under"
        elif r.age_group_min:
            age = f"{r.age_group_min} & Over"
        else:
            age = "Open"
        print(f"  {r.sex} {age} {r.distance} {r.stroke} ({r.event_type})")

        if r.event_type == "Relay":
            print(f"    Team: {r.team}")
            if r.relay_names:
                print(f"    Members: {r.relay_names}")
        else:
            name = r.swimmer_name or "(vacant)"
            print(f"    {name} ({r.team})" if r.team else f"    {name}")

        parts = [r.time_formatted]
        date_str = format_record_date(r.record_date)
        if date_str:
            parts.append(f"Date: {date_str}")
        if r.record_team:
            parts.append(f"Team: {r.record_team}")
        print(f"    {' | '.join(parts)}")
        print()
