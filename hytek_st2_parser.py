"""
Parser for HyTek Meet Manager time standards files (.st2).

Usage as library:
    from hytek_st2_parser import parse_st2_file

    st2 = parse_st2_file("MixedTimeStandards.st2")
    for event in st2.events:
        for course in event.courses:
            for qt in course.times:
                print(f"{qt.standard.tag}: {qt.time_formatted}")
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from hytek_rec_parser import (
    SEXES,
    STROKES,
    _format_time,
    _mbf_single_to_float,
    _parse_date,
    format_record_date,
)

ST2_RECORD_LENGTH = 320
ST2_MAX_STANDARDS = 12
ST2_TAG_OFFSET = 20
ST2_TAG_SIZE = 4
ST2_DESC_OFFSET = 68
ST2_DESC_SIZE = 20
ST2_TRAILER_SIZE = 5

# Course time block offsets within data records
ST2_COURSE_OFFSETS = {
    "SCY": 260,
    "SCM": 164,
    "LCM": 212,
}


@dataclass
class TimeStandard:
    tag: str
    description: str


@dataclass
class St2Header:
    record_count: int | None
    export_date: date
    standards: list[TimeStandard]


@dataclass
class QualifyingTime:
    standard: TimeStandard
    time_seconds: float
    time_formatted: str


@dataclass
class CourseStandards:
    course: str
    times: list[QualifyingTime]


@dataclass
class St2Event:
    event_number: int
    sex: str
    sex_code: int
    stroke: str
    stroke_code: int
    distance: int
    age_group_min: int | None
    age_group_max: int | None
    event_type: str
    courses: list[CourseStandards]


@dataclass
class St2File:
    header: St2Header
    events: list[St2Event]


def _parse_st2_header(data: bytes) -> St2Header:
    """Parse a 320-byte .st2 header record."""
    identifier = data[0:3].decode("ascii", errors="replace").strip()
    if identifier != "STD":
        raise ValueError(f"Expected header identifier 'STD', got '{identifier}'")

    count_raw = data[6:7].decode("ascii", errors="replace").strip()
    record_count = int(count_raw) if count_raw.isdigit() else None

    export_date = _parse_date(data[7:13].decode("ascii", errors="replace"))

    # Parse standard tags: up to 12 × 4-char slots starting at offset 20
    standards: list[TimeStandard] = []
    for i in range(ST2_MAX_STANDARDS):
        tag_start = ST2_TAG_OFFSET + i * ST2_TAG_SIZE
        tag = data[tag_start : tag_start + ST2_TAG_SIZE].decode("ascii", errors="replace").strip()
        if not tag:
            break

        desc_start = ST2_DESC_OFFSET + i * ST2_DESC_SIZE
        desc = data[desc_start : desc_start + ST2_DESC_SIZE].decode("ascii", errors="replace").strip()

        standards.append(TimeStandard(tag=tag, description=desc))

    return St2Header(
        record_count=record_count,
        export_date=export_date,
        standards=standards,
    )


def _parse_st2_event(
    data: bytes, standards: list[TimeStandard], event_number: int
) -> St2Event:
    """Parse a 320-byte .st2 data record."""
    # Event descriptor (offsets 0-10, same layout as .rec)
    sex_code = int(chr(data[0]))
    stroke_code = int(chr(data[1]))
    distance = int(data[2:6].decode("ascii", errors="replace").strip())

    age_min_raw = data[6:8].decode("ascii", errors="replace").strip()
    age_max_raw = data[8:10].decode("ascii", errors="replace").strip()
    age_group_min = int(age_min_raw) if age_min_raw else None
    age_group_max = int(age_max_raw) if age_max_raw else None

    event_type_code = chr(data[10])
    event_type = (
        "Individual" if event_type_code == "I" else "Relay" if event_type_code == "R" else event_type_code
    )

    # Parse times for each course
    courses: list[CourseStandards] = []
    for course_name, block_offset in ST2_COURSE_OFFSETS.items():
        times: list[QualifyingTime] = []
        for i, std in enumerate(standards):
            mbf_offset = block_offset + i * 4
            mbf_bytes = data[mbf_offset : mbf_offset + 4]
            time_seconds = _mbf_single_to_float(mbf_bytes)
            if time_seconds > 0.0:
                times.append(
                    QualifyingTime(
                        standard=std,
                        time_seconds=time_seconds,
                        time_formatted=_format_time(time_seconds),
                    )
                )
        if times:
            courses.append(CourseStandards(course=course_name, times=times))

    return St2Event(
        event_number=event_number,
        sex=SEXES.get(sex_code, f"Unknown ({sex_code})"),
        sex_code=sex_code,
        stroke=STROKES.get(stroke_code, f"Unknown ({stroke_code})"),
        stroke_code=stroke_code,
        distance=distance,
        age_group_min=age_group_min,
        age_group_max=age_group_max,
        event_type=event_type,
        courses=courses,
    )


def parse_st2_file(filepath: str | Path) -> St2File:
    """Parse a HyTek .st2 file and return structured data.

    Args:
        filepath: Path to the .st2 file.

    Returns:
        An St2File containing the header and list of events with qualifying times.
    """
    filepath = Path(filepath)
    raw = filepath.read_bytes()

    # Strip trailing DOS EOF marker (0x1A) and preceding bytes if present
    if len(raw) >= ST2_TRAILER_SIZE and raw[-1] == 0x1A:
        raw = raw[:-ST2_TRAILER_SIZE]

    if len(raw) < ST2_RECORD_LENGTH:
        raise ValueError(
            f"File too small ({len(raw)} bytes), expected at least {ST2_RECORD_LENGTH}"
        )

    if len(raw) % ST2_RECORD_LENGTH != 0:
        raise ValueError(
            f"File size ({len(raw)} bytes after trailer removal) is not a multiple "
            f"of record length ({ST2_RECORD_LENGTH})"
        )

    num_records = len(raw) // ST2_RECORD_LENGTH

    header = _parse_st2_header(raw[0:ST2_RECORD_LENGTH])

    events: list[St2Event] = []
    for i in range(1, num_records):
        offset = i * ST2_RECORD_LENGTH
        record_data = raw[offset : offset + ST2_RECORD_LENGTH]
        events.append(_parse_st2_event(record_data, header.standards, event_number=i))

    return St2File(header=header, events=events)


def _format_age_group(
    age_min: int | None, age_max: int | None
) -> str:
    """Format an age group for display."""
    if age_min and age_max:
        return f"{age_min}-{age_max}"
    elif age_max:
        return f"{age_max} & Under"
    elif age_min:
        return f"{age_min} & Over"
    else:
        return "Open"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <file.st2>")
        sys.exit(1)

    st2 = parse_st2_file(sys.argv[1])

    std_tags = ", ".join(s.tag for s in st2.header.standards)
    print(f"Standards:   {std_tags}")
    export_str = format_record_date(st2.header.export_date) or "N/A"
    print(f"Export Date: {export_str}")
    print(f"Events:      {len(st2.events)}")
    print()

    for event in st2.events:
        age = _format_age_group(event.age_group_min, event.age_group_max)
        print(
            f"  Event {event.event_number}: {event.sex} {age} "
            f"{event.distance} {event.stroke} ({event.event_type})"
        )

        for cs in event.courses:
            parts = [f"{qt.standard.tag}  {qt.time_formatted}" for qt in cs.times]
            print(f"    {cs.course}:  {'  |  '.join(parts)}")

        if not event.courses:
            print("    (no times)")

        print()
