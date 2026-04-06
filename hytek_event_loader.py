import pickle
import copy
import tempfile
import os

from hytek_parser.hy3_parser import parse_hy3
from hytek_parser.hy3.enums import Stroke, Gender, GenderAge, Course

# Monkey-patch e2_parser to handle empty/invalid date fields in .hy3 files
import hytek_parser.hy3.line_parsers.e_event_parsers as _e_parsers
from datetime import datetime as _datetime

_original_e2_parser = _e_parsers.e2_parser

def _patched_e2_parser(line, file, opts):
    try:
        return _original_e2_parser(line, file, opts)
    except (ValueError, IndexError):
        # Handle lines with missing/invalid date fields by injecting a valid placeholder
        # extract() uses 1-based indexing: extract(line, 88, 8) reads line[87:95]
        padded = line.ljust(96)
        patched = padded[:87] + "01011900" + padded[95:]
        result = _original_e2_parser(patched, file, opts)
        # Clear the placeholder date on the entry
        event_num, event = result.meet.last_event
        entry = event.last_entry
        placeholder = _datetime(1900, 1, 1).date()
        for prefix in ("prelim", "swimoff", "finals"):
            if getattr(entry, f"{prefix}_date", None) == placeholder:
                setattr(entry, f"{prefix}_date", None)
        return result

_e_parsers.e2_parser = _patched_e2_parser
from hytek_parser.hy3 import HY3_LINE_PARSERS
HY3_LINE_PARSERS["E2"] = _patched_e2_parser


STROKE_NAMES = {
    Stroke.FREESTYLE: "Freestyle",
    Stroke.BACKSTROKE: "Backstroke",
    Stroke.BREASTSTROKE: "Breaststroke",
    Stroke.BUTTERFLY: "Butterfly",
    Stroke.MEDLEY: "Medley",
    Stroke.UNKNOWN: "",
}

COURSE_NAMES = {
    Course.SCY: "Yard",
    Course.SCM: "Meter",
    Course.LCM: "Meter",
    Course.UNKNOWN: "",
}

GENDER_AGE_NAMES = {
    GenderAge.GIRL_S: "Girls",
    GenderAge.BOY_S: "Boys",
    GenderAge.WOMEN_S: "Women",
    GenderAge.MEN_S: "Men",
    GenderAge.UNKNOWN: "",
}


def _build_event_name(event):
    gender = GENDER_AGE_NAMES.get(event.gender_age, "")
    if event.age_min and event.age_max:
        age = "%d-%d" % (event.age_min, event.age_max)
    elif event.age_min:
        age = "%d & Over" % event.age_min
    elif event.age_max:
        age = "%d & Under" % event.age_max
    else:
        age = "Open"

    distance = str(event.distance)
    course = COURSE_NAMES.get(event.course, "")
    stroke = STROKE_NAMES.get(event.stroke, "")
    relay = "Relay" if event.relay else ""

    parts = [p for p in [gender, age, distance, course, stroke, relay] if p]
    return " ".join(parts)


def _build_display_string(entry):
    if entry.relay:
        return ""
    elif entry.swimmers:
        swimmer = entry.swimmers[0]
        return "%s %s" % (swimmer.first_name, swimmer.last_name)
    return ""


def _get_team_code(entry):
    if entry.swimmers:
        return entry.swimmers[0].team_code
    return ""


class HytekEventLoader():
    max_display_string_length = 0

    def __init__(self, file_name=None):
        self.event_names = {}
        self.events = {}
        self.teams = {}
        self.events_uncombined = {}
        self.teams_uncombined = {}
        self.combined = {}
        if file_name:
            self.load(file_name)

    def clear(self):
        self.event_names.clear()
        self.events.clear()
        self.teams.clear()
        self.events_uncombined = copy.deepcopy(self.events)
        self.teams_uncombined = copy.deepcopy(self.teams)
        self.combined.clear()
        self.max_display_string_length = 0

    def load(self, file_name):
        self.clear()
        parsed = parse_hy3(file_name)
        self._load_from_parsed(parsed)

    def load_from_bytestream(self, stream):
        self.clear()
        with tempfile.NamedTemporaryFile(suffix='.hy3', delete=False) as tmp:
            tmp.write(stream.read())
            tmp_path = tmp.name
        try:
            parsed = parse_hy3(tmp_path)
            self._load_from_parsed(parsed)
        finally:
            os.unlink(tmp_path)

    def _load_from_parsed(self, parsed):
        meet = parsed.meet
        for event_num_str, event in meet.events.items():
            try:
                event_number = int(event_num_str)
            except ValueError:
                event_number = event_num_str

            self.event_names[event_number] = _build_event_name(event)

            for entry in event.entries:
                heat = entry.finals_heat
                lane = entry.finals_lane

                if heat is None or lane is None:
                    continue

                display_string = _build_display_string(entry)
                team_code = _get_team_code(entry)
                self.max_display_string_length = max(
                    self.max_display_string_length, len(display_string))

                if (event_number, heat) not in self.events:
                    self.events[(event_number, heat)] = {}
                    self.teams[(event_number, heat)] = {}

                self.events[(event_number, heat)][lane] = display_string
                self.teams[(event_number, heat)][lane] = team_code

        self.events_uncombined = copy.deepcopy(self.events)
        self.teams_uncombined = copy.deepcopy(self.teams)

    def combine_events(self, combined=None):
        if combined is not None:
            self.combined = combined
        self.events = copy.deepcopy(self.events_uncombined)
        self.teams = copy.deepcopy(self.teams_uncombined)

        for combine_source, combine_destination in self.combined.items():
            if (combine_source != combine_destination):
                for lane in self.events[combine_source]:
                    self.events[combine_destination][lane] = self.events[combine_source][lane] + '*'
                    if combine_source in self.teams and lane in self.teams[combine_source]:
                        self.teams[combine_destination][lane] = self.teams[combine_source][lane]
                del self.events[combine_source]
                self.events[combine_source] = self.events[combine_destination]
                if combine_source in self.teams:
                    del self.teams[combine_source]
                    self.teams[combine_source] = self.teams[combine_destination]

    def get_event_name(self, event_number):
        try:
            return self.event_names[event_number]
        except Exception:
            return ""

    def get_display_string(self, event_number, heat_number, lane):
        try:
            return self.events[(event_number, heat_number)][lane]
        except Exception:
            pass
        return ""

    def get_team_code(self, event_number, heat_number, lane):
        try:
            return self.teams[(event_number, heat_number)][lane]
        except Exception:
            pass
        return ""

    def get_display_string_uncombined(self, event_number, heat_number, lane):
        try:
            return self.events_uncombined[(event_number, heat_number)][lane]
        except Exception:
            pass
        return ""

    def to_object(self):
        return pickle.dumps({
            "event_names": self.event_names,
            "events": self.events,
            "events_uncombined": self.events_uncombined,
            "teams": self.teams,
            "teams_uncombined": self.teams_uncombined,
            "combined": self.combined,
        }, protocol=0).decode('utf8')

    def from_object(self, p):
        o = pickle.loads(p.encode('utf8'))
        self.event_names = o['event_names']
        self.events = o['events']
        self.events_uncombined = o['events_uncombined']
        self.teams = o.get('teams', {})
        self.teams_uncombined = o.get('teams_uncombined', {})
        self.combined = o['combined']


if __name__ == "__main__":
    import sys
    event_info = HytekEventLoader(sys.argv[1])

    event_heat = list(event_info.events.keys())
    event_heat.sort()
    for event, heat in event_heat:
        print("Event", event, " Heat", heat, end=" ")
        if event in event_info.event_names:
            print(event_info.event_names[event])
        else:
            print("")

        lanes = list(event_info.events[(event, heat)].keys())
        lanes.sort()
        for lane in lanes:
            print("\t", lane, "\t", event_info.events[(event, heat)][lane])
