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
    ['start_running', 'PreRace', 'Running'],
    ['go_blank', 'PreRace', 'Blank'],
    ['go_total_blank', 'PreRace', 'TotalBlank'],

    # Running -> Finished when all active running lanes stop
    ['finish', 'Running', 'Finished'],

    # Finished -> Clear when lanes blank but event/heat still present
    ['clear_lanes', 'Finished', 'Clear'],

    # Finished -> PreRace when event/heat changes
    ['change_event', 'Finished', 'PreRace'],

    # Clear -> Running when a lane starts running (pre-race skipped)
    ['start_running', 'Clear', 'Running'],

    # Clear -> ClearPreRace when event/heat changes while in Clear
    ['change_event', 'Clear', 'ClearPreRace'],

    # ClearPreRace -> Running
    ['start_running', 'ClearPreRace', 'Running'],

    # ClearPreRace -> PreRace when non-blank lane data arrives
    ['show_lanes', 'ClearPreRace', 'PreRace'],

    # Clear -> Blank/TotalBlank when event/heat disappears
    ['go_blank', 'Clear', 'Blank'],
    ['go_total_blank', 'Clear', 'TotalBlank'],

    # Blank transitions
    ['change_event', 'Blank', 'BlankPreRace'],
    ['start_running', 'Blank', 'Running'],
    ['show_lanes', 'Blank', 'PreRace'],
    ['go_total_blank', 'Blank', 'TotalBlank'],

    # BlankPreRace transitions
    ['start_running', 'BlankPreRace', 'Running'],
    ['show_lanes', 'BlankPreRace', 'PreRace'],

    # TotalBlank transitions
    ['change_event', 'TotalBlank', 'TotalBlankPreRace'],
    ['go_blank', 'TotalBlank', 'Blank'],
    ['show_lanes', 'TotalBlank', 'PreRace'],
    ['start_running', 'TotalBlank', 'Running'],

    # TotalBlankPreRace transitions
    ['start_running', 'TotalBlankPreRace', 'Running'],
    ['show_lanes', 'TotalBlankPreRace', 'PreRace'],

    # Running -> PreRace on event/heat change (unusual but possible)
    ['change_event', 'Running', 'PreRace'],

    # PreRace -> PreRace on event/heat change (reflexive, resets context)
    ['change_event', 'PreRace', 'PreRace'],

    # ClearPreRace -> ClearPreRace on another event change
    ['change_event', 'ClearPreRace', 'ClearPreRace'],
    # BlankPreRace -> BlankPreRace on another event change
    ['change_event', 'BlankPreRace', 'BlankPreRace'],
    # TotalBlankPreRace -> TotalBlankPreRace on another event change
    ['change_event', 'TotalBlankPreRace', 'TotalBlankPreRace'],
]


class RaceStateMachine:
    """Server-side state machine tracking the CTS scoreboard race lifecycle.

    Call evaluate_update() with the accumulated channel_running array and
    latest update dict after each parse_line() cycle.  The machine determines
    which trigger to fire based on the data snapshot.

    The current state name is available via .state (a string).
    """

    def __init__(self):
        # Shadow state for accumulated display data
        self._active_running_lanes = set()
        self._current_event_heat = None  # (event_str, heat_str)
        self._scores = {'score_home': '', 'score_guest1': '', 'score_guest2': '', 'score_guest3': ''}
        self._prev_state = None

        self.machine = LockedMachine(
            model=self,
            states=RaceState,
            transitions=TRANSITIONS,
            initial=RaceState.TotalBlank,
            ignore_invalid_triggers=True,
            after_state_change='_on_state_changed',
        )

    def _on_state_changed(self):
        """Called after every state transition."""
        new_state = self.state
        if new_state != self._prev_state:
            logger.info("Race state: %s -> %s", self._prev_state, new_state)
            self._prev_state = new_state

    # ------------------------------------------------------------------
    # Callback stubs for future behavior hooks
    # ------------------------------------------------------------------
    def on_enter_PreRace(self):
        self._active_running_lanes.clear()

    def on_enter_Running(self):
        pass

    def on_enter_Finished(self):
        pass

    def on_enter_Clear(self):
        pass

    def on_enter_ClearPreRace(self):
        self._active_running_lanes.clear()

    def on_enter_Blank(self):
        pass

    def on_enter_BlankPreRace(self):
        self._active_running_lanes.clear()

    def on_enter_TotalBlank(self):
        pass

    def on_enter_TotalBlankPreRace(self):
        self._active_running_lanes.clear()

    # ------------------------------------------------------------------
    # Main evaluation logic — call once per parse_line cycle
    # ------------------------------------------------------------------
    def evaluate_update(self, channel_running, update, num_lanes=10):
        """Examine current data and trigger appropriate state transitions.

        Args:
            channel_running: list of bool, index 0..9 for lanes 1..10
            update: dict of fields being sent this cycle (keys like
                    'current_event', 'lane_time3', 'lane_running3', etc.)
            num_lanes: number of lanes in use
        """
        # ------ 1. Detect event/heat change ------
        ev = update.get('current_event')
        ht = update.get('current_heat')
        if ev is not None or ht is not None:
            new_eh = (ev or (self._current_event_heat[0] if self._current_event_heat else ''),
                      ht or (self._current_event_heat[1] if self._current_event_heat else ''))
            if new_eh != self._current_event_heat:
                self._current_event_heat = new_eh
                # Fire change_event trigger
                self.trigger('change_event')

        # ------ 2. Update running lanes set and detect transitions ------
        had_running = len(self._active_running_lanes) > 0

        for i in range(num_lanes):
            key = 'lane_running%d' % (i + 1)
            if key in update:
                if update[key]:
                    self._active_running_lanes.add(i + 1)
                else:
                    self._active_running_lanes.discard(i + 1)

        has_running = len(self._active_running_lanes) > 0

        if has_running and not had_running:
            self.trigger('start_running')
        elif not has_running and had_running:
            self.trigger('finish')

        # ------ 3. Track scores ------
        for key in ('score_home', 'score_guest1', 'score_guest2', 'score_guest3'):
            if key in update:
                self._scores[key] = update[key]

        # ------ 4. Detect blank/clear states ------
        # Only evaluate if we're in Finished (→ Clear) or Clear/Blank states
        current = self.state
        if current in (RaceState.Finished, RaceState.Clear, RaceState.ClearPreRace,
                       RaceState.Blank, RaceState.BlankPreRace,
                       RaceState.TotalBlank, RaceState.TotalBlankPreRace,
                       RaceState.PreRace):
            self._evaluate_blank_state(channel_running, update, num_lanes)

    def _has_nonzero_scores(self):
        """Return True if any score is non-empty and non-zero."""
        for val in self._scores.values():
            if val and val.strip() and val.strip() != '0':
                return True
        return False

    def _evaluate_blank_state(self, channel_running, update, num_lanes):
        """Check if display has gone to Clear, Blank, or TotalBlank."""
        has_event_heat = False
        if self._current_event_heat:
            ev_str, ht_str = self._current_event_heat
            has_event_heat = bool(ev_str.strip()) and bool(ht_str.strip())

        scores_present = self._has_nonzero_scores()

        # Separate lane 3 (running clock channel) from result lanes
        other_lanes_blank = True  # All lanes except lane 3 have blank times
        lane3_has_data = False    # Lane 3 shows clock or any data

        has_lane_data = False
        for i in range(1, num_lanes + 1):
            key = 'lane_time%d' % i
            if key in update:
                has_lane_data = True
                val = update[key]
                if val and val.strip():
                    if i == 3:
                        lane3_has_data = True
                    else:
                        other_lanes_blank = False

        # Only act if we have lane data in this update
        if not has_lane_data:
            return

        current = self.state
        if current == RaceState.Finished and other_lanes_blank and has_event_heat:
            self.trigger('clear_lanes')
        elif current in (RaceState.Clear, RaceState.ClearPreRace, RaceState.PreRace) and other_lanes_blank and not has_event_heat and not scores_present:
            if lane3_has_data:
                self.trigger('go_blank')
            else:
                self.trigger('go_total_blank')
        elif current in (RaceState.Blank, RaceState.BlankPreRace) and not lane3_has_data and not scores_present:
            self.trigger('go_total_blank')
        elif current in (RaceState.TotalBlank, RaceState.TotalBlankPreRace) and lane3_has_data and other_lanes_blank:
            self.trigger('go_blank')

        # If result lanes have non-blank data, transition to PreRace from blank states
        if not other_lanes_blank:
            self.trigger('show_lanes')

    def notify_event_change(self):
        """Call when send_event_info() fires due to event/heat change
        or client connect — ensures FSM is in a reasonable state."""
        self.trigger('change_event')

    @property
    def state_name(self):
        """Return the current state as a plain string."""
        return self.state if isinstance(self.state, str) else self.state.value
