# HyTek Meet Manager .rec File Format

This document describes the binary file format used by HyTek Meet Manager for swim records files (`.rec`). The format was reverse-engineered from sample files exported by Meet Manager.

## Overview

A `.rec` file is a flat binary file composed of fixed-length **120-byte records** with no line separators or delimiters. The first record is always a header; all subsequent records are data records, one per swim record entry.

```
[ Header (120 bytes) ][ Record 1 (120 bytes) ][ Record 2 (120 bytes) ] ...
```

Total file size is always a multiple of 120.

## Header Record (120 bytes)

| Offset | Length | Type  | Description                     | Example         |
|--------|--------|-------|---------------------------------|-----------------|
| 0–2    | 3      | ASCII | File type identifier, always `REC` | `REC`        |
| 3–5    | 3      | ASCII | Padding (spaces)                |                 |
| 6      | 1      | ASCII | Number of records in file (including header) | `7` |
| 7–12   | 6      | ASCII | Export date in MMDDYY format    | `040526`        |
| 13     | 1      | ASCII | Space                           |                 |
| 14     | 1      | ASCII | Course code                     | `Y`             |
| 15–29  | 15     | ASCII | Record set name (space-padded)  | `B Champs`      |
| 30–44  | 15     | ASCII | Software version (space-padded) | `20WIN-MM038.0` |
| 45–119 | 75     | ASCII | Padding (spaces)                |                 |

### Course Codes

| Code | Meaning              |
|------|----------------------|
| `Y`  | Short Course Yards   |
| `S`  | Short Course Meters  |
| `L`  | Long Course Meters   |

## Data Record (120 bytes)

Each data record contains event identification, record holder information, and the record time.

### Event Descriptor (offsets 0–10)

| Offset | Length | Type  | Description          | Example |
|--------|--------|-------|----------------------|---------|
| 0      | 1      | ASCII | Sex code             | `1`     |
| 1      | 1      | ASCII | Stroke code          | `1`     |
| 2–5    | 4      | ASCII | Distance (left-justified, space-padded) | `100 ` or `50  ` |
| 6–7    | 2      | ASCII | Age group minimum (space-padded if N/A) | `11` or `  ` |
| 8–9    | 2      | ASCII | Age group maximum (space-padded if N/A) | `12` or `08` |
| 10     | 1      | ASCII | Event type           | `I`     |

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

### Record Holder Info (offsets 11–90, 80 characters)

The layout of this section depends on the event type.

#### Individual Records

| Offset | Length | Type  | Description                    | Example               |
|--------|--------|-------|--------------------------------|-----------------------|
| 11–40  | 30     | ASCII | Swimmer name (space-padded)    | `Betz, Ben`           |
| 41–56  | 16     | ASCII | Team abbreviation (space-padded) | `DCST`              |
| 57–90  | 34     | ASCII | Unused (spaces)                |                       |

#### Relay Records

| Offset | Length | Type  | Description                    | Example                                          |
|--------|--------|-------|--------------------------------|--------------------------------------------------|
| 11–40  | 30     | ASCII | Team name (space-padded)       | `High Woodlands Dolphins`                        |
| 41–90  | 50     | ASCII | Relay member names, comma-separated (space-padded) | `K. Samson, B. Betz, C. Shackter, J. O' Shea` |

### Record Metadata (offsets 91–119)

| Offset  | Length | Type   | Description                          | Example          |
|---------|--------|--------|--------------------------------------|------------------|
| 91–96   | 6      | ASCII  | Record date (see Date Encoding below) | `071424`        |
| 97–100  | 4      | Binary | Record time as MBF single-precision float (seconds) | See below |
| 101–102 | 2      | ASCII  | Padding (spaces)                     |                  |
| 103–107 | 5      | ASCII  | Record-setting team abbreviation (space-padded) | `DCST ` |
| 108–110 | 3      | ASCII  | Entry type sentinel                  | `A20`            |
| 111–119 | 9      | ASCII  | Padding (spaces)                     |                  |

### Date Encoding (offsets 91–96)

The 6-byte date field uses MMDDYY format but may be partially populated:

| Raw Value | Interpretation         |
|-----------|------------------------|
| `071424`  | Full date: July 14, 2024 |
| `06  82`  | Month + year only: June 1982 (day blank) |
| `    19`  | Year only: 2019        |
| `010126`  | Could be full date (Jan 1, 2026) or a placeholder for year-only (2026) — ambiguous for Jan 1 dates |
| `      `  | No date recorded       |

Two-digit years should be interpreted using a sliding window: 00–68 → 2000–2068, 69–99 → 1969–1999.

### Time Encoding (offsets 97–100)

Times are stored as **Microsoft Binary Format (MBF) single-precision floats** representing the time in **seconds**.

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

| Raw Bytes (hex)  | Value (seconds) | Formatted Time |
|------------------|-----------------|----------------|
| `A4 70 1E 87`    | 79.22           | 1:19.22        |
| `B8 1E 2F 85`    | 21.89           | 21.89          |
| `29 5C 5A 87`    | 109.18          | 1:49.18        |
| `00 00 00 00`    | 0.0             | (no time)      |

**Note:** MBF is not the same as IEEE 754. The exponent bias is 128 (vs. 127 for IEEE), and the mantissa has an implied `0.1` binary prefix rather than IEEE's `1.m` format.

## Vacant Records

A data record may exist with event information and a time but no swimmer name or team. This represents an event slot with a record standard or cut time but no current record holder. The swimmer name and team fields will be all spaces.

## Entry Type Sentinel (offsets 108–110)

This 3-character field appears to be a format or version marker following the HyTek convention of 1–2 letters followed by 1–2 digits. Observed values include `A20` and `A19`. The exact meaning is unknown, but it is consistent across all records within a file and does **not** represent a year.

## Open Questions

1. **Entry type sentinel (`A20`, `A19`)**: What do these values mean? They appear to be format versioning or record type markers, but the exact semantics are unknown. Do they vary by Meet Manager version, record set type, or something else?

2. **Record count field (header byte 6)**: Observed as a single ASCII digit matching the total number of 120-byte blocks in the file (including header). What happens when there are more than 9 records? Does it become multi-digit, wrap, or use a different encoding?

3. **Header bytes 3–5**: Always observed as spaces. Could these carry data in other files?

4. **Unused individual record area (offsets 57–90)**: Always spaces in all observed files. Could this area be populated under certain conditions (e.g., meet name, location, or other metadata)?

5. **Stroke codes 6 and 7**: The SDIF standard defines codes 6 (Freestyle Relay) and 7 (Medley Relay) as stroke values. In observed files, relays use the individual stroke code (e.g., `1` for Freestyle) combined with the `R` event type at offset 10. Are stroke codes 6 and 7 ever used in `.rec` files, or is the relay distinction handled entirely by the event type field?

6. **Date ambiguity for January 1**: A date of `010126` (January 1, 2026) is indistinguishable from a year-only placeholder of 2026 if Meet Manager defaults blank month/day to `01`. Is `0101` a known default, or can genuine January 1 records occur?

7. **Record-setting team truncation**: The record team field is only 5 characters (offsets 103–107), which truncates longer team names (e.g., "Kishwaukee YMCA" → "Kishw", "Chicago Fire Swimming" → "Chica"). Is this the team abbreviation from Meet Manager's team setup, or is it always a truncation of the full name?

8. **MBF precision**: Times are stored as MBF single-precision (4 bytes / ~7 significant digits). This is sufficient for swim times but introduces minor floating-point representation differences (e.g., a time entered as 22.00 may decode as 21.89). Could HyTek be using MBF double-precision (8 bytes) in some cases, potentially extending into the 2 ASCII bytes at offsets 95–96?
