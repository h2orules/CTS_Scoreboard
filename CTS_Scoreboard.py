#! /usr/bin/python3
import flask
import flask_login
import flask_socketio
import datetime
import traceback
import ctypes
import serial
import serial.tools.list_ports
import re
import time
import json
import os.path
import glob
from hytek_event_loader import HytekEventLoader
from hytek_parser.hy3.enums import GenderAge
from hytek_st2_parser import parse_st2_file
from hytek_rec_parser import parse_rec_file, format_record_date
from race_state_machine import RaceStateMachine
import ap

DEBUG = False
#DEBUG = True
settings_file = './settings.json'

settings = {
    'meet_title': '',
    'serial_port': 'COM1',
    'username': 'admin',
    'password': 'password',
    'ad_url': '',
    'num_lanes': 6,
    'pool_course': 'SCY',
    'show_pr_tags': True,
    'show_confetti': True,
    'seed_time_label': 'Seed Time',
    'team_home': '',
    'team_home_tag': '',
    'team_guest1': '',
    'team_guest1_tag': '',
    'team_guest2': '',
    'team_guest2_tag': '',
    'team_guest3': '',
    'team_guest3_tag': '',
    'std_desc_overrides': {}
    }
in_file = None
out_file = None
in_speed = 1.0
debug_console = False

# Event Settings
event_info = HytekEventLoader()

# Time Standards
time_standards = None

# Swim Records (list of dicts: {rec_file, filename, team_tag, set_id})
swim_record_sets = []
_next_rec_set_id = 0

app = flask.Flask(__name__)
# config
app.config.update(
    DEBUG = False,
    SECRET_KEY = 'rimnqiuqnewiornhf7nfwenjmqvliwynhtmlfnlsklrmqwe'
)
socketio = flask_socketio.SocketIO(app)

main_thread = None
event_heat_info = [' ',' ',' ',' ',' ',' ',' ',' ']
lane_info = [[],
            [0,0,0,0,0,0,0,0],
            [0,0,0,0,0,0,0,0],
            [0,0,0,0,0,0,0,0],
            [0,0,0,0,0,0,0,0],
            [0,0,0,0,0,0,0,0],
            [0,0,0,0,0,0,0,0],
            [0,0,0,0,0,0,0,0],
            [0,0,0,0,0,0,0,0],
            [0,0,0,0,0,0,0,0],
            [0,0,0,0,0,0,0,0]]
time_info = [0,0,0,0,0,0,0,0]
running_time = '        '
channel_running = [False for i in range(10)]
score_info = {0x14: [' ',' ',' ',' ',' ',' ',' ',' '],
              0x15: [' ',' ',' ',' ',' ',' ',' ',' ']}
team_scores = {'score_home': '', 'score_guest1': '', 'score_guest2': '', 'score_guest3': ''}
race_fsm = RaceStateMachine()

def load_settings():
    global settings, time_standards, swim_record_sets, _next_rec_set_id
    try:
        with open(settings_file, "rt") as f:
            settings.update(json.load(f))
        if 'event_info' in settings:
            event_info.from_object(settings['event_info'])
        if 'time_standards' in settings:
            import pickle, base64
            time_standards = pickle.loads(base64.b64decode(settings['time_standards']))
        if 'swim_record_sets' in settings:
            import pickle, base64
            swim_record_sets = pickle.loads(base64.b64decode(settings['swim_record_sets']))
            if swim_record_sets:
                _next_rec_set_id = max(s['set_id'] for s in swim_record_sets) + 1
        elif 'swim_records' in settings:
            # Backward compat: migrate single record file to list format
            import pickle, base64
            old_rec = pickle.loads(base64.b64decode(settings['swim_records']))
            swim_record_sets = [{'rec_file': old_rec, 'filename': 'migrated.rec', 'team_tag': 'ALL', 'set_id': 0}]
            _next_rec_set_id = 1
            settings['swim_record_sets'] = base64.b64encode(pickle.dumps(swim_record_sets)).decode('ascii')
            settings.pop('swim_records', None)
            with open(settings_file, "wt") as f:
                json.dump(settings, f, sort_keys=True, indent=4)
    except: pass

## Stuff to move the cursor
def print_at(r, c, s):
    if debug_console:
        ap.output(c, r, s)   
            
def hex_to_digit(c):
    c = c & 0x0F
    c ^= 0x0F # Invert lower nybble
    if (c > 9):
        return ' '
    return ("%i" % c)

update={}
next_update = datetime.datetime.now()
last_event_sent = (1,1)

def parse_line(l, out = None):
    global event_heat_info, lane_info, time_info, running_time, update, next_update, last_event_sent, team_scores
    
    s = ""
    if out:
        k = "[%f] "% time.time() + " ".join(["%02X" % int(c) for c in l])
        out.write(k)
    try:
        # Byte 0 - Channel
        c = l.pop(0)
        running_finish = True if (c & 0x40) else False
        format_display = True if (c & 0x01) else False
        channel = ((c & 0x3E) >> 1) ^ 0x1F
        
        if (1 <= channel <= 10) and not format_display:
            channel_running[channel-1] = running_finish
            # This is a lane display of interest
            while len(l):
                c = l.pop(0)
                lane_info[channel][(c >> 4) & 0x0F] = c
            
            lane = hex_to_digit(lane_info[channel][0])
            place = hex_to_digit(lane_info[channel][1])

            update["lane_place%i"%channel] = place
            update["lane_running%i"%channel] = running_finish
            
            if running_finish:
                lane_time = running_time # '        '
                s = "%4s: running" % (channel)
            else:
                lane_time = hex_to_digit(lane_info[channel][2]) + hex_to_digit(lane_info[channel][3])
                lane_time += ':' if lane_time.strip() else ' '
                lane_time += hex_to_digit(lane_info[channel][4]) + hex_to_digit(lane_info[channel][5])
                lane_time += '.' if lane_time.strip() else ' '
                lane_time += hex_to_digit(lane_info[channel][6]) + hex_to_digit(lane_info[channel][7])
                s = "%4s: %s %s %s" % (channel, lane, place, lane_time)
                update["lane_time%i"%channel] = lane_time
                
            print_at(channel+1, 0, " " * 20)
            print_at(channel+1, 0, "%4s: %s %s %s" % (channel, lane, place, lane_time))
            
        if (channel == 0) and not format_display:
            # Running time
            while len(l):
                c = l.pop(0)
                time_info[(c >> 4) & 0x0F] = c
            running_time = hex_to_digit(time_info[2]) + hex_to_digit(time_info[3])
            running_time += ':' if running_time.strip() else ' '
            running_time += hex_to_digit(time_info[4]) + hex_to_digit(time_info[5])
            running_time += '.' if running_time.strip() else ' '
            running_time += hex_to_digit(time_info[6]) + hex_to_digit(time_info[7])
            update["running_time"] = running_time
            
            s = "Running Time: " + running_time

        if (channel == 12) and not format_display:
            # Event / Heat
            while len(l):
                c = l.pop(0)
                event_heat_info[(c >> 4) & 0x0F] = hex_to_digit(c)
                
            update["current_event"] = ''.join(event_heat_info[:3])
            update["current_heat"] = ''.join(event_heat_info[-3:])
            try:
                event_tuple = (int(update["current_event"]), int(update["current_heat"]))
            except: return

            print_at(0, 0, " Event:" +  update["current_event"] + " Heat:" + update["current_heat"] + "    ")
            
            s = " Event:" +  update["current_event"] + " Heat:" + update["current_heat"] + "    "
            
            if last_event_sent != event_tuple:
                last_event_sent = event_tuple
                send_event_info()

        if channel in (0x14, 0x15) and not format_display:
            # Team scores: 0x14 = Home + Guest 1, 0x15 = Guest 2 + Guest 3
            while len(l):
                c = l.pop(0)
                score_info[channel][(c >> 4) & 0x0F] = hex_to_digit(c)

            score_a = ''.join(score_info[channel][:4])
            score_b = ''.join(score_info[channel][-4:])

            if channel == 0x14:
                new_scores = {'score_home': score_a, 'score_guest1': score_b}
            else:
                new_scores = {'score_guest2': score_a, 'score_guest3': score_b}

            sendScores = False
            for key, val in new_scores.items():
                if team_scores[key] != val:
                    team_scores[key] = val
                    update[key] = val
                    sendScores = True

            s = "Scores ch%02X: %s / %s" % (channel, score_a.strip(), score_b.strip())

            if sendScores:
                send_scores_info()

        if out:
            if s:
                out.write(' '*max(0, 50-len(k)) + " # " + s)
            out.write("\n")
    except IndexError:
        traceback.print_exc()
        
    finally:
        #Output anything we got
        if "current_event" in update or "running_time" in update:
            race_fsm.evaluate_update(channel_running, update)
            update["race_state"] = race_fsm.state_name
            socketio.emit('update_scoreboard', update, namespace='/scoreboard')
            update.clear()
            
        if (datetime.datetime.now() > next_update) and debug_console:
            next_update = datetime.datetime.now() + datetime.timedelta(seconds=0.2)
            ap.render()


def main_thread_worker():
    j = None
    if in_file:
        delay = 0.0
        start_time = None
        with open(in_file, 'rt') as f:
            if out_file:
                j = open(out_file, "at")
            l = []
            for d in re.finditer(r"\[([0-9.]+)\]\s*|([0-9a-fA-F]{2})\s+", f.read()):
                if d.group(1):
                    if start_time:
                        delay = float(d.group(1)) - in_speed*time.time() - start_time
                        if delay > 0:
                            socketio.sleep(delay)
                        print_at(13, 0, " " + d.group(1) + "   ")
                    else:
                        start_time = float(d.group(1)) - in_speed*time.time()
                    continue
                c = int(d.group(2), 16)
                if c:
                    if (c & 0x80) or (len(l) > 8):
                        if len(l):
                            parse_line(l, j)
                        l=[]
                    l.append(c)
                if delay > (0.1):
                    delay = 0
                    socketio.sleep(0.1) # 9600 = about 1ms per character
                else:
                    delay += 1/9600.0
    else:
        with serial.Serial(settings['serial_port'], 9600, timeout=0) as f:
            if out_file:
                j = open(out_file, "at")
            l = []
            while True:
                c = f.read(1)
                if c:
                    c=c[0]
                    if (c & 0x80) or (len(l) > 8):
                        if len(l):
                            parse_line(l, j)
                        l=[]
                    l.append(c)
                else:
                    socketio.sleep(0.01)
            
# flask-login
login_manager = flask_login.LoginManager()
login_manager.init_app(app)
login_manager.login_view = "route_login"


# simple user model
class User(flask_login.UserMixin):

    def __init__(self, id):
        self.id = id
        self.name = settings['username']
        self.password = settings['password']
        
    def __repr__(self):
        return "%d/%s" % (self.id, self.name)


# create the user       
user = User(0)

def _get_qualifying_times(event_number):
    """Look up qualifying times from time_standards for a given event.
    
    Returns (list_of_dicts, show_age_codes) where list_of_dicts has
    [{time, tag, description, qualifiers}, ...] and show_age_codes is True
    when multiple standards match for age or gender reasons.
    """
    if time_standards is None:
        return [], False
    
    meta = event_info.event_meta.get(event_number)
    if not meta:
        return [], False
    
    pool_course = settings.get('pool_course', 'SCY')
    
    # Determine which st2 sex codes to search for
    sex_codes = meta.get('sex_codes', [])
    if not sex_codes:
        return [], False
    
    stroke_code = meta.get('stroke_code')
    distance = meta.get('distance')
    is_relay = meta.get('relay', False)
    age_min = meta.get('age_min')
    age_max = meta.get('age_max')
    is_mixed = meta.get('is_mixed', False)
    
    # Determine expected event_type
    event_type_match = "Relay" if is_relay else "Individual"
    
    # For combined events, collect all source event age ranges
    age_ranges = []
    combined = event_info.combined
    source_events = set()
    for src, dst in combined.items():
        if dst == (event_number, 1) or src == (event_number, 1):
            src_event_num = src[0]
            src_meta = event_info.event_meta.get(src_event_num)
            if src_meta:
                source_events.add(src_event_num)
                age_ranges.append((src_meta.get('age_min'), src_meta.get('age_max')))
    
    if not age_ranges:
        age_ranges.append((age_min, age_max))
    
    # Find matching st2 events
    # Use age-appropriate gender names: Boys/Girls for youth, Men/Women for adults
    gender_age = meta.get('gender_age')
    if gender_age in (GenderAge.MEN_S, GenderAge.WOMEN_S):
        sex_display = {1: 'Men', 2: 'Women'}
    else:
        sex_display = {1: 'Boys', 2: 'Girls'}
    matches = []
    
    for sex_code in sex_codes:
        for ar_min, ar_max in age_ranges:
            for st2_event in time_standards.events:
                if st2_event.stroke_code != stroke_code:
                    continue
                if st2_event.distance != distance:
                    continue
                if st2_event.event_type != event_type_match:
                    continue
                if st2_event.sex_code != sex_code:
                    continue
                
                # Age range overlap check
                st2_min = st2_event.age_group_min
                st2_max = st2_event.age_group_max
                ev_min = ar_min if ar_min else 0
                ev_max = ar_max if ar_max else 999
                s_min = st2_min if st2_min else 0
                s_max = st2_max if st2_max else 999
                
                if ev_min > s_max or s_min > ev_max:
                    continue
                
                # Found a match - get times for the pool course
                for cs in st2_event.courses:
                    if cs.course == pool_course:
                        for qt in cs.times:
                            matches.append({
                                'sex_code': sex_code,
                                'age_min': st2_min,
                                'age_max': st2_max,
                                'tag': qt.standard.tag,
                                'description': qt.standard.description,
                                'time': qt.time_formatted,
                                'time_seconds': qt.time_seconds,
                            })
    
    if not matches:
        return [], False
    
    # Determine which qualifiers need to be shown
    unique_sex = len(set(m['sex_code'] for m in matches)) > 1
    unique_age = len(set((m['age_min'], m['age_max']) for m in matches)) > 1
    
    # Build results grouped by qualifier string
    groups = []         # [{qualifiers: str, items: [...]}, ...]
    group_map = {}      # qualifiers_str -> index in groups
    color_idx = 0
    desc_overrides = settings.get('std_desc_overrides', {})
    for m in matches:
        qualifiers = []
        if unique_age:
            a_min = m['age_min']
            a_max = m['age_max']
            if a_min and a_max:
                qualifiers.append("%d-%d" % (a_min, a_max))
            elif a_max:
                qualifiers.append("%d & Under" % a_max)
            elif a_min:
                qualifiers.append("%d & Over" % a_min)
            else:
                qualifiers.append("Open")
        if unique_sex:
            qualifiers.append(sex_display.get(m['sex_code'], ''))
        
        qual_str = ' '.join(qualifiers)
        item = {
            'time': m['time'],
            'time_seconds': m['time_seconds'],
            'tag': m['tag'],
            'description': desc_overrides.get(m['tag'], m['description']),
            'color_class': 'qt-color-%d' % (color_idx % 12),
            'sex_code': m['sex_code'],
            'age_min': m['age_min'],
            'age_max': m['age_max'],
        }
        if qual_str not in group_map:
            group_map[qual_str] = len(groups)
            groups.append({'qualifiers': qual_str, 'items': []})
        groups[group_map[qual_str]]['items'].append(item)
        color_idx += 1
    
    return groups, (unique_sex or unique_age)

def _get_matching_records(event_number):
    """Look up swim records for a given event across all loaded record sets.
    
    Returns (list_of_set_results, show_age_codes) where list_of_set_results is:
    [{set_name, set_team_tag, records: [...]}, ...] in upload order.
    Records use strict less-than for breaking (tying does not break a record).
    """
    if not swim_record_sets:
        return [], False
    
    meta = event_info.event_meta.get(event_number)
    if not meta:
        return [], False
    
    pool_course = settings.get('pool_course', 'SCY')
    
    sex_codes = meta.get('sex_codes', [])
    if not sex_codes:
        return [], False
    
    stroke_code = meta.get('stroke_code')
    distance = meta.get('distance')
    is_relay = meta.get('relay', False)
    age_min = meta.get('age_min')
    age_max = meta.get('age_max')
    
    event_type_match = "Relay" if is_relay else "Individual"
    
    # For combined events, collect all source event age ranges
    age_ranges = []
    combined = event_info.combined
    for src, dst in combined.items():
        if dst == (event_number, 1) or src == (event_number, 1):
            src_meta = event_info.event_meta.get(src[0])
            if src_meta:
                age_ranges.append((src_meta.get('age_min'), src_meta.get('age_max')))
    if not age_ranges:
        age_ranges.append((age_min, age_max))
    
    gender_age = meta.get('gender_age')
    if gender_age in (GenderAge.MEN_S, GenderAge.WOMEN_S):
        sex_display = {1: 'Men', 2: 'Women'}
    else:
        sex_display = {1: 'Boys', 2: 'Girls'}
    
    all_set_results = []
    any_show_age = False
    color_idx = 0
    
    for rec_set in swim_record_sets:
        rec_file = rec_set['rec_file']
        
        # Check if records file course matches pool course
        if rec_file.header.course != pool_course:
            continue
        
        matches = []
        for sex_code in sex_codes:
            for ar_min, ar_max in age_ranges:
                for rec in rec_file.records:
                    if rec.stroke_code != stroke_code:
                        continue
                    if rec.distance != distance:
                        continue
                    if rec.event_type != event_type_match:
                        continue
                    if rec.sex_code != sex_code:
                        continue
                    
                    # Age range overlap check
                    rec_min = rec.age_group_min
                    rec_max = rec.age_group_max
                    ev_min = ar_min if ar_min else 0
                    ev_max = ar_max if ar_max else 999
                    r_min = rec_min if rec_min else 0
                    r_max = rec_max if rec_max else 999
                    
                    if ev_min > r_max or r_min > ev_max:
                        continue
                    
                    from hytek_rec_parser import EPOCH
                    rec_year = rec.record_date.year if rec.record_date != EPOCH else None
                    
                    matches.append({
                        'sex_code': sex_code,
                        'age_min': rec_min,
                        'age_max': rec_max,
                        'time': rec.time_formatted,
                        'time_seconds': rec.time_seconds,
                        'swimmer_name': rec.swimmer_name or '',
                        'record_team': rec.record_team or '',
                        'record_year': str(rec_year) if rec_year else '',
                        'relay_names': rec.relay_names or '',
                    })
        
        if not matches:
            continue
        
        unique_sex = len(set(m['sex_code'] for m in matches)) > 1
        unique_age = len(set((m['age_min'], m['age_max']) for m in matches)) > 1
        if unique_sex or unique_age:
            any_show_age = True
        
        # Sort: girls (2) before boys (1), youngest first, then fastest first
        def sort_key(m):
            sex_order = 0 if m['sex_code'] == 2 else 1
            age_lo = m['age_min'] if m['age_min'] else 0
            return (sex_order, age_lo, m['time_seconds'])
        matches.sort(key=sort_key)
        
        records = []
        for m in matches:
            qualifiers = []
            if unique_age:
                a_min = m['age_min']
                a_max = m['age_max']
                if a_min and a_max:
                    qualifiers.append("%d-%d" % (a_min, a_max))
                elif a_max:
                    qualifiers.append("%d & Under" % a_max)
                elif a_min:
                    qualifiers.append("%d & Over" % a_min)
                else:
                    qualifiers.append("Open")
            if unique_sex:
                qualifiers.append(sex_display.get(m['sex_code'], ''))
            
            records.append({
                'time': m['time'],
                'time_seconds': m['time_seconds'],
                'swimmer_name': m['swimmer_name'],
                'record_team': m['record_team'],
                'record_year': m['record_year'],
                'relay_names': m['relay_names'],
                'color_class': 'rec-color-%d' % (color_idx % 12),
                'qualifiers': ' '.join(qualifiers),
                'sex_code': m['sex_code'],
                'age_min': m['age_min'],
                'age_max': m['age_max'],
            })
            color_idx += 1
        
        all_set_results.append({
            'set_name': rec_file.header.record_set_name or '',
            'set_team_tag': rec_set['team_tag'],
            'records': records,
        })
    
    return all_set_results, any_show_age

def send_event_info():            
    update={}
    update["current_event"] = str(last_event_sent[0])
    update["current_heat"] = str(last_event_sent[1])
    update["event_name"] = event_info.get_event_name(last_event_sent[0])
    update["schedule_has_names"] = event_info.has_names
    qt_results, qt_show_age = _get_qualifying_times(last_event_sent[0])
    rec_set_results, rec_show_age = _get_matching_records(last_event_sent[0])
    show_age_codes = qt_show_age or rec_show_age
    update["qualifying_times"] = qt_results
    update["record_sets"] = rec_set_results
    
    for i in range(1,11):
        update["lane_name%i" % i] = event_info.get_display_string(last_event_sent[0], last_event_sent[1], i)
        update["lane_team%i" % i] = event_info.get_team_code(last_event_sent[0], last_event_sent[1], i)
        update["lane_age_code%i" % i] = event_info.get_age_code(last_event_sent[0], last_event_sent[1], i) if show_age_codes else ""
        seed = event_info.get_seed_time(last_event_sent[0], last_event_sent[1], i)
        update["lane_seed_time%i" % i] = seed if seed is not None else ""

    update["show_pr_tags"] = settings.get('show_pr_tags', True)
    update["show_confetti"] = settings.get('show_confetti', True)
    update["seed_time_label"] = settings.get('seed_time_label', 'Seed Time')
    update["race_state"] = race_fsm.state_name

    socketio.emit('update_scoreboard', update, namespace='/scoreboard')

def send_scores_info():
    update = {}
    update["score_home"] = team_scores['score_home']
    update["score_guest1"] = team_scores['score_guest1']
    update["score_guest2"] = team_scores['score_guest2']
    update["score_guest3"] = team_scores['score_guest3']
    update["race_state"] = race_fsm.state_name
    socketio.emit('update_scoreboard', update, namespace='/scoreboard')
            
@socketio.on('connect', namespace='/scoreboard')
def ws_scoreboard():
    print("Client connected to scoreboard namespace")
    global main_thread
    if(main_thread is None):
        main_thread = socketio.start_background_task(target=main_thread_worker)
        
    send_event_info()
    send_scores_info()

@socketio.on('next_heat', namespace='/scoreboard')
def ws_next_heat(d):
    global last_event_sent
    
    update={}
    
    event_list = list(event_info.events.keys())
    event_list.sort()

    try:
        event_tuple = event_list[event_list.index(last_event_sent)+1]
    except:
        event_tuple = event_list[0]
    
    last_event_sent = event_tuple
    race_fsm.notify_event_change()
    send_event_info()

@socketio.on('set_event_heat', namespace='/scoreboard')
def ws_set_event_heat(d):
    global last_event_sent
    event = int(d.get('event', last_event_sent[0]))
    heat = int(d.get('heat', last_event_sent[1]))
    last_event_sent = (event, heat)
    race_fsm.notify_event_change()
    send_event_info()

# ---------------------------------------------------------------------------
# Test simulation handlers
# ---------------------------------------------------------------------------
_sim_running = False  # Whether the sim clock is ticking

SIM_LANES = {
    1: {'name': 'Timmy Splash',  'team': 'HOME', 'age_code': '8B', 'seed': 17.50, 'time': 15.23, 'place': '1'},
    2: {'name': 'Sally Wave',    'team': 'AWAY', 'age_code': '8G', 'seed': 18.50, 'time': 16.89, 'place': '4'},
    3: {'name': 'Xander Annika', 'team': 'HOME', 'age_code': '7B', 'seed': 17.22, 'time': 17.54, 'place': '5'},
    4: {'name': 'Bobby Kick',    'team': 'HOME', 'age_code': '8B', 'seed': 19.00, 'time': 17.55, 'place': '5'},
    5: {'name': 'Lily Stroke',   'team': 'AWAY', 'age_code': '8G', 'seed': 17.20, 'time': 15.87, 'place': '2'},
    6: {'name': 'Max Dive',      'team': 'HOME', 'age_code': '8B', 'seed': 16.50, 'time': 18.50, 'place': '6'},
}

def _format_lane_time(seconds):
    """Format seconds as ' M:SS.HH' (8-char CTS-style time string)."""
    m = int(seconds) // 60
    s = seconds - m * 60
    if m > 0:
        return ' %d:%05.2f' % (m, s)
    else:
        return '   %05.2f' % s

@socketio.on('sim_load_event', namespace='/scoreboard')
def ws_sim_load_event(d=None):
    """Populate event_info, time_standards, and records with test data."""
    global last_event_sent, time_standards, swim_record_sets

    from hytek_st2_parser import St2File, St2Header, St2Event, CourseStandards, QualifyingTime, TimeStandard
    from hytek_rec_parser import RecFile, RecHeader, SwimRecord
    from datetime import date

    ev_num = 99
    heat_num = 1

    # --- Populate event_info ---
    event_info.event_names[ev_num] = "Mixed 8 & Under 25 Yard Freestyle"
    event_info.events[(ev_num, heat_num)] = {}
    event_info.teams[(ev_num, heat_num)] = {}
    event_info.age_codes[(ev_num, heat_num)] = {}
    event_info.seed_times[(ev_num, heat_num)] = {}

    for lane, data in SIM_LANES.items():
        event_info.events[(ev_num, heat_num)][lane] = data['name']
        event_info.teams[(ev_num, heat_num)][lane] = data['team']
        event_info.age_codes[(ev_num, heat_num)][lane] = data['age_code']
        event_info.seed_times[(ev_num, heat_num)][lane] = data['seed']

    # Lane 3 is clock channel — no swimmer
    #for d_key in (event_info.events, event_info.teams, event_info.age_codes):
    #    d_key.setdefault((ev_num, heat_num), {})[3] = ""
    #event_info.seed_times.setdefault((ev_num, heat_num), {})[3] = None

    event_info.event_meta[ev_num] = {
        'stroke_code': 1,
        'distance': 25,
        'relay': False,
        'age_min': None,
        'age_max': 8,
        'sex_codes': [1, 2],
        'is_mixed': True,
        'gender_age': GenderAge.BOY_S,
    }
    event_info.has_names = True

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

    time_standards = St2File(
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

    swim_record_sets = [{
        'rec_file': RecFile(
            header=RecHeader(course='SCY', course_code='Y', record_set_name='Pool Records',
                             software_version='SIM', record_count=2, export_date=date.today()),
            records=[
                _make_record(1, 15.50, 'Jimmy Fast', 'TEAM', 2024),
                _make_record(2, 16.00, 'Sally Swift', 'TEAM', 2023),
            ]
        ),
        'filename': 'sim_pool_records.rec',
        'team_tag': 'ALL',
        'set_id': 999,
    }]

    # --- Set scores ---
    team_scores['score_home'] = ' 142'
    team_scores['score_guest1'] = ' 138'
    team_scores['score_guest2'] = ''
    team_scores['score_guest3'] = ''

    # --- Trigger PreRace ---
    last_event_sent = (ev_num, heat_num)
    race_fsm.notify_event_change()
    send_event_info()
    send_scores_info()


@socketio.on('sim_step', namespace='/scoreboard')
def ws_sim_step(d):
    """Advance the simulation through race phases."""
    global channel_running, running_time, _sim_running

    step = d.get('step', '') if d else ''
    update = {}
    num_lanes = settings.get('num_lanes', 6)

    if step == 'start':
        _sim_running = True
        running_time = ' 0:00.00'
        update['running_time'] = running_time
        update['current_event'] = str(last_event_sent[0])
        update['current_heat'] = str(last_event_sent[1])
        for i in range(1, num_lanes + 1):
            channel_running[i - 1] = True
            update['lane_running%d' % i] = True
            update['lane_time%d' % i] = running_time
            update['lane_place%d' % i] = ' '

        race_fsm.evaluate_update(channel_running, update)
        update['race_state'] = race_fsm.state_name
        socketio.emit('update_scoreboard', update, namespace='/scoreboard')

        # Start background clock ticker
        socketio.start_background_task(_sim_clock_tick)

    elif step == 'finish':
        _sim_running = False
        update['current_event'] = str(last_event_sent[0])
        update['current_heat'] = str(last_event_sent[1])
        for i in range(1, num_lanes + 1):
            channel_running[i - 1] = False
            update['lane_running%d' % i] = False
            if i in SIM_LANES:
                update['lane_time%d' % i] = _format_lane_time(SIM_LANES[i]['time'])
                update['lane_place%d' % i] = SIM_LANES[i]['place']
            else:
                update['lane_time%d' % i] = '        '
                update['lane_place%d' % i] = ' '

        race_fsm.evaluate_update(channel_running, update)
        update['race_state'] = race_fsm.state_name
        socketio.emit('update_scoreboard', update, namespace='/scoreboard')

    elif step == 'clear':
        update['current_event'] = str(last_event_sent[0])
        update['current_heat'] = str(last_event_sent[1])
        for i in range(1, num_lanes + 1):
            update['lane_time%d' % i] = '        '
            update['lane_place%d' % i] = ' '

        race_fsm.evaluate_update(channel_running, update)
        update['race_state'] = race_fsm.state_name
        socketio.emit('update_scoreboard', update, namespace='/scoreboard')
        # Re-send event info so names/teams reappear on scoreboard
        send_event_info()

    elif step == 'blank':
        _sim_running = False
        update['current_event'] = '   '
        update['current_heat'] = '   '
        for i in range(1, num_lanes + 1):
            channel_running[i - 1] = False
            update['lane_running%d' % i] = False
        # Lane 3 still shows clock
        for i in range(1, num_lanes + 1):
            if i == 3:
                update['lane_time%d' % i] = '    5:22'
            else:
                update['lane_time%d' % i] = '        '
            update['lane_place%d' % i] = ' '

        # Clear scores
        for key in team_scores:
            team_scores[key] = ''
        update['score_home'] = ''
        update['score_guest1'] = ''
        update['score_guest2'] = ''
        update['score_guest3'] = ''

        race_fsm.evaluate_update(channel_running, update)
        update['race_state'] = race_fsm.state_name
        socketio.emit('update_scoreboard', update, namespace='/scoreboard')

    elif step == 'total_blank':
        _sim_running = False
        update['current_event'] = '   '
        update['current_heat'] = '   '
        for i in range(1, num_lanes + 1):
            channel_running[i - 1] = False
            update['lane_running%d' % i] = False
            update['lane_time%d' % i] = '        '
            update['lane_place%d' % i] = ' '

        for key in team_scores:
            team_scores[key] = ''
        update['score_home'] = ''
        update['score_guest1'] = ''
        update['score_guest2'] = ''
        update['score_guest3'] = ''

        race_fsm.evaluate_update(channel_running, update)
        update['race_state'] = race_fsm.state_name
        socketio.emit('update_scoreboard', update, namespace='/scoreboard')


def _sim_clock_tick():
    """Background task: increment running_time and emit to running lanes."""
    global running_time
    t = 0.0
    while _sim_running:
        socketio.sleep(0.1)
        if not _sim_running:
            break
        t += 0.1
        m = int(t) // 60
        s = t - m * 60
        running_time = ' %d:%05.2f' % (m, s)
        tick_update = {'running_time': running_time}
        num_lanes = settings.get('num_lanes', 6)
        for i in range(1, num_lanes + 1):
            if channel_running[i - 1]:
                tick_update['lane_time%d' % i] = running_time
        socketio.emit('update_scoreboard', tick_update, namespace='/scoreboard')

        
# Scoreboard Templates
@app.route('/overlay/<name>')
def route_overlay(name):
    overlay_name = "overlay/" + name + '.html'
    return flask.render_template(overlay_name, meet_title=settings['meet_title'], test_background='test' in flask.request.args.keys(), num_lanes=settings['num_lanes'])
    
@app.route('/web/<name>')
def route_web(name):
    web_name = "web/" + name + '.html'
    test_event = flask.request.args.get('event', None)
    test_heat = flask.request.args.get('heat', None)
    return flask.render_template(web_name, meet_title=settings['meet_title'], test_background='test' in flask.request.args.keys(), num_lanes=settings['num_lanes'], test_event=test_event, test_heat=test_heat, ad_url=settings['ad_url'], schedule_has_names=event_info.has_names, team_names=[('score_home', settings.get('team_home', '')), ('score_guest1', settings.get('team_guest1', '')), ('score_guest2', settings.get('team_guest2', '')), ('score_guest3', settings.get('team_guest3', ''))])

@app.route('/settings', methods=['POST', 'GET'])
@flask_login.login_required
def route_settings():
    global settings
    schedule_error = None
    standards_error = None
    records_error = None
    if flask.request.method == 'POST':
        modified = False
        
        # check if the post request has the file part
        if 'meet_schedule' in flask.request.files:
            file = flask.request.files['meet_schedule']
            # if user does not select file, browser also
            # submit a empty part without filename
            if file and file.filename and file.filename.endswith('.hy3'):
                try:
                    event_info.load_from_bytestream(file.stream)
                except Exception as e:
                    detail = str(e)
                    schedule_error = 'Failed to parse the schedule file'
                    if detail:
                        schedule_error += ': ' + detail
                else:
                    settings['event_info'] = event_info.to_object()
                    settings['schedule_filename'] = file.filename
                    send_event_info()
                    modified = True
        
        if 'time_standards_file' in flask.request.files:
            file = flask.request.files['time_standards_file']
            if file and file.filename and file.filename.endswith('.st2'):
                import pickle, base64
                import tempfile
                try:
                    with tempfile.NamedTemporaryFile(suffix='.st2', delete=False) as tmp:
                        tmp.write(file.stream.read())
                        tmp_path = tmp.name
                    global time_standards
                    time_standards = parse_st2_file(tmp_path)
                except Exception as e:
                    detail = str(e)
                    standards_error = 'Failed to parse the time standards file'
                    if detail:
                        standards_error += ': ' + detail
                else:
                    settings['time_standards'] = base64.b64encode(pickle.dumps(time_standards)).decode('ascii')
                    settings['standards_filename'] = file.filename
                    # Auto-populate desc overrides for new tags, preserve existing
                    new_tags = {s.tag for s in time_standards.header.standards}
                    overrides = settings.get('std_desc_overrides', {})
                    for std in time_standards.header.standards:
                        if std.tag not in overrides:
                            overrides[std.tag] = std.description
                    # Remove stale tags no longer in the file
                    settings['std_desc_overrides'] = {k: v for k, v in overrides.items() if k in new_tags}
                    modified = True
                finally:
                    try:
                        os.unlink(tmp_path)
                    except:
                        pass
        
        if 'records_file' in flask.request.files:
            file = flask.request.files['records_file']
            if file and file.filename and file.filename.endswith('.rec'):
                import pickle, base64
                import tempfile
                try:
                    with tempfile.NamedTemporaryFile(suffix='.rec', delete=False) as tmp:
                        tmp.write(file.stream.read())
                        tmp_path = tmp.name
                    global _next_rec_set_id
                    new_rec = parse_rec_file(tmp_path)
                except Exception as e:
                    detail = str(e)
                    records_error = 'Failed to parse the records file'
                    if detail:
                        records_error += ': ' + detail
                else:
                    swim_record_sets.append({
                        'rec_file': new_rec,
                        'filename': file.filename,
                        'team_tag': 'ALL',
                        'set_id': _next_rec_set_id,
                    })
                    _next_rec_set_id += 1
                    settings['swim_record_sets'] = base64.b64encode(pickle.dumps(swim_record_sets)).decode('ascii')
                    modified = True
                finally:
                    try:
                        os.unlink(tmp_path)
                    except:
                        pass
        
        # Handle record set team_tag dropdown updates
        for rec_set in swim_record_sets:
            form_key = 'rec_team_%d' % rec_set['set_id']
            if form_key in flask.request.form:
                new_tag = flask.request.form[form_key]
                if new_tag != rec_set['team_tag']:
                    rec_set['team_tag'] = new_tag
                    import pickle, base64
                    settings['swim_record_sets'] = base64.b64encode(pickle.dumps(swim_record_sets)).decode('ascii')
                    modified = True
        
        # Handle time standard description overrides
        if time_standards is not None:
            overrides = settings.get('std_desc_overrides', {})
            for std in time_standards.header.standards:
                form_key = 'std_desc_' + std.tag
                if form_key in flask.request.form:
                    new_desc = flask.request.form[form_key].strip()[:15]
                    if new_desc and new_desc != overrides.get(std.tag):
                        overrides[std.tag] = new_desc
                        modified = True
            settings['std_desc_overrides'] = overrides

        # Handle team tag auto-fill: if tag field is empty on Update, auto-fill from name
        for team_base in ['team_home', 'team_guest1', 'team_guest2', 'team_guest3']:
            tag_key = team_base + '_tag'
            if team_base in flask.request.form:
                name_val = flask.request.form.get(team_base, '').strip()
                tag_val = flask.request.form.get(tag_key, '').strip()
                if name_val and not tag_val:
                    # Auto-fill tag from name
                    tag_val = name_val[:5].upper()
                elif not name_val:
                    # Clear clears both
                    tag_val = ''
                tag_val = tag_val[:5]
                if settings.get(tag_key) != tag_val:
                    settings[tag_key] = tag_val
                    modified = True
        
        for k in settings.keys(): 
            if k in flask.request.form and settings[k]!=flask.request.form.get(k):
                if k == 'num_lanes':
                    val = int(flask.request.form.get(k))
                    if val != settings[k]:
                        settings[k] = val
                        modified = True
                elif k.endswith('_tag'):
                    pass  # Already handled above
                else:
                    val = flask.request.form.get(k)
                    if k.startswith('team_') and not k.endswith('_tag'):
                        val = val[:15]
                    settings[k]=val
                    modified = True
        
        # Handle checkbox fields (not present in form when unchecked)
        if 'show_pr_tags_form' in flask.request.form:
            new_val = 'show_pr_tags' in flask.request.form
            if settings.get('show_pr_tags') != new_val:
                settings['show_pr_tags'] = new_val
                modified = True

        if 'show_confetti_form' in flask.request.form:
            new_val = 'show_confetti' in flask.request.form
            if settings.get('show_confetti') != new_val:
                settings['show_confetti'] = new_val
                modified = True
        
        if modified:
            with open(settings_file, "wt") as f:
                json.dump(settings, f, sort_keys=True, indent=4)
                
    comm_port_list = [(port, "%s: %s" % (port,desc)) for port, desc, id in serial.tools.list_ports.comports()]
    if settings['serial_port'] not in [port for port,desc in comm_port_list]:
        comm_port_list.insert(0, (settings['serial_port'], settings['serial_port']))
        
    ad_url_list = []
    for dirpath, dir, file in os.walk(os.path.join("static", "ad")):
        ad_url_list.extend(file)
 
    schedule_loaded = bool(event_info.event_names)
    standards_loaded = time_standards is not None
    
    # Build record set info for template
    rec_set_info = []
    for rs in swim_record_sets:
        rec_set_info.append({
            'set_id': rs['set_id'],
            'filename': rs['filename'],
            'set_name': rs['rec_file'].header.record_set_name or '',
            'team_tag': rs['team_tag'],
        })
    
    # Build team tag options for record set dropdown
    team_tag_options = [('ALL', 'All')]
    for tag_key, name_key in [('team_home_tag', 'team_home'), ('team_guest1_tag', 'team_guest1'), ('team_guest2_tag', 'team_guest2'), ('team_guest3_tag', 'team_guest3')]:
        tag = settings.get(tag_key, '')
        name = settings.get(name_key, '')
        if tag:
            team_tag_options.append((tag, '%s (%s)' % (tag, name) if name else tag))
    
    return flask.render_template('settings.html', 
                meet_title=settings['meet_title'], 
                serial_port=settings['serial_port'],
                serial_port_list=comm_port_list,
                user_name=settings['username'],
                ad_url_list = ad_url_list,
                ad_url=settings['ad_url'],
                num_lanes=settings['num_lanes'],
                pool_course=settings.get('pool_course', 'SCY'),
                seed_time_label=settings.get('seed_time_label', 'Seed Time'),
                schedule_loaded=schedule_loaded,
                schedule_error=schedule_error,
                schedule_filename=settings.get('schedule_filename', ''),
                standards_loaded=standards_loaded,
                standards_error=standards_error,
                standards_filename=settings.get('standards_filename', ''),
                std_tag_info=[{'tag': s.tag, 'original_desc': s.description, 'desc_override': settings.get('std_desc_overrides', {}).get(s.tag, s.description)} for s in time_standards.header.standards] if time_standards else [],
                rec_set_info=rec_set_info,
                records_error=records_error,
                team_tag_options=team_tag_options,
                show_pr_tags=settings.get('show_pr_tags', True),
                show_confetti=settings.get('show_confetti', True),
                team_home=settings.get('team_home', ''),
                team_home_tag=settings.get('team_home_tag', ''),
                team_guest1=settings.get('team_guest1', ''),
                team_guest1_tag=settings.get('team_guest1_tag', ''),
                team_guest2=settings.get('team_guest2', ''),
                team_guest2_tag=settings.get('team_guest2_tag', ''),
                team_guest3=settings.get('team_guest3', ''),
                team_guest3_tag=settings.get('team_guest3_tag', ''),
                shutdown_nonce=_new_shutdown_nonce())
                
@app.route('/schedule_clear')
@flask_login.login_required
def route_schedule_clear():
    event_info.clear()
    settings['event_info'] = event_info.to_object()
    settings.pop('schedule_filename', None)
    with open(settings_file, "wt") as f:
        json.dump(settings, f, sort_keys=True, indent=4)
    return flask.redirect('/settings')

@app.route('/standards_clear')
@flask_login.login_required
def route_standards_clear():
    global time_standards
    time_standards = None
    settings.pop('time_standards', None)
    settings.pop('standards_filename', None)
    settings.pop('std_desc_overrides', None)
    with open(settings_file, "wt") as f:
        json.dump(settings, f, sort_keys=True, indent=4)
    return flask.redirect('/settings')

@app.route('/records_remove/<int:set_id>')
@flask_login.login_required
def route_records_remove(set_id):
    global swim_record_sets
    swim_record_sets = [s for s in swim_record_sets if s['set_id'] != set_id]
    import pickle, base64
    if swim_record_sets:
        settings['swim_record_sets'] = base64.b64encode(pickle.dumps(swim_record_sets)).decode('ascii')
    else:
        settings.pop('swim_record_sets', None)
    with open(settings_file, "wt") as f:
        json.dump(settings, f, sort_keys=True, indent=4)
    return flask.redirect('/settings')
                
_shutdown_nonces = []

def _new_shutdown_nonce():
    import secrets
    nonce = secrets.token_hex(16)
    _shutdown_nonces.append(nonce)
    if len(_shutdown_nonces) > 10:
        del _shutdown_nonces[:-10]
    return nonce

@app.route('/shutdown', methods=['POST'])
@flask_login.login_required
def route_shutdown():
    nonce = flask.request.form.get('nonce', '')
    if not nonce or nonce not in _shutdown_nonces:
        return 'Invalid request', 403
    _shutdown_nonces.clear()  # Invalidate all
    import threading
    def _exit():
        import time
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()
    return 'Server shutting down...', 200

@app.route('/combine_events')
@flask_login.login_required
def route_combine_events():
    event_heat = list(event_info.events_uncombined.keys())
    event_heat.sort()
    return flask.render_template('schedule_preview.html', 
                event_heat = event_heat, 
                event_names = event_info.event_names, 
                events = event_info.events_uncombined,
                combined = event_info.combined,
                show_combine_select = True)

@app.route('/schedule_preview', methods=["GET", "POST"])
@flask_login.login_required
def route_schedule_preview():
    if flask.request.method == 'POST':
        # Posted from combine events
        combined = {}
        for key, value in flask.request.form.items():
            if key.startswith('combine_') and value.strip():
                k = key.split('_')
                v = value.split(',')
                combined[(int(k[1]), int(k[2]))] = ( int(v[0]), int(v[1]) )
        event_info.combine_events(combined)
        settings['event_info'] = event_info.to_object()
        with open(settings_file, "wt") as f:
            json.dump(settings, f, sort_keys=True, indent=4)
    event_heat = list(event_info.events.keys())
    event_heat.sort()
    return flask.render_template('schedule_preview.html', 
                event_heat = event_heat, 
                event_names = event_info.event_names, 
                events = event_info.events,
                show_combine_select = False)

    
# somewhere to login
@app.route("/login", methods=["GET", "POST"])
def route_login():
    if flask.request.method == 'POST':
        if ((flask.request.form['username']==settings['username']) and
            (flask.request.form['password']==settings['password'])):        
            user = User(0)
            flask_login.login_user(user)
            return flask.redirect(flask.request.args.get("next"))
        else:
            return flask.abort(401)
    else:
        return flask.render_template('login.html')


# somewhere to logout
@app.route("/logout")
@flask_login.login_required
def route_logout():
    flask_login.logout_user()
    return flask.redirect('/')


# handle login failed
@app.errorhandler(401)
def page_not_found(e):
    return flask.render_template('login.html', login_failed=True)
    

def has_no_empty_params(rule):
    defaults = rule.defaults if rule.defaults is not None else ()
    arguments = rule.arguments if rule.arguments is not None else ()
    return len(defaults) >= len(arguments)

@app.route("/")
def route_site_map():
    links = []
    for rule in app.url_map.iter_rules():
        # Filter out rules we can't navigate to in a browser
        # and rules that require parameters
        if "GET" in rule.methods and has_no_empty_params(rule):
            url = flask.url_for(rule.endpoint, **(rule.defaults or {}))
            title = rule.endpoint.replace("_"," ")
            if title.startswith('route '):
                title = title[6:]
            if title not in ['login','logout','site map']:
                links.append((url, title.title()))
                
#   for file in glob.glob(os.path.join("templates", "overlay", "*.html")):
#        links.append( (file[file.startswith("templates") and len("templates"):].rsplit('.',1)[0], "Overlay " + os.path.basename(file).rsplit('.',1)[0]) )

    for file in glob.glob(os.path.join("templates", "web", "*.html")):
        links.append( (file[file.startswith("templates") and len("templates"):].rsplit('.',1)[0], "Web " + os.path.basename(file).rsplit('.',1)[0]) )

    # links is now a list of url, endpoint tuple
    links.sort(key=lambda a: '_' if (a[1] == 'Site List') else a[1])
    return flask.render_template('site_map.html', links=links)
    

@app.context_processor
def inject_ad():
    return dict(ad_url=settings['ad_url'])
    
# callback to reload the user object        
@login_manager.user_loader
def load_user(userid):
    return User(userid)
    
    
if __name__ == '__main__':
    import argparse
    
    load_settings()

    parser = argparse.ArgumentParser(description='Provide HTML rendering of Coloado Timing System data.')
    parser.add_argument('--port', '-p', action = 'store', default = '', 
        help='Serial port input from CTS scoreboard')
    parser.add_argument('--in', '-i', action = 'store', default = '', dest='in_file',
        help='Input file to use instead of serial port')
    parser.add_argument('--out', '-o', action = 'store', default = '', 
        help='Output file to dump data')
    parser.add_argument('--portlist', '-l', action = 'store_const', const=True, default = False,
        help='List of available serial ports')        
    parser.add_argument('--speed', '-s', action = 'store', default = 1.0, dest='in_speed',
        help='Speed to play input file at')
    parser.add_argument('--debug', '-d', action = 'store_const', const=True, default = False,
        help='Display debug info at console')
    args = parser.parse_args()

    try:
        if (args.portlist):
            print ("Available COM ports:")
            for port, desc, id in serial.tools.list_ports.comports():
                print (port, desc, id)
        if (args.port):
            settings['serial_port'] = args.port
        in_file = args.in_file
        out_file = args.out
        in_speed = float(args.in_speed)
        debug_console = args.debug
        ap.c()
        socketio.run(app, host="0.0.0.0")
    except:
        traceback.print_exc()
    finally:
        input('Press enter to continue...')
        
