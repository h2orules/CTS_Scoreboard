"""Tests for compute_lane_display_mode — the single source of truth for
who owns the lane time/place cells on the client."""

import pytest

import CTS_Scoreboard as cts


@pytest.fixture
def restore_settings():
    saved_seed_label = cts.settings.get("seed_time_label", "Seed Time")
    saved_state = cts.race_fsm.state_name
    yield
    cts.settings["seed_time_label"] = saved_seed_label
    # Best-effort: state machine reset is opaque; tests below explicitly
    # drive the FSM where needed, so just leave it as the test left it.
    _ = saved_state


class TestLaneDisplayMode:
    def test_clear_state_returns_clear(self, restore_settings):
        cts.settings["seed_time_label"] = "Seed Time"
        assert cts.compute_lane_display_mode("Clear") == "clear"

    def test_clear_state_returns_clear_even_with_seed_none(self, restore_settings):
        # Operator's CTS Clear-Lanes submenu trumps the seed-time setting.
        cts.settings["seed_time_label"] = "None"
        assert cts.compute_lane_display_mode("Clear") == "clear"

    @pytest.mark.parametrize(
        "state",
        ["PreRace", "ClearPreRace", "BlankPreRace", "TotalBlankPreRace"],
    )
    def test_pre_race_states_with_seed_enabled(self, restore_settings, state):
        cts.settings["seed_time_label"] = "Seed Time"
        assert cts.compute_lane_display_mode(state) == "seed_times"

    @pytest.mark.parametrize(
        "state",
        ["PreRace", "ClearPreRace", "BlankPreRace", "TotalBlankPreRace"],
    )
    def test_pre_race_states_with_seed_none_fall_back_to_server(
        self, restore_settings, state
    ):
        # When the operator has disabled seed-time display, the previous
        # race's finish times should persist into the next heat — i.e.
        # the server retains ownership of the cells.
        cts.settings["seed_time_label"] = "None"
        assert cts.compute_lane_display_mode(state) == "server"

    @pytest.mark.parametrize(
        "state", ["Running", "Finished", "Blank", "TotalBlank"]
    )
    def test_non_pre_race_returns_server(self, restore_settings, state):
        cts.settings["seed_time_label"] = "Seed Time"
        assert cts.compute_lane_display_mode(state) == "server"

    def test_alternate_seed_label_still_enables_seed_times(self, restore_settings):
        cts.settings["seed_time_label"] = "Entry Time"
        assert cts.compute_lane_display_mode("PreRace") == "seed_times"
