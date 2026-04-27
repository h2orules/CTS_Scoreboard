# ---------------------------------------------------------------------------
# Test simulation handlers
# ---------------------------------------------------------------------------
"""Simulation / demo-mode helpers for the CTS Scoreboard.

Call ``register(socketio, app_module)`` once after the Flask-SocketIO instance
and all referenced globals (event_info, race_fsm, …) exist.  This wires up
the ``sim_load_event`` and ``sim_step`` WebSocket handlers on the
``/scoreboard`` namespace.
"""

from hytek_st2_parser import St2File, St2Header, St2Event, CourseStandards, QualifyingTime, TimeStandard
from hytek_rec_parser import RecFile, RecHeader, SwimRecord
from hytek_parser.hy3.enums import GenderAge
from datetime import date

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
_sim_running = False  # Whether the sim clock is ticking
_sio = None           # socketio instance, set by register()
_app = None           # reference to CTS_Scoreboard module, set by register()

SIM_LANES = {
    1: {'name': 'Timmy Splash',   'team': 'HOME', 'age_code': '8B', 'seed': 17.50, 'time': 15.23, 'place': '1'},
    2: {'name': 'Sally Wave',     'team': 'AWAY', 'age_code': '8G', 'seed': 18.50, 'time': 16.89, 'place': '4'},
    3: {'name': 'James Pullbuoy', 'team': 'HOME', 'age_code': '7B', 'seed': 17.22, 'time': 17.54, 'place': '5'},
    4: {'name': 'Bobby Kick',     'team': 'HOME', 'age_code': '8B', 'seed': 19.00, 'time': 17.55, 'place': '5'},
    5: {'name': 'Lily Laneline',  'team': 'AWAY', 'age_code': '8G', 'seed': 17.20, 'time': 15.87, 'place': '2'},
    6: {'name': 'Max Dive',       'team': 'HOME', 'age_code': '8B', 'seed': 16.50, 'time': 18.50, 'place': '6'},
}


def _format_lane_time(seconds, final=True):
    """Format seconds CTS-style (8 chars, right-justified).

    CTS behaviors reproduced here:
    - Minutes are never zero-padded (shown as space when <10).
    - The tens-of-seconds digit is not zero-padded (space when seconds<10).
    - The ones-of-seconds digit is always shown.
    - While the race is running CTS emits only one decimal digit (tenths);
      the hundredths digit appears only when the time is final.
    """
    m = int(seconds) // 60
    s = seconds - m * 60
    if final:
        secs_str = '%5.2f' % s   # e.g. '15.87' or ' 5.23'
    else:
        secs_str = '%4.1f' % s   # e.g. '15.8' or ' 5.2'
    if m > 0:
        out = '%d:%s' % (m, secs_str)
    else:
        out = secs_str
    return out.rjust(8)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(socketio, app_module):
    """Wire up simulation socket handlers.

    Parameters
    ----------
    socketio : flask_socketio.SocketIO
        The SocketIO instance to register handlers on.
    app_module : module
        The ``CTS_Scoreboard`` module so we can read/write its globals
        (``event_info``, ``race_fsm``, ``settings``, etc.).
    """
    global _sio, _app
    _sio = socketio
    _app = app_module

    @socketio.on('sim_load_event', namespace='/scoreboard')
    def ws_sim_load_event(d=None):
        """Populate event_info, time_standards, and records with test data."""
        ev_num = 99
        heat_num = 1

        # --- Populate event_info ---
        _app.event_info.event_names[ev_num] = "Mixed 8 & Under 25 Yard Freestyle"
        _app.event_info.events[(ev_num, heat_num)] = {}
        _app.event_info.teams[(ev_num, heat_num)] = {}
        _app.event_info.age_codes[(ev_num, heat_num)] = {}
        _app.event_info.seed_times[(ev_num, heat_num)] = {}

        for lane, data in SIM_LANES.items():
            _app.event_info.events[(ev_num, heat_num)][lane] = data['name']
            _app.event_info.teams[(ev_num, heat_num)][lane] = data['team']
            _app.event_info.age_codes[(ev_num, heat_num)][lane] = data['age_code']
            _app.event_info.seed_times[(ev_num, heat_num)][lane] = data['seed']

        _app.event_info.event_meta[ev_num] = {
            'stroke_code': 1,
            'distance': 25,
            'relay': False,
            'age_min': None,
            'age_max': 8,
            'sex_codes': [1, 2],
            'is_mixed': True,
            'gender_age': GenderAge.BOY_S,
        }
        _app.event_info.has_names = True

        # --- Fake time standards: Boys A=16.00 B=18.00, Girls A=17.00 B=19.00 ---
        std_a = TimeStandard(tag='A', description='A Time')
        std_b = TimeStandard(tag='B', description='B Time')

        def _make_st2_event(sex_code, a_secs, b_secs):
            return St2Event(
                event_number=0, sex='Male' if sex_code == 1 else 'Female',
                sex_code=sex_code, stroke='Freestyle', stroke_code=1,
                distance=25, age_group_min=None, age_group_max=8,
                event_type='Individual',
                courses=[CourseStandards(course='SCY', times=[
                    QualifyingTime(standard=std_a, time_seconds=a_secs, time_formatted=_format_lane_time(a_secs).strip()),
                    QualifyingTime(standard=std_b, time_seconds=b_secs, time_formatted=_format_lane_time(b_secs).strip()),
                ])]
            )

        _app.time_standards = St2File(
            header=St2Header(record_count=2, export_date=date.today(), standards=[std_a, std_b]),
            events=[_make_st2_event(1, 16.00, 18.00), _make_st2_event(2, 17.00, 19.00)]
        )

        # --- Fake records: Boys 15.50, Girls 16.00 ---
        def _make_record(sex_code, secs, swimmer, team, year):
            return SwimRecord(
                sex='Male' if sex_code == 1 else 'Female', sex_code=sex_code,
                stroke='Freestyle', stroke_code=1, distance=25,
                age_group_min=None, age_group_max=8, event_type='Individual',
                swimmer_name=swimmer, team=team, relay_names=None,
                record_date=date(year, 1, 1), time_seconds=secs,
                time_formatted=_format_lane_time(secs).strip(),
                record_team=team, entry_type='A20'
            )

        _app.swim_record_sets = [
            {
                'rec_file': RecFile(
                    header=RecHeader(course='SCY', course_code='Y', record_set_name='Midlakes Records',
                                     software_version='SIM', record_count=2, export_date=date.today()),
                    records=[
                        _make_record(1, 15.50, 'Jimmy Fast', 'MIDL', 2024),
                        _make_record(2, 16.00, 'Sally Swift', 'MIDL', 2023),
                    ]
                ),
                'filename': 'sim_midlakes_records.rec',
                'team_tag': 'ALL',
                'set_id': 999,
            },
            {
                'rec_file': RecFile(
                    header=RecHeader(course='SCY', course_code='Y', record_set_name='Home Team',
                                     software_version='SIM', record_count=2, export_date=date.today()),
                    records=[
                        _make_record(1, 15.75, 'Henry Home', 'HOME', 2022),
                    ]
                ),
                'filename': 'sim_home_records.rec',
                'team_tag': 'HOME',
                'set_id': 1000,
            },
            {
                'rec_file': RecFile(
                    header=RecHeader(course='SCY', course_code='Y', record_set_name='Away Team',
                                     software_version='SIM', record_count=2, export_date=date.today()),
                    records=[
                        _make_record(1, 14.75, 'Andrew Away', 'AWAY', 2023),
                        _make_record(2, 16.40, 'Amy Away', 'AWAY', 2024),
                    ]
                ),
                'filename': 'sim_away_records.rec',
                'team_tag': 'AWAY',
                'set_id': 1001,
            },
        ]

        # --- Set scores ---
        _app.team_scores['score_home'] = ' 142'
        _app.team_scores['score_guest1'] = ' 138'
        _app.team_scores['score_guest2'] = ''
        _app.team_scores['score_guest3'] = ''

        # --- Trigger PreRace ---
        _app.last_event_sent = (ev_num, heat_num)
        _app.race_fsm.notify_event_change()
        # Transition out of blank states since we now have lane data
        _app.race_fsm.trigger('show_lanes')
        _app.send_event_info()
        _app.send_scores_info()

    @socketio.on('sim_step', namespace='/scoreboard')
    def ws_sim_step(d):
        """Advance the simulation through race phases."""
        global _sim_running

        step = d.get('step', '') if d else ''
        update = {}
        num_lanes = _app.settings.get('num_lanes', 6)

        if step == 'start':
            _sim_running = True
            _app.running_time = _format_lane_time(0.0, final=False)
            update['running_time'] = _app.running_time
            update['current_event'] = str(_app.last_event_sent[0])
            update['current_heat'] = str(_app.last_event_sent[1])
            for i in range(1, num_lanes + 1):
                _app.channel_running[i - 1] = True
                update['lane_running%d' % i] = True
                update['lane_time%d' % i] = _app.running_time
                update['lane_place%d' % i] = ' '

            _app.race_fsm.evaluate_update(_app.channel_running, update)
            update['race_state'] = _app.race_fsm.state_name
            _sio.emit('update_scoreboard', update, namespace='/scoreboard')
            # Re-send scores in case we were previously in a blank state
            _app.send_scores_info()

            # Start background clock ticker
            _sio.start_background_task(_sim_clock_tick)

        elif step == 'finish':
            _sim_running = False
            update['current_event'] = str(_app.last_event_sent[0])
            update['current_heat'] = str(_app.last_event_sent[1])
            for i in range(1, num_lanes + 1):
                _app.channel_running[i - 1] = False
                update['lane_running%d' % i] = False
                if i in SIM_LANES:
                    update['lane_time%d' % i] = _format_lane_time(SIM_LANES[i]['time'])
                    update['lane_place%d' % i] = SIM_LANES[i]['place']
                else:
                    update['lane_time%d' % i] = '        '
                    update['lane_place%d' % i] = ' '

            _app.race_fsm.evaluate_update(_app.channel_running, update)
            update['race_state'] = _app.race_fsm.state_name
            _sio.emit('update_scoreboard', update, namespace='/scoreboard')
            # Re-send scores in case we were previously in a blank state
            _app.send_scores_info()

        elif step == 'clear':
            update['current_event'] = str(_app.last_event_sent[0])
            update['current_heat'] = str(_app.last_event_sent[1])
            for i in range(1, num_lanes + 1):
                update['lane_time%d' % i] = '        '
                update['lane_place%d' % i] = ' '

            _app.race_fsm.evaluate_update(_app.channel_running, update)
            update['race_state'] = _app.race_fsm.state_name
            _sio.emit('update_scoreboard', update, namespace='/scoreboard')
            # Re-send event info so names/teams reappear on scoreboard
            _app.send_event_info()
            _app.send_scores_info()

        elif step == 'blank':
            _sim_running = False
            update['current_event'] = '   '
            update['current_heat'] = '   '
            for i in range(1, num_lanes + 1):
                _app.channel_running[i - 1] = False
                update['lane_running%d' % i] = False
            # Lane 3 still shows clock
            for i in range(1, num_lanes + 1):
                if i == 3:
                    update['lane_time%d' % i] = '    5:22'
                else:
                    update['lane_time%d' % i] = '        '
                update['lane_place%d' % i] = ' '

            # CTS blanks team scores when going Blank; emit empty values but keep
            # the server-side team_scores cache intact so we can restore on exit.
            update['score_home'] = ''
            update['score_guest1'] = ''
            update['score_guest2'] = ''
            update['score_guest3'] = ''

            _app.race_fsm.evaluate_update(_app.channel_running, update)
            update['race_state'] = _app.race_fsm.state_name
            _sio.emit('update_scoreboard', update, namespace='/scoreboard')

        elif step == 'total_blank':
            _sim_running = False
            update['current_event'] = '   '
            update['current_heat'] = '   '
            for i in range(1, num_lanes + 1):
                _app.channel_running[i - 1] = False
                update['lane_running%d' % i] = False
                update['lane_time%d' % i] = '        '
                update['lane_place%d' % i] = ' '

            # CTS blanks team scores when going TotalBlank; emit empty values but
            # keep the server-side team_scores cache intact for restoration.
            update['score_home'] = ''
            update['score_guest1'] = ''
            update['score_guest2'] = ''
            update['score_guest3'] = ''

            _app.race_fsm.evaluate_update(_app.channel_running, update)
            update['race_state'] = _app.race_fsm.state_name
            _sio.emit('update_scoreboard', update, namespace='/scoreboard')


def _sim_clock_tick():
    """Background task: increment running_time and emit to running lanes."""
    global _sim_running
    t = 0.0
    while _sim_running:
        _sio.sleep(0.1)
        if not _sim_running:
            break
        t += 0.1
        _app.running_time = _format_lane_time(t, final=False)
        tick_update = {'running_time': _app.running_time}
        num_lanes = _app.settings.get('num_lanes', 6)
        for i in range(1, num_lanes + 1):
            if _app.channel_running[i - 1]:
                tick_update['lane_time%d' % i] = _app.running_time
        _sio.emit('update_scoreboard', tick_update, namespace='/scoreboard')
