import pytest

from hytek_event_loader import HytekEventLoader


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
