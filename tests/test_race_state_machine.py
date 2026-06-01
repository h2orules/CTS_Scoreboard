import pytest

from race_state_machine import RaceState, RaceStateMachine


def board(*, event_heat=None, running_lanes=None, lane_times=None,
          scores=None, num_lanes=6):
    """Shortcut for building a board snapshot dict for tests."""
    return {
        "event_heat": event_heat,
        "running_lanes": set(running_lanes or []),
        "lane_times": dict(lane_times or {}),
        "scores": scores,
        "num_lanes": num_lanes,
    }


class TestInitialState:
    def test_starts_at_total_blank(self):
        fsm = RaceStateMachine()
        assert fsm.state == RaceState.TotalBlank

    def test_state_name(self):
        fsm = RaceStateMachine()
        assert fsm.state_name == "TotalBlank"

    def test_no_active_running_lanes(self):
        fsm = RaceStateMachine()
        assert len(fsm._prev_running_lanes) == 0

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

        fsm.evaluate_update(board(running_lanes={1}))
        assert fsm.state == RaceState.Running

    def test_all_lanes_stop_triggers_finish(self):
        fsm = RaceStateMachine()
        fsm.trigger("show_lanes")
        fsm.evaluate_update(board(running_lanes={1}))
        assert fsm.state == RaceState.Running
        fsm.evaluate_update(board(running_lanes=set()))
        assert fsm.state == RaceState.Finished

    def test_multiple_lanes_running(self):
        fsm = RaceStateMachine()
        fsm.trigger("show_lanes")
        fsm.evaluate_update(board(running_lanes={1, 2}))
        assert fsm.state == RaceState.Running
        # One lane finishes but another still running
        fsm.evaluate_update(board(running_lanes={2}))
        assert fsm.state == RaceState.Running
        # Last lane finishes
        fsm.evaluate_update(board(running_lanes=set()))
        assert fsm.state == RaceState.Finished

    def test_event_change_detected(self):
        # With the snapshot model, an empty lane_times dict legitimately means
        # "all lanes blank". When event_heat arrives but no lane data is
        # present, the FSM correctly upgrades TotalBlankPreRace -> ClearPreRace
        # (event/heat known, lanes empty == Clear-equivalent).
        fsm = RaceStateMachine()
        fsm.evaluate_update(board(event_heat=("1", "1")))
        assert fsm.state == RaceState.ClearPreRace

    def test_blank_detection_after_finish(self):
        fsm = RaceStateMachine()
        # Manually drive into Finished via triggers, carrying realistic
        # non-blank lane_times so the snapshot-based blank evaluator doesn't
        # immediately demote Finished -> Clear before the final blank step.
        fsm.trigger("show_lanes")
        running_times = {i: "   12.3" for i in range(1, 7)}
        fsm.evaluate_update(board(
            event_heat=("1", "1"), running_lanes={1}, lane_times=running_times,
        ))
        finish_times = {i: "   15.23" for i in range(1, 7)}
        fsm.evaluate_update(board(
            event_heat=("1", "1"), running_lanes=set(), lane_times=finish_times,
        ))
        assert fsm.state == RaceState.Finished

        # Lanes blank but event/heat still present -> Clear
        fsm.evaluate_update(board(
            event_heat=("1", "1"),
            running_lanes=set(),
            lane_times={i: "        " for i in range(1, 7)},
        ))
        assert fsm.state == RaceState.Clear

    def test_score_tracking(self):
        fsm = RaceStateMachine()
        fsm.evaluate_update(board(scores={"score_home": "42",
                                          "score_guest1": "",
                                          "score_guest2": "",
                                          "score_guest3": ""}))
        assert fsm._scores["score_home"] == "42"


class TestPerChannelFinishStream:
    """Even though `evaluate_update` now receives a full board snapshot, the
    real CTS still fires one packet per channel. Each packet causes a fresh
    snapshot to be passed to the FSM with one more lane filled in. These
    regression tests simulate that arrival pattern."""

    def _drive_into_running(self, fsm, num_lanes=6):
        fsm.evaluate_update(board(event_heat=("1", "1")))
        fsm.trigger("show_lanes")
        running = {i: " 12.3" for i in range(1, num_lanes + 1)}
        fsm.evaluate_update(board(
            event_heat=("1", "1"),
            running_lanes=set(range(1, num_lanes + 1)),
            lane_times=running,
            num_lanes=num_lanes,
        ))
        assert fsm.state == RaceState.Running
        return fsm

    def test_empty_lane_in_finish_stream_does_not_trigger_clear(self):
        """Lane 5 is empty (blank time) but the others have real finish
        times. The per-channel arrival of lane 5's blank time must not push
        the FSM to Clear and then warn on the next lane's non-blank time."""
        fsm = RaceStateMachine()
        self._drive_into_running(fsm)

        # All lanes stop running (snapshot rebuilt with empty running set)
        fsm.evaluate_update(board(
            event_heat=("1", "1"),
            running_lanes=set(),
            lane_times={i: " 12.3" for i in range(1, 7)},
        ))
        assert fsm.state == RaceState.Finished

        # Finish times arrive per-channel; lane 5 is an empty lane (blank).
        finish_times = {1: "  15.23", 2: "  16.89", 3: "  17.54",
                        4: "  17.55", 5: "        ", 6: "  18.50"}
        accumulated = {i: " 12.3" for i in range(1, 7)}
        for lane, t in finish_times.items():
            accumulated[lane] = t
            fsm.evaluate_update(board(
                event_heat=("1", "1"),
                running_lanes=set(),
                lane_times=dict(accumulated),
            ))
            assert fsm.state == RaceState.Finished, (
                "Transitioned out of Finished after lane %d update" % lane
            )

    def test_per_channel_clear_after_all_lanes_blanked(self):
        """When CTS blanks every lane one-at-a-time, Finished should advance
        to Clear only once the accumulated picture actually shows all result
        lanes blank."""
        fsm = RaceStateMachine()
        self._drive_into_running(fsm)
        fsm.evaluate_update(board(
            event_heat=("1", "1"),
            running_lanes=set(),
            lane_times={i: "  15.23" for i in range(1, 7)},
        ))
        assert fsm.state == RaceState.Finished

        # Blank lanes one at a time. Should stay Finished until the LAST
        # non-blank lane is cleared.
        accumulated = {i: "  15.23" for i in range(1, 7)}
        for i in range(1, 6):
            accumulated[i] = "        "
            fsm.evaluate_update(board(
                event_heat=("1", "1"),
                running_lanes=set(),
                lane_times=dict(accumulated),
            ))
            assert fsm.state == RaceState.Finished
        accumulated[6] = "        "
        fsm.evaluate_update(board(
            event_heat=("1", "1"),
            running_lanes=set(),
            lane_times=dict(accumulated),
        ))
        assert fsm.state == RaceState.Clear


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
    """The previous FSM cleared an internal `_active_running_lanes` set on
    entry to PreRace-style states. With the Option-A snapshot API the FSM
    no longer owns that cache — the caller's next snapshot is authoritative
    — so these tests just confirm a clean transition into the PreRace
    variants without leaking running state across races."""

    def test_clear_prerace_after_clear_change_event(self):
        fsm = RaceStateMachine()
        fsm.trigger("show_lanes")
        fsm.trigger("start_running")
        fsm.trigger("finish")
        fsm.trigger("clear_lanes")
        fsm.trigger("change_event")
        assert fsm.state == RaceState.ClearPreRace

    def test_next_snapshot_drives_pre_running_lanes(self):
        """After a fresh PreRace, the next snapshot's running_lanes should
        drive the start_running edge regardless of what the FSM remembered
        from the previous race."""
        fsm = RaceStateMachine()
        running_times = {i: "   12.3" for i in range(1, 7)}
        finish_times = {i: "   15.23" for i in range(1, 7)}
        # First race
        fsm.evaluate_update(board(event_heat=("1", "1"), running_lanes={1},
                                   lane_times=running_times))
        fsm.evaluate_update(board(event_heat=("1", "1"), running_lanes=set(),
                                   lane_times=finish_times))
        assert fsm.state == RaceState.Finished
        # Operator advances event; FSM lands in PreRace
        fsm.evaluate_update(board(event_heat=("2", "1"), running_lanes=set(),
                                   lane_times=finish_times))
        assert fsm.state == RaceState.PreRace
        # Next snapshot has lanes running again
        fsm.evaluate_update(board(event_heat=("2", "1"), running_lanes={1, 2},
                                   lane_times=running_times))
        assert fsm.state == RaceState.Running
