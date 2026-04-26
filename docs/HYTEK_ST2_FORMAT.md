# HyTek Meet Manager .st2 File Format

This document describes the binary file format used by HyTek Meet Manager for time standards files (`.st2`). The format was reverse-engineered from sample files exported by Meet Manager.

## Overview

An `.st2` file is a flat binary file composed of fixed-length **320-byte records** followed by a **5-byte trailer**. The first record is always a header; all subsequent records are data records, one per event. A single file can contain qualifying times for multiple courses (SCY, SCM, LCM) simultaneously.

```
[ Header (320 bytes) ][ Event 1 (320 bytes) ][ Event 2 (320 bytes) ] ... [ Trailer (5 bytes) ]
```

Total file size is always `(N * 320) + 5`, where N is the number of records (header + events).

## Header Record (320 bytes)

| Offset | Length | Type  | Description                                          | Example    |
|--------|--------|-------|------------------------------------------------------|------------|
| 0–2    | 3      | ASCII | File type identifier, always `STD`                   | `STD`      |
| 3–5    | 3      | ASCII | Padding (spaces)                                     |            |
| 6      | 1      | ASCII | Number of records in file (including header)          | `7`        |
| 7–12   | 6      | ASCII | Export date in MMDDYY format                         | `040526`   |
| 13–19  | 7      | ASCII | Padding (spaces)                                     |            |
| 20–67  | 48     | ASCII | Standard tags (up to 12 × 4-char, space-padded)      | `A   BMIN` |
| 68–307 | 240    | ASCII | Standard descriptions (up to 12 × 20-char, space-padded) | `A                   BMIN                ` |
| 308–319 | 12    | ASCII | Padding (spaces)                                     |            |

### Standard Definitions

The header defines which time standards are present in the file. Each standard has a short **tag** (up to 4 characters) and a longer **description** (up to 20 characters).

Tags are stored at offsets 20–67 as up to 12 consecutive 4-character slots:

```
Offset 20: Tag 0 (4 chars)
Offset 24: Tag 1 (4 chars)
Offset 28: Tag 2 (4 chars)
...
Offset 64: Tag 11 (4 chars)
```

Descriptions are stored at offsets 68–307 as up to 12 consecutive 20-character slots:

```
Offset  68: Description 0 (20 chars)
Offset  88: Description 1 (20 chars)
Offset 108: Description 2 (20 chars)
...
Offset 288: Description 11 (20 chars)
```

The number of defined standards is determined by scanning tag slots from index 0 until a blank (all-spaces) slot is encountered. Each standard at index `i` has its tag at offset `20 + i*4` and description at offset `68 + i*20`.

## Data Record (320 bytes)

Each data record represents one event and contains an event descriptor followed by qualifying times for each defined standard, organized by course.

### Record Layout

| Offset  | Length | Type   | Description                          |
|---------|--------|--------|--------------------------------------|
| 0–10    | 11     | ASCII  | Event descriptor (see below)         |
| 11–19   | 9      | ASCII  | Padding (spaces)                     |
| 20–67   | 48     | Binary | Unused (null bytes)                  |
| 68–115  | 48     | Binary | Unused (null bytes)                  |
| 116–163 | 48     | Binary | Unused (null bytes)                  |
| 164–211 | 48     | Binary | SCM qualifying times (see below)     |
| 212–259 | 48     | Binary | LCM qualifying times (see below)     |
| 260–307 | 48     | Binary | SCY qualifying times (see below)     |
| 308–319 | 12     | ASCII  | Padding (spaces)                     |

### Event Descriptor (offsets 0–10)

The event descriptor uses the same format as the `.rec` file format.

| Offset | Length | Type  | Description                                     | Example          |
|--------|--------|-------|-------------------------------------------------|------------------|
| 0      | 1      | ASCII | Sex code                                        | `2`              |
| 1      | 1      | ASCII | Stroke code                                     | `1`              |
| 2–5    | 4      | ASCII | Distance (left-justified, space-padded)          | `100 ` or `50  ` |
| 6–7    | 2      | ASCII | Age group minimum (space-padded if N/A)          | `11` or `  `     |
| 8–9    | 2      | ASCII | Age group maximum (space-padded if N/A)          | `12` or `08`     |
| 10     | 1      | ASCII | Event type                                      | `I`              |

### Sex Codes

| Code | Meaning |
|------|---------|
| `0`  | Mixed   |
| `1`  | Male    |
| `2`  | Female  |

### Stroke Codes

| Code | Meaning          |
|------|------------------|
| `1`  | Freestyle        |
| `2`  | Backstroke       |
| `3`  | Breaststroke     |
| `4`  | Butterfly        |
| `5`  | Individual Medley|
| `6`  | Freestyle Relay  |
| `7`  | Medley Relay     |

### Event Type

| Code | Meaning    |
|------|------------|
| `I`  | Individual |
| `R`  | Relay      |

### Age Group Encoding

The age group is encoded as two 2-character fields (min and max). The combination determines the age group type:

| Min   | Max   | Meaning               | Example        |
|-------|-------|-----------------------|----------------|
| blank | `08`  | 8 & Under             | `"  08"`       |
| `11`  | `12`  | 11-12 (age range)     | `"1112"`       |
| `18`  | `29`  | 18-29 (age range)     | `"1829"`       |
| `15`  | blank | 15 & Over             | `"15  "`       |
| blank | blank | Open / Senior         | `"    "`       |

### Qualifying Times

Times for each course are stored in a 48-byte block, with each standard's time occupying 4 bytes as an MBF single-precision float. The block contains up to 12 time slots (one per standard defined in the header).

#### Course Block Offsets

| Course | Block Offset | Description          |
|--------|-------------|----------------------|
| SCM    | 164         | Short Course Meters  |
| LCM    | 212         | Long Course Meters   |
| SCY    | 260         | Short Course Yards   |

For standard at index `i`, the time is located at:

```
SCM time: offset 164 + (i * 4)
LCM time: offset 212 + (i * 4)
SCY time: offset 260 + (i * 4)
```

A time of zero (4 null bytes) indicates no qualifying time is defined for that standard/course combination. For example, a 25-yard event would have SCY times but no LCM times, since LCM distances must be multiples of 50 meters.

### Time Encoding

Times are stored as **Microsoft Binary Format (MBF) single-precision floats** representing the time in **seconds**. This is the same encoding used in `.rec` files.

#### MBF Single Layout (4 bytes)

```
Byte 0: Mantissa LSB
Byte 1: Mantissa middle byte
Byte 2: Sign (bit 7) + Mantissa MSB (bits 6-0)
Byte 3: Exponent (biased by 128)
```

#### Conversion Formula

```
If exponent == 0:
    value = 0.0
Else:
    sign = (byte2 >> 7) & 1
    mantissa = 0x800000 | ((byte2 & 0x7F) << 16) | (byte1 << 8) | byte0
    value = (-1)^sign * (mantissa / 2^24) * 2^(exponent - 128)
```

#### Examples

| Raw Bytes (hex) | Value (seconds) | Formatted Time |
|-----------------|-----------------|----------------|
| `7B 14 1A 85`   | 19.26           | 19.26          |
| `B8 1E 31 85`   | 22.14           | 22.14          |
| `52 B8 08 86`   | 34.18           | 34.18          |
| `C3 F5 0C 86`   | 35.24           | 35.24          |
| `AE 47 6D 87`   | 1:28.46         | 1:28.46        |
| `00 00 00 00`   | 0.0             | (no time)      |

**Note:** MBF is not the same as IEEE 754. The exponent bias is 128 (vs. 127 for IEEE), and the mantissa has an implied `0.1` binary prefix rather than IEEE's `1.m` format.

## Qualifier Direction

Time standards can have a qualifier direction — "= or faster" (time must be at or below the standard) or "slower than" (time must be above the standard). This direction is **not stored** in the `.st2` file; it is configured separately at the meet level within Meet Manager.

## File Trailer (5 bytes)

Every `.st2` file ends with a 5-byte trailer after the last 320-byte record:

| Offset | Length | Type   | Description              | Observed Value |
|--------|--------|--------|--------------------------|----------------|
| 0–3    | 4      | Binary | Unknown                  | `08 00 01 00`  |
| 4      | 1      | Binary | DOS EOF marker           | `1A`           |

The final byte `0x1A` is the standard DOS end-of-file character (Ctrl+Z). The meaning of the preceding 4 bytes is unknown; they have been consistent across all observed files.

## Complete Example

Given a file with 2 standards (A, BMIN) and an event for Female 15 & Over 50 Backstroke with times in all three courses:

**Header** defines standards:
- Index 0: tag `A`, description `A`
- Index 1: tag `BMIN`, description `BMIN`

**Data record** (hex dump of time blocks):

```
Offset 164 (SCM):  C3 F5 0C 86  A4 70 5F 86  00 00 00 00 ...
                   ^^^^^^^^^^^  ^^^^^^^^^^^
                   A: 35.24     BMIN: 55.86

Offset 212 (LCM):  48 E1 43 86  5C 8F 66 86  00 00 00 00 ...
                   ^^^^^^^^^^^  ^^^^^^^^^^^
                   A: 48.97     BMIN: 57.64

Offset 260 (SCY):  B8 1E 31 85  52 B8 08 86  00 00 00 00 ...
                   ^^^^^^^^^^^  ^^^^^^^^^^^
                   A: 22.14     BMIN: 34.18
```

Each 4-byte MBF float at position `i` corresponds to the standard at index `i` defined in the header.

## Open Questions

1. **Record count field (header byte 6)**: Observed as a single ASCII digit matching the total number of 320-byte blocks in the file (including header). What happens when there are more than 9 events? Does it become multi-digit, wrap, or use a different encoding?

2. **Unused data record blocks (offsets 20–163)**: Three 48-byte blocks in each data record are always null bytes in observed files. These correspond to the tag and description areas in the header. Could these carry additional data under certain conditions (e.g., event-specific notes, alternate standard names)?

3. **Trailer bytes (`08 00 01 00`)**: The 4 bytes preceding the DOS EOF marker have unknown meaning. They are consistent across all observed files. Could represent a binary version number, checksum, or other metadata.

4. **Maximum standards**: The layout supports up to 12 standards per file (12 × 4-byte slots = 48 bytes per course block), but only 2 have been tested. Files with more standards would verify that the slot layout scales correctly.
