"""Tests for the per-event dimensional metadata used by viewer-engagement
telemetry on the Azure side. The Pi pushes ``current_event_dims`` so the
notebooks can slice viewing sessions by stroke / age group / gender / relay
without having to re-parse the meet."""
from __future__ import annotations

from hytek_event_loader import HytekEventLoader


class TestGetEventDims:
    def test_returns_none_for_unknown_event(self, samples_dir):
        loader = HytekEventLoader(str(samples_dir / "DemoMeet-MixedEvent.hy3"))
        assert loader.get_event_dims(99999) is None

    def test_shape_and_types_for_known_event(self, samples_dir):
        loader = HytekEventLoader(str(samples_dir / "DemoMeet-MixedEvent.hy3"))
        first = next(iter(loader.event_meta))
        dims = loader.get_event_dims(first)
        assert dims is not None
        # Required keys are always present, even when empty.
        for k in (
            "distance", "stroke_code", "stroke_name",
            "age_min", "age_max", "age_group_label",
            "gender_agnostic", "relay",
        ):
            assert k in dims
        assert isinstance(dims["distance"], int)
        assert isinstance(dims["stroke_code"], int)
        assert isinstance(dims["stroke_name"], str)
        assert isinstance(dims["relay"], bool)
        assert dims["gender_agnostic"] in ("M", "F", "X", "")

    def test_all_events_have_resolvable_dims(self, samples_dir):
        loader = HytekEventLoader(str(samples_dir / "DemoMeet-MixedEvent.hy3"))
        for ev in loader.event_meta:
            dims = loader.get_event_dims(ev)
            assert dims is not None
            # stroke_name must be populated for any non-zero stroke_code.
            if dims["stroke_code"]:
                assert dims["stroke_name"], f"event {ev} stroke not resolved"
