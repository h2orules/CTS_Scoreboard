import enum
import logging

from transitions.extensions import LockedMachine

logger = logging.getLogger(__name__)


class RaceState(enum.Enum):
    PreRace = "PreRace"
    Running = "Running"
    Finished = "Finished"
    Clear = "Clear"
    ClearPreRace = "ClearPreRace"
    Blank = "Blank"
    BlankPreRace = "BlankPreRace"
    TotalBlank = "TotalBlank"
    TotalBlankPreRace = "TotalBlankPreRace"


# All valid transitions as [trigger, source, dest]
TRANSITIONS = [
    # PreRace -> Running when any lane starts running
    ["start_running", "PreRace", "Running"],
    ["go_blank", "PreRace", "Blank"],
    ["go_total_blank", "PreRace", "TotalBlank"],
    # Running -> Finished when all active running lanes stop
    ["finish", "Running", "Finished"],
    # Finished -> Clear when lanes blank but event/heat still present
    ["clear_lanes", "Finished", "Clear"],
    # Finished -> PreRace when event/heat changes
    ["change_event", "Finished", "PreRace"],
    # Clear -> Running when a lane starts running (pre-race skipped)
    ["start_running", "Clear", "Running"],
    # Clear -> ClearPreRace when event/heat changes while in Clear
    ["change_event", "Clear", "ClearPreRace"],
    # ClearPreRace -> Running
    ["start_running", "ClearPreRace", "Running"],
    # ClearPreRace -> PreRace when non-blank lane data arrives
    ["show_lanes", "ClearPreRace", "PreRace"],
    # Clear -> Blank/TotalBlank when event/heat disappears
    ["go_blank", "Clear", "Blank"],
    ["go_total_blank", "Clear", "TotalBlank"],
    # ClearPreRace -> Blank/TotalBlank when event/heat disappears
    ["go_blank", "ClearPreRace", "Blank"],
    ["go_total_blank", "ClearPreRace", "TotalBlank"],
    # Blank transitions
    ["change_event", "Blank", "BlankPreRace"],
    ["start_running", "Blank", "Running"],
    ["show_lanes", "Blank", "PreRace"],
    ["clear_lanes", "Blank", "Clear"],
    ["go_total_blank", "Blank", "TotalBlank"],
    # BlankPreRace transitions
    ["start_running", "BlankPreRace", "Running"],
    ["show_lanes", "BlankPreRace", "PreRace"],
    ["clear_lanes", "BlankPreRace", "ClearPreRace"],
    # TotalBlank transitions
    ["change_event", "TotalBlank", "TotalBlankPreRace"],
    ["go_blank", "TotalBlank", "Blank"],
    ["show_lanes", "TotalBlank", "PreRace"],
    ["clear_lanes", "TotalBlank", "Clear"],
    ["start_running", "TotalBlank", "Running"],
    # TotalBlankPreRace transitions
    ["start_running", "TotalBlankPreRace", "Running"],
    ["show_lanes", "TotalBlankPreRace", "PreRace"],
    ["clear_lanes", "TotalBlankPreRace", "ClearPreRace"],
    # Running -> PreRace on event/heat change (unusual but possible)
    ["change_event", "Running", "PreRace"],
    # PreRace -> PreRace on event/heat change (reflexive, resets context)
    ["change_event", "PreRace", "PreRace"],
    # ClearPreRace -> ClearPreRace on another event change
    ["change_event", "ClearPreRace", "ClearPreRace"],
    # BlankPreRace -> BlankPreRace on another event change
    ["change_event", "BlankPreRace", "BlankPreRace"],
    # TotalBlankPreRace -> TotalBlankPreRace on another event change
    ["change_event", "TotalBlankPreRace", "TotalBlankPreRace"],
]


class RaceStateMachine:
    """Server-side state machine tracking the CTS scoreboard race lifecycle.

    Call ``evaluate_update(board)`` with a full snapshot of the current
    display state after each parse_line() cycle. The FSM compares the new
    snapshot to its remembered previous snapshot to detect transitions and
    fire the appropriate trigger.

    The board snapshot is a dict with these keys (all optional; sensible
    defaults are used):
        - ``event_heat``: tuple ``(ev_str, ht_str)`` of the currently selected
          event/heat strings. ``('', '')`` (or any pair where either side is
          blank) is treated as "no event/heat displayed".
        - ``running_lanes``: iterable of int lane numbers (1-indexed) that
          are currently running.
        - ``lane_times``: dict ``{lane_number: time_string}`` of the
          currently displayed time per lane. Blank/whitespace strings count
          as no time.
        - ``scores``: dict of score_home / score_guest1 / score_guest2 /
          score_guest3 strings.
        - ``num_lanes``: number of lanes in use (default 10).

    The FSM keeps no per-lane shadow cache of its own — the caller already
    has the canonical state (``lane_info``, ``channel_running``,
    ``team_scores``, ``event_heat_info``) and passes a snapshot built from
    that. The FSM only remembers the previous snapshot's event_heat and
    running set to detect edges.

    The current state name is available via ``.state`` (a string).
    """

    def __init__(self):
        # Snapshot tracking for edge detection
        self._prev_event_heat = None  # (ev_str, ht_str) or None
        self._prev_running_lanes = set()  # set of int lane numbers
        # Last seen scores + lane_times, retained between calls so callers
        # can pass partial boards (mainly the tests). Production callers
        # always pass full snapshots.
        self._scores = {
            "score_home": "",
            "score_guest1": "",
            "score_guest2": "",
            "score_guest3": "",
        }
        self._lane_times = {}
        self._prev_state = None

        self.machine = LockedMachine(
            model=self,
            states=RaceState,
            transitions=TRANSITIONS,
            initial=RaceState.TotalBlank,
            ignore_invalid_triggers=True,
            after_state_change="_on_state_changed",
        )

    def _on_state_changed(self):
        """Called after every state transition."""
        new_state = self.state
        if new_state != self._prev_state:
            logger.info("Race state: %s -> %s", self._prev_state, new_state)
            self._prev_state = new_state

    # ------------------------------------------------------------------
    # Main evaluation logic — call once per parse_line cycle
    # ------------------------------------------------------------------
    def evaluate_update(self, board):
        """Examine a full board snapshot and fire any needed transitions.

        See class docstring for the ``board`` dict shape.
        """
        num_lanes = board.get("num_lanes", 10)

        # ------ 1. Detect event/heat change ------
        new_eh = board.get("event_heat")
        if new_eh is not None and new_eh != self._prev_event_heat:
            self._prev_event_heat = new_eh
            self.trigger("change_event")

        # ------ 2. Running lane edge detection ------
        running = set(board.get("running_lanes") or [])
        had_running = bool(self._prev_running_lanes)
        has_running = bool(running)
        self._prev_running_lanes = running
        if has_running and not had_running:
            self.trigger("start_running")
        elif not has_running and had_running:
            self.trigger("finish")

        # ------ 3. Score + lane time snapshot ------
        if "scores" in board and board["scores"] is not None:
            self._scores = dict(board["scores"])
        if "lane_times" in board and board["lane_times"] is not None:
            self._lane_times = dict(board["lane_times"])

        # ------ 4. Blank/clear evaluation ------
        current = self.state
        if current in (
            RaceState.Finished,
            RaceState.Clear,
            RaceState.ClearPreRace,
            RaceState.Blank,
            RaceState.BlankPreRace,
            RaceState.TotalBlank,
            RaceState.TotalBlankPreRace,
            RaceState.PreRace,
        ):
            self._evaluate_blank_state(num_lanes)

    def _has_nonzero_scores(self):
        """Return True if any score is non-empty and non-zero."""
        for val in self._scores.values():
            if val and val.strip() and val.strip() != "0":
                return True
        return False

    def _evaluate_blank_state(self, num_lanes):
        """Check if display has gone to Clear, Blank, or TotalBlank.

        Reads from the snapshot last passed to ``evaluate_update`` (kept in
        ``self._lane_times`` / ``self._prev_event_heat`` / ``self._scores``).
        """
        has_event_heat = False
        if self._prev_event_heat:
            ev_str, ht_str = self._prev_event_heat
            has_event_heat = bool(ev_str.strip()) and bool(ht_str.strip())

        scores_present = self._has_nonzero_scores()

        # Separate lane 3 (running clock channel) from result lanes
        other_lanes_blank = True  # All lanes except lane 3 have blank times
        lane3_has_data = False  # Lane 3 shows clock or any data
        for i in range(1, num_lanes + 1):
            val = self._lane_times.get(i, "")
            if val and val.strip():
                if i == 3:
                    lane3_has_data = True
                else:
                    other_lanes_blank = False

        current = self.state
        if current == RaceState.Finished and other_lanes_blank and has_event_heat:
            self.trigger("clear_lanes")
        elif (
            current in (RaceState.Clear, RaceState.ClearPreRace, RaceState.PreRace)
            and other_lanes_blank
            and not has_event_heat
            and not scores_present
        ):
            if lane3_has_data:
                self.trigger("go_blank")
            else:
                self.trigger("go_total_blank")
        elif (
            current
            in (
                RaceState.Blank,
                RaceState.BlankPreRace,
                RaceState.TotalBlank,
                RaceState.TotalBlankPreRace,
            )
            and other_lanes_blank
            and has_event_heat
        ):
            self.trigger("clear_lanes")
        elif (
            current in (RaceState.Blank, RaceState.BlankPreRace)
            and not lane3_has_data
            and not scores_present
        ):
            self.trigger("go_total_blank")
        elif (
            current in (RaceState.TotalBlank, RaceState.TotalBlankPreRace)
            and lane3_has_data
            and other_lanes_blank
        ):
            self.trigger("go_blank")

        # If result lanes have non-blank data, transition out of blank states.
        # Only fire from actual blank states — show_lanes isn't defined from
        # Finished/Clear/Running, and firing it there just produces noisy
        # warnings without doing useful work.
        if not other_lanes_blank and current in (
            RaceState.Blank,
            RaceState.BlankPreRace,
            RaceState.TotalBlank,
            RaceState.TotalBlankPreRace,
        ):
            self.trigger("show_lanes")

    def notify_event_change(self):
        """Call when send_event_info() fires due to event/heat change
        or client connect — ensures FSM is in a reasonable state."""
        self.trigger("change_event")

    @property
    def state_name(self):
        """Return the current state as a plain string."""
        return self.state if isinstance(self.state, str) else self.state.value
