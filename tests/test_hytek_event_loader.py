import io
from types import SimpleNamespace

import hytek_parser.hy3.line_parsers.f_relay_parsers as f_relay_parsers
import pytest
from hytek_parser.hy3 import HY3_LINE_PARSERS
from hytek_parser.hy3.enums import Course, GenderAge, Stroke
from hytek_parser.hy3_parser import parse_hy3

import hytek_event_loader
from hytek_event_loader import HytekEventLoader


def _copy_with_invalid_result_date(source, destination, record_code, date_offset):
    lines = source.read_text().splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith(record_code):
            content = line.rstrip("\r\n").ljust(date_offset + 8)
            newline = line[len(line.rstrip("\r\n")) :]
            lines[index] = (
                content[:date_offset] + "99999999" + content[date_offset + 8 :] + newline
            )
            destination.write_text("".join(lines))
            return
    raise AssertionError(f"No {record_code} record found in {source}")


class TestHytekEventLoaderInit:
    def test_empty_init(self):
        loader = HytekEventLoader()
        assert len(loader.event_names) == 0
        assert loader.has_names is False

    def test_load_from_constructor(self, samples_dir):
        loader = HytekEventLoader(str(samples_dir / "DemoMeet-MixedEvent.hy3"))
        assert len(loader.event_names) > 0


class TestLoad:
    def test_load_populates_events(self, samples_dir):
        loader = HytekEventLoader()
        loader.load(str(samples_dir / "DemoMeet-MixedEvent.hy3"))
        assert len(loader.event_names) > 0
        assert len(loader.events) > 0

    def test_load_populates_event_meta(self, samples_dir):
        loader = HytekEventLoader(str(samples_dir / "DemoMeet-MixedEvent.hy3"))
        for event_num, meta in loader.event_meta.items():
            assert "stroke_code" in meta
            assert "distance" in meta
            assert "relay" in meta
            assert isinstance(meta["distance"], int)

    def test_individual_medley_and_open_ended_age_names(self):
        event = SimpleNamespace(
            gender_age=GenderAge.GIRL_S,
            entries=[],
            age_min=15,
            age_max=109,
            distance=100,
            course=Course.SCY,
            stroke=Stroke.MEDLEY,
            relay=False,
        )
        assert hytek_event_loader._build_event_name(event) == (
            "Girls 15 & Over 100 Yard Individual Medley"
        )

        loader = HytekEventLoader()
        loader.event_meta[25] = {
            "stroke_code": 5,
            "distance": 100,
            "relay": False,
            "age_min": 15,
            "age_max": 109,
            "sex_codes": [2],
            "gender_age": GenderAge.GIRL_S,
        }
        assert loader.get_event_dims(25)["age_group_label"] == "15 & Over"

        event.relay = True
        event.age_min = 0
        event.age_max = 8
        assert hytek_event_loader._build_event_name(event) == (
            "Girls 8 & Under 100 Yard Medley Relay"
        )

    def test_relay_name_includes_id_and_truncated_roster(self):
        swimmers = [
            SimpleNamespace(
                first_name="Madeline", last_name="Lindberg", team_code="HW"
            ),
            SimpleNamespace(first_name="Lucy", last_name="Miller", team_code="HW"),
            SimpleNamespace(
                first_name="Alexandria", last_name="Robertson", team_code="HW"
            ),
            SimpleNamespace(first_name="Sophie", last_name="Johnson", team_code="HW"),
        ]
        entry = SimpleNamespace(
            relay=True,
            relay_team_id="B",
            relay_swim_team_code="HW",
            swimmers=swimmers,
        )

        assert hytek_event_loader._build_display_string(entry) == (
            "Relay B: Madeline L., Lucy M., ..."
        )
        assert hytek_event_loader._get_team_code(entry) == "HW"

    def test_load_from_bytestream_matches_file_load(self, samples_dir):
        source = samples_dir / "DemoMeet-MixedEvent.hy3"
        from_file = HytekEventLoader(str(source))
        from_stream = HytekEventLoader()

        from_stream.load_from_bytestream(io.BytesIO(source.read_bytes()))

        assert from_stream.to_object() == from_file.to_object()

    def test_all_hy3_samples_parse(self, samples_dir):
        for path in samples_dir.glob("*.hy3"):
            loader = HytekEventLoader(str(path))
            assert loader.event_names


class TestHytekParserCompatibility:
    def test_upstream_f1_parser_retains_relay_id_without_patch(self, samples_dir):
        assert HY3_LINE_PARSERS["F1"] is f_relay_parsers.f1_parser
        loader = HytekEventLoader(
            str(samples_dir / "Meet Entries-SW @ HW - A - 6-18-26-18Jun2026-001.hy3")
        )

        relay_names = [
            name
            for lanes in loader.events.values()
            for name in lanes.values()
            if name.startswith("Relay ")
        ]
        assert any(name.startswith("Relay A:") for name in relay_names)
        assert any(name.startswith("Relay B:") for name in relay_names)

    @pytest.mark.parametrize(
        ("record_code", "source_name", "original_parser"),
        [
            (
                "E2",
                "DemoMeet-MixedEvent.hy3",
                hytek_event_loader._original_e2_parser,
            ),
            (
                "F2",
                "Meet Entries-SW @ HW - A - 6-18-26-18Jun2026-001.hy3",
                hytek_event_loader._original_f2_parser,
            ),
        ],
    )
    def test_upstream_parser_handles_blank_result_dates_without_patch(
        self, samples_dir, monkeypatch, record_code, source_name, original_parser
    ):
        monkeypatch.setitem(HY3_LINE_PARSERS, record_code, original_parser)
        parsed = parse_hy3(samples_dir / source_name)
        assert parsed.meet.events

    @pytest.mark.parametrize(
        ("record_code", "date_offset", "source_name", "original_parser", "patched_parser"),
        [
            (
                "E2",
                87,
                "DemoMeet-MixedEvent.hy3",
                hytek_event_loader._original_e2_parser,
                hytek_event_loader._patched_e2_parser,
            ),
            (
                "F2",
                102,
                "Meet Entries-SW @ HW - A - 6-18-26-18Jun2026-001.hy3",
                hytek_event_loader._original_f2_parser,
                hytek_event_loader._patched_f2_parser,
            ),
        ],
    )
    def test_invalid_result_date_still_requires_patch(
        self,
        samples_dir,
        tmp_path,
        monkeypatch,
        record_code,
        date_offset,
        source_name,
        original_parser,
        patched_parser,
    ):
        malformed = tmp_path / source_name
        _copy_with_invalid_result_date(
            samples_dir / source_name, malformed, record_code, date_offset
        )

        monkeypatch.setitem(HY3_LINE_PARSERS, record_code, original_parser)
        with pytest.raises(RuntimeError) as error:
            parse_hy3(malformed)
        assert isinstance(error.value.__cause__, ValueError)

        monkeypatch.setitem(HY3_LINE_PARSERS, record_code, patched_parser)
        parsed = parse_hy3(malformed)
        dates = [
            getattr(entry, f"{prefix}_date")
            for event in parsed.meet.events.values()
            for entry in event.entries
            for prefix in ("prelim", "swimoff", "finals")
        ]
        assert all(value is None or value.year != 1900 for value in dates)


class TestGetters:
    @pytest.fixture
    def loaded(self, samples_dir):
        return HytekEventLoader(str(samples_dir / "DemoMeet-MixedEvent.hy3"))

    def test_get_event_name(self, loaded):
        first_event = next(iter(loaded.event_names))
        name = loaded.get_event_name(first_event)
        assert name != ""

    def test_get_event_name_missing(self):
        loader = HytekEventLoader()
        assert loader.get_event_name(9999) == ""

    def test_get_display_string(self, loaded):
        if loaded.has_names:
            key = next(iter(loaded.events))
            lane = next(iter(loaded.events[key]))
            name = loaded.get_display_string(key[0], key[1], lane)
            assert isinstance(name, str)

    def test_get_display_string_missing(self):
        loader = HytekEventLoader()
        assert loader.get_display_string(1, 1, 1) == ""

    def test_get_team_code(self, loaded):
        if loaded.teams:
            key = next(iter(loaded.teams))
            lane = next(iter(loaded.teams[key]))
            code = loaded.get_team_code(key[0], key[1], lane)
            assert isinstance(code, str)

    def test_get_team_code_missing(self):
        loader = HytekEventLoader()
        assert loader.get_team_code(1, 1, 1) == ""

    def test_get_seed_time(self, loaded):
        if loaded.seed_times:
            key = next(iter(loaded.seed_times))
            lane = next(iter(loaded.seed_times[key]))
            t = loaded.get_seed_time(key[0], key[1], lane)
            assert t is None or isinstance(t, float)

    def test_get_seed_time_missing(self):
        loader = HytekEventLoader()
        assert loader.get_seed_time(1, 1, 1) is None

    def test_get_age_code(self, loaded):
        if loaded.age_codes:
            key = next(iter(loaded.age_codes))
            lane = next(iter(loaded.age_codes[key]))
            code = loaded.get_age_code(key[0], key[1], lane)
            assert isinstance(code, str)

    def test_get_age_code_missing(self):
        loader = HytekEventLoader()
        assert loader.get_age_code(1, 1, 1) == ""


class TestClear:
    def test_clear_resets_data(self, samples_dir):
        loader = HytekEventLoader(str(samples_dir / "DemoMeet-MixedEvent.hy3"))
        assert len(loader.event_names) > 0
        loader.clear()
        assert len(loader.event_names) == 0
        assert loader.has_names is False
        assert loader.max_display_string_length == 0


class TestCombineEvents:
    def test_combines_names_and_lane_metadata(self, samples_dir):
        loader = HytekEventLoader(str(samples_dir / "DemoMeet-MixedEvent.hy3"))
        source, destination = list(loader.events)[:2]
        source_lane = next(iter(loader.events[source]))
        source_name = loader.events[source][source_lane]
        source_team = loader.teams[source][source_lane]
        source_age = loader.age_codes[source][source_lane]
        source_seed = loader.seed_times[source][source_lane]

        loader.combine_events({source: destination})

        assert loader.events[destination][source_lane] == source_name + "*"
        assert loader.events[source] == loader.events[destination]
        assert loader.teams[destination][source_lane] == source_team
        assert loader.age_codes[destination][source_lane] == source_age
        assert loader.seed_times[destination][source_lane] == source_seed
        assert (
            loader.get_display_string_uncombined(*source, source_lane) == source_name
        )

    def test_reapplies_stored_combination(self, samples_dir):
        loader = HytekEventLoader(str(samples_dir / "DemoMeet-MixedEvent.hy3"))
        source, destination = list(loader.events)[:2]
        loader.combine_events({source: destination})
        first_result = loader.events

        loader.combine_events()

        assert loader.events == first_result


class TestSerialization:
    def test_round_trip(self, samples_dir):
        loader = HytekEventLoader(str(samples_dir / "DemoMeet-MixedEvent.hy3"))
        serialized = loader.to_object()
        assert isinstance(serialized, str)

        loader2 = HytekEventLoader()
        loader2.from_object(serialized)
        assert loader2.event_names == loader.event_names

    def test_round_trip_preserves_events(self, samples_dir):
        loader = HytekEventLoader(str(samples_dir / "DemoMeet-MixedEvent.hy3"))
        serialized = loader.to_object()

        loader2 = HytekEventLoader()
        loader2.from_object(serialized)
        assert loader2.events == loader.events
        assert loader2.teams == loader.teams
