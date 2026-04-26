import pytest

from race_state_machine import RaceState, RaceStateMachine


class TestInitialState:
    def test_starts_at_total_blank(self):
        fsm = RaceStateMachine()
        assert fsm.state == RaceState.TotalBlank

    def test_state_name(self):
        fsm = RaceStateMachine()
        assert fsm.state_name == "TotalBlank"

    def test_no_active_running_lanes(self):
        fsm = RaceStateMachine()
        assert len(fsm._active_running_lanes) == 0

    def test_no_scores(self):
        fsm = RaceStateMachine()
        assert fsm._has_nonzero_scores() is False


class TestDirectTriggers:
    def test_total_blank_to_prerace(self):
        fsm = RaceStateMachine()
        fsm.trigger("show_lanes")
        assert fsm.state == RaceState.PreRace

    def test_prerace_to_running(self):
        fsm = RaceStateMachine()
        fsm.trigger("show_lanes")
        fsm.trigger("start_running")
        assert fsm.state == RaceState.Running

    def test_running_to_finished(self):
        fsm = RaceStateMachine()
        fsm.trigger("show_lanes")
        fsm.trigger("start_running")
        fsm.trigger("finish")
        assert fsm.state == RaceState.Finished

    def test_finished_to_clear(self):
        fsm = RaceStateMachine()
        fsm.trigger("show_lanes")
        fsm.trigger("start_running")
        fsm.trigger("finish")
        fsm.trigger("clear_lanes")
        assert fsm.state == RaceState.Clear

    def test_finished_to_prerace_on_event_change(self):
        fsm = RaceStateMachine()
        fsm.trigger("show_lanes")
        fsm.trigger("start_running")
        fsm.trigger("finish")
        fsm.trigger("change_event")
        assert fsm.state == RaceState.PreRace

    def test_invalid_trigger_ignored(self):
        fsm = RaceStateMachine()
        # finish is not valid from TotalBlank
        fsm.trigger("finish")
        assert fsm.state == RaceState.TotalBlank


class TestFullLifecycle:
    def test_complete_race(self):
        fsm = RaceStateMachine()
        assert fsm.state == RaceState.TotalBlank

        fsm.trigger("show_lanes")
        assert fsm.state == RaceState.PreRace

        fsm.trigger("start_running")
        assert fsm.state == RaceState.Running

        fsm.trigger("finish")
        assert fsm.state == RaceState.Finished

        fsm.trigger("clear_lanes")
        assert fsm.state == RaceState.Clear

        fsm.trigger("change_event")
        assert fsm.state == RaceState.ClearPreRace

        fsm.trigger("show_lanes")
        assert fsm.state == RaceState.PreRace

    def test_back_to_back_races(self):
        fsm = RaceStateMachine()
        # First race
        fsm.trigger("show_lanes")
        fsm.trigger("start_running")
        fsm.trigger("finish")
        fsm.trigger("change_event")
        assert fsm.state == RaceState.PreRace

        # Second race
        fsm.trigger("start_running")
        fsm.trigger("finish")
        assert fsm.state == RaceState.Finished


class TestEvaluateUpdate:
    def test_lane_running_triggers_start(self):
        fsm = RaceStateMachine()
        fsm.trigger("show_lanes")
        assert fsm.state == RaceState.PreRace

        update = {"lane_running1": True}
        channel_running = [True] + [False] * 9
        fsm.evaluate_update(channel_running, update, num_lanes=6)
        assert fsm.state == RaceState.Running

    def test_all_lanes_stop_triggers_finish(self):
        fsm = RaceStateMachine()
        fsm.trigger("show_lanes")
        fsm.evaluate_update([False] * 10, {"lane_running1": True}, num_lanes=6)
        assert fsm.state == RaceState.Running
        fsm.evaluate_update([False] * 10, {"lane_running1": False}, num_lanes=6)
        assert fsm.state == RaceState.Finished

    def test_multiple_lanes_running(self):
        fsm = RaceStateMachine()
        fsm.trigger("show_lanes")
        fsm.evaluate_update([False] * 10, {"lane_running1": True, "lane_running2": True}, num_lanes=6)
        assert fsm.state == RaceState.Running
        # One lane finishes but another is still running
        fsm.evaluate_update([False] * 10, {"lane_running1": False}, num_lanes=6)
        assert fsm.state == RaceState.Running
        # Last lane finishes
        fsm.evaluate_update([False] * 10, {"lane_running2": False}, num_lanes=6)
        assert fsm.state == RaceState.Finished

    def test_event_change_detected(self):
        fsm = RaceStateMachine()
        update = {"current_event": "1", "current_heat": "1"}
        fsm.evaluate_update([False] * 10, update, num_lanes=6)
        assert fsm.state == RaceState.TotalBlankPreRace

    def test_blank_detection_after_finish(self):
        fsm = RaceStateMachine()
        fsm.trigger("show_lanes")
        fsm.evaluate_update([False] * 10, {"lane_running1": True}, num_lanes=6)
        fsm.evaluate_update([False] * 10, {"lane_running1": False}, num_lanes=6)
        assert fsm.state == RaceState.Finished

        # Blank lane data with event still present → Clear
        fsm._current_event_heat = ("1", "1")
        blank_update = {f"lane_time{i}": "        " for i in range(1, 7)}
        fsm.evaluate_update([False] * 10, blank_update, num_lanes=6)
        assert fsm.state == RaceState.Clear

    def test_score_tracking(self):
        fsm = RaceStateMachine()
        fsm.evaluate_update([False] * 10, {"score_home": "42"}, num_lanes=6)
        assert fsm._scores["score_home"] == "42"


class TestNotifyEventChange:
    def test_from_total_blank(self):
        fsm = RaceStateMachine()
        fsm.notify_event_change()
        assert fsm.state == RaceState.TotalBlankPreRace

    def test_from_finished(self):
        fsm = RaceStateMachine()
        fsm.trigger("show_lanes")
        fsm.trigger("start_running")
        fsm.trigger("finish")
        fsm.notify_event_change()
        assert fsm.state == RaceState.PreRace


class TestHasNonzeroScores:
    def test_empty_scores(self):
        fsm = RaceStateMachine()
        assert fsm._has_nonzero_scores() is False

    def test_with_scores(self):
        fsm = RaceStateMachine()
        fsm._scores["score_home"] = "42"
        assert fsm._has_nonzero_scores() is True

    def test_zero_score(self):
        fsm = RaceStateMachine()
        fsm._scores["score_home"] = "0"
        assert fsm._has_nonzero_scores() is False

    def test_whitespace_score(self):
        fsm = RaceStateMachine()
        fsm._scores["score_home"] = "   "
        assert fsm._has_nonzero_scores() is False


class TestBlankTransitions:
    def test_prerace_to_blank(self):
        fsm = RaceStateMachine()
        fsm.trigger("show_lanes")
        fsm.trigger("go_blank")
        assert fsm.state == RaceState.Blank

    def test_clear_to_blank(self):
        fsm = RaceStateMachine()
        fsm.trigger("show_lanes")
        fsm.trigger("start_running")
        fsm.trigger("finish")
        fsm.trigger("clear_lanes")
        fsm.trigger("go_blank")
        assert fsm.state == RaceState.Blank

    def test_blank_to_total_blank(self):
        fsm = RaceStateMachine()
        fsm.trigger("show_lanes")
        fsm.trigger("go_blank")
        fsm.trigger("go_total_blank")
        assert fsm.state == RaceState.TotalBlank

    def test_total_blank_to_blank(self):
        fsm = RaceStateMachine()
        fsm.trigger("go_blank")
        assert fsm.state == RaceState.Blank

    def test_blank_to_prerace_via_show_lanes(self):
        fsm = RaceStateMachine()
        fsm.trigger("go_blank")
        fsm.trigger("show_lanes")
        assert fsm.state == RaceState.PreRace

    def test_blank_change_event(self):
        fsm = RaceStateMachine()
        fsm.trigger("go_blank")
        fsm.trigger("change_event")
        assert fsm.state == RaceState.BlankPreRace

    def test_total_blank_change_event(self):
        fsm = RaceStateMachine()
        fsm.trigger("change_event")
        assert fsm.state == RaceState.TotalBlankPreRace


class TestPreRaceClears:
    def test_on_enter_prerace_clears_lanes(self):
        fsm = RaceStateMachine()
        fsm._active_running_lanes.add(1)
        fsm.trigger("show_lanes")
        assert len(fsm._active_running_lanes) == 0

    def test_on_enter_clear_prerace_clears_lanes(self):
        fsm = RaceStateMachine()
        fsm.trigger("show_lanes")
        fsm.trigger("start_running")
        fsm._active_running_lanes.add(1)
        fsm.trigger("finish")
        fsm.trigger("clear_lanes")
        fsm.trigger("change_event")
        assert fsm.state == RaceState.ClearPreRace
        assert len(fsm._active_running_lanes) == 0
