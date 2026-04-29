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
import argparse
import hashlib
import sim
import wifi_manager
import settings_routes

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
    'show_time_decorations': False,
    'seed_time_label': 'Seed Time',
    'message_pages': [{'text': '', 'align': 'left', 'enabled': False}],
    'message_overlay_enabled': False,
    'message_rotation_interval': 30,
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

# Content cache for server-rendered HTML fragments.
# Structure: { resource_name: { 'key': str, 'html': str } }
_content_cache = {}

def _cache_put(resource, html):
    """Store rendered HTML in the content cache. Returns the content key."""
    key = hashlib.sha256(html.encode('utf-8')).hexdigest()[:12]
    _content_cache[resource] = {'key': key, 'html': html}
    return key

def _cache_get(resource):
    """Return (key, html) for a cached resource, or (None, None) if missing."""
    entry = _content_cache.get(resource)
    if entry:
        return entry['key'], entry['html']
    return None, None

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
        # Migrate old flat blank_message keys → message_pages array
        if 'blank_message' in settings and 'message_pages' not in settings:
            settings['message_pages'] = [{
                'text': settings.pop('blank_message', ''),
                'align': settings.pop('blank_message_align', 'left'),
                'enabled': bool(settings.pop('blank_message_visible', False)),
            }]
            settings['message_overlay_enabled'] = settings['message_pages'][0]['enabled']
            settings.setdefault('message_rotation_interval', 30)
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

    # Sort: girls/women (sex_code 2) before boys/men (1), youngest age first,
    # then fastest first. Keeps standards-qualifier group ordering consistent
    # with the records display.
    def _std_sort_key(m):
        sex_order = 0 if m['sex_code'] == 2 else 1
        age_lo = m['age_min'] if m['age_min'] else 0
        return (sex_order, age_lo, m['time_seconds'])
    matches.sort(key=_std_sort_key)
    
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

    # First pass: collect matches for every set so we can compute qualifier
    # visibility (unique_sex / unique_age) globally across sets. This way a
    # set that only has one sex still gets the sex qualifier shown whenever
    # any other set needs the sex distinction.
    per_set_matches = []
    for rec_set in swim_record_sets:
        rec_file = rec_set['rec_file']

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

        if matches:
            per_set_matches.append((rec_set, rec_file, matches))

    # Global qualifier flags: if any set would differentiate by sex/age, then
    # every set should display that qualifier for visual consistency.
    all_matches = [m for _, _, ms in per_set_matches for m in ms]
    unique_sex = len(set(m['sex_code'] for m in all_matches)) > 1
    unique_age = len(set((m['age_min'], m['age_max']) for m in all_matches)) > 1
    if unique_sex or unique_age:
        any_show_age = True

    for rec_set, rec_file, matches in per_set_matches:
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


def _render_blank_message_html(text):
    """Render a simple markdown subset to HTML for the blank-state message.

    Supports: # .. #### headers, **bold**, *italic*, _underline_ (extension),
    ~~strike~~, `code`, ordered (1.) and unordered (-,*) lists.
    Input is HTML-escaped first.
    """
    if text is None:
        return ''
    # HTML-escape
    esc = str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    lines = esc.split('\n')
    out = []
    list_type = None  # 'ul' | 'ol' | None

    def close_list():
        nonlocal list_type
        if list_type:
            out.append('</' + list_type + '>')
            list_type = None

    def inline(s):
        # Protect inline code spans first.
        codes = []
        def _code_repl(m):
            codes.append(m.group(1))
            return '\x01' + str(len(codes) - 1) + '\x01'
        s = re.sub(r'`([^`\n]+)`', _code_repl, s)
        # Bold (**...**) before italic (*...*).
        s = re.sub(r'\*\*([^\*\n]+)\*\*', r'<strong>\1</strong>', s)
        # Strikethrough.
        s = re.sub(r'~~([^~\n]+)~~', r'<s>\1</s>', s)
        # Italic: single * not adjacent to another *.
        s = re.sub(r'(^|[^\*])\*([^\*\n]+)\*(?!\*)', r'\1<em>\2</em>', s)
        # Underline (project extension): _text_.
        s = re.sub(r'(^|[^_])_([^_\n]+)_(?!_)', r'\1<u>\2</u>', s)
        # Restore code spans.
        def _code_restore(m):
            return '<code>' + codes[int(m.group(1))] + '</code>'
        s = re.sub(r'\x01(\d+)\x01', _code_restore, s)
        return s

    for ln in lines:
        m = re.match(r'^\s*(#{1,4})\s+(.*)$', ln)
        if m:
            close_list()
            lvl = len(m.group(1))
            out.append('<h%d>%s</h%d>' % (lvl, inline(m.group(2)), lvl))
            continue
        m = re.match(r'^\s*\d+\.\s+(.*)$', ln)
        if m:
            if list_type != 'ol':
                close_list()
                out.append('<ol>')
                list_type = 'ol'
            out.append('<li>' + inline(m.group(1)) + '</li>')
            continue
        m = re.match(r'^\s*[-\*]\s+(.*)$', ln)
        if m:
            if list_type != 'ul':
                close_list()
                out.append('<ul>')
                list_type = 'ul'
            out.append('<li>' + inline(m.group(1)) + '</li>')
            continue
        close_list()
        if len(ln) == 0:
            out.append('<div class="md-blank"></div>')
        else:
            out.append(inline(ln) + '<br>')

    close_list()
    # Trim trailing <br>
    while out and out[-1] == '<br>':
        out.pop()
    return ''.join(out)


def _render_qualifying_html(qt_groups, rec_set_list):
    """Render qualifying times + records into an HTML fragment and cache it.

    Returns the content key (hash).
    """
    html = flask.render_template('partials/_qualifying_info.html',
                                 qt_groups=qt_groups,
                                 rec_set_list=rec_set_list)
    return _cache_put('qualifying_info', html)


def _render_and_cache_message_pages():
    """Render all message pages to HTML and cache them.

    Returns a list of content keys (one per page).
    """
    pages = settings.get('message_pages', [])
    keys = []
    for i, page in enumerate(pages):
        html = _render_blank_message_html(page.get('text', ''))
        key = _cache_put('message_page_%d' % i, html)
        keys.append(key)
    return keys


# --- Message rotation timer ---
_message_rotation_index = 0   # index into the full message_pages list (the currently shown page)
_message_rotation_running = False


def _enabled_page_indices():
    """Return list of indices of enabled message pages."""
    return [i for i, p in enumerate(settings.get('message_pages', [])) if p.get('enabled')]


def _start_message_rotation():
    """Start the background rotation timer if 2+ pages are enabled and overlay is on."""
    global _message_rotation_running
    if _message_rotation_running:
        return
    enabled = _enabled_page_indices()
    if len(enabled) < 2 or not settings.get('message_overlay_enabled', False):
        return
    _message_rotation_running = True
    socketio.start_background_task(_message_rotation_tick)


def _stop_message_rotation():
    """Signal the rotation timer to stop."""
    global _message_rotation_running
    _message_rotation_running = False


def _message_rotation_tick():
    """Background task: rotate through enabled pages and broadcast changes."""
    global _message_rotation_index, _message_rotation_running
    while _message_rotation_running:
        interval = settings.get('message_rotation_interval', 30)
        socketio.sleep(interval)
        if not _message_rotation_running:
            break
        enabled = _enabled_page_indices()
        if len(enabled) < 2:
            _message_rotation_running = False
            break
        # Advance to next enabled page
        try:
            cur_pos = enabled.index(_message_rotation_index)
            next_pos = (cur_pos + 1) % len(enabled)
        except ValueError:
            next_pos = 0
        _message_rotation_index = enabled[next_pos]
        # Broadcast page change
        page_keys = _render_and_cache_message_pages()
        key = page_keys[_message_rotation_index] if _message_rotation_index < len(page_keys) else None
        socketio.emit('update_scoreboard', {
            'active_message_page': _message_rotation_index,
            'active_message_key': key,
        }, namespace='/scoreboard')


def _update_message_rotation():
    """Re-evaluate whether the rotation timer should be running.
    Call after any change to message_pages, overlay_enabled, or interval."""
    global _message_rotation_index
    enabled = _enabled_page_indices()
    if not enabled:
        _message_rotation_index = 0
    elif _message_rotation_index not in enabled:
        _message_rotation_index = enabled[0]
    if len(enabled) >= 2 and settings.get('message_overlay_enabled', False):
        _start_message_rotation()
    else:
        _stop_message_rotation()


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
    update["qualifying_times_key"] = _render_qualifying_html(qt_results, rec_set_results)
    
    for i in range(1,11):
        update["lane_name%i" % i] = event_info.get_display_string(last_event_sent[0], last_event_sent[1], i)
        update["lane_team%i" % i] = event_info.get_team_code(last_event_sent[0], last_event_sent[1], i)
        update["lane_age_code%i" % i] = event_info.get_age_code(last_event_sent[0], last_event_sent[1], i) if show_age_codes else ""
        seed = event_info.get_seed_time(last_event_sent[0], last_event_sent[1], i)
        update["lane_seed_time%i" % i] = seed if seed is not None else ""

    update["show_pr_tags"] = settings.get('show_pr_tags', True)
    update["show_confetti"] = settings.get('show_confetti', True)
    update["show_time_decorations"] = settings.get('show_time_decorations', False)
    update["seed_time_label"] = settings.get('seed_time_label', 'Seed Time')
    page_keys = _render_and_cache_message_pages()
    pages = settings.get('message_pages', [])
    update["message_overlay_enabled"] = settings.get('message_overlay_enabled', False)
    update["message_pages"] = [
        {'text': p.get('text', ''), 'align': p.get('align', 'left'),
         'enabled': p.get('enabled', False), 'key': page_keys[i] if i < len(page_keys) else None}
        for i, p in enumerate(pages)
    ]
    update["message_rotation_interval"] = settings.get('message_rotation_interval', 30)
    update["active_message_page"] = _message_rotation_index
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

def send_message_overlay_state():
    """Broadcast message overlay state to all scoreboard clients."""
    page_keys = _render_and_cache_message_pages()
    pages = settings.get('message_pages', [])
    update = {
        'message_overlay_enabled': settings.get('message_overlay_enabled', False),
        'message_pages': [
            {'text': p.get('text', ''), 'align': p.get('align', 'left'),
             'enabled': p.get('enabled', False), 'key': page_keys[i] if i < len(page_keys) else None}
            for i, p in enumerate(pages)
        ],
        'message_rotation_interval': settings.get('message_rotation_interval', 30),
        'active_message_page': _message_rotation_index,
    }
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

# Register simulation handlers
import sys
sim.register(socketio, sys.modules[__name__])

# Register settings/admin routes
settings_routes.register(app, sys.modules[__name__])


# REST API endpoints for cached HTML fragments
@app.route('/api/qualifying-info')
def api_qualifying_info():
    key, html = _cache_get('qualifying_info')
    if key is None:
        return flask.Response('', status=200, content_type='text/html; charset=utf-8')
    etag = '"' + key + '"'
    if flask.request.headers.get('If-None-Match') == etag:
        return flask.Response('', status=304)
    resp = flask.Response(html, status=200, content_type='text/html; charset=utf-8')
    resp.headers['ETag'] = etag
    resp.headers['Cache-Control'] = 'public, max-age=60'
    return resp

@app.route('/api/message-page/<int:index>')
def api_message_page(index):
    resource = 'message_page_%d' % index
    key, html = _cache_get(resource)
    if key is None:
        return flask.Response('', status=200, content_type='text/html; charset=utf-8')
    etag = '"' + key + '"'
    if flask.request.headers.get('If-None-Match') == etag:
        return flask.Response('', status=304)
    resp = flask.Response(html, status=200, content_type='text/html; charset=utf-8')
    resp.headers['ETag'] = etag
    resp.headers['Cache-Control'] = 'public, max-age=60'
    return resp

        
# Scoreboard Templates    
@app.route('/web/<name>')
def route_web(name):
    web_name = "web/" + name + '.html'
    test_event = flask.request.args.get('event', None)
    test_heat = flask.request.args.get('heat', None)

    # Pre-populate initial page state so content appears immediately
    ev = last_event_sent[0]
    ht = last_event_sent[1]
    qt_results, qt_show_age = _get_qualifying_times(ev)
    rec_set_results, rec_show_age = _get_matching_records(ev)
    show_age_codes = qt_show_age or rec_show_age
    qt_key = _render_qualifying_html(qt_results, rec_set_results)
    page_keys = _render_and_cache_message_pages()
    _, qt_html = _cache_get('qualifying_info')
    num_lanes = settings['num_lanes']

    initial_lanes = {}
    for i in range(1, num_lanes + 1):
        initial_lanes[i] = {
            'name': event_info.get_display_string(ev, ht, i),
            'team': event_info.get_team_code(ev, ht, i),
            'age_code': event_info.get_age_code(ev, ht, i) if show_age_codes else '',
            'seed_time': event_info.get_seed_time(ev, ht, i),
        }

    pages = settings.get('message_pages', [])
    initial_message_pages = [
        {'text': p.get('text', ''), 'align': p.get('align', 'left'),
         'enabled': p.get('enabled', False), 'key': page_keys[i] if i < len(page_keys) else None}
        for i, p in enumerate(pages)
    ]

    return flask.render_template(web_name,
        meet_title=settings['meet_title'],
        test_background='test' in flask.request.args.keys(),
        num_lanes=num_lanes,
        test_event=test_event,
        test_heat=test_heat,
        ad_url=settings['ad_url'],
        schedule_has_names=event_info.has_names,
        team_names=[
            ('score_home', settings.get('team_home', '')),
            ('score_guest1', settings.get('team_guest1', '')),
            ('score_guest2', settings.get('team_guest2', '')),
            ('score_guest3', settings.get('team_guest3', '')),
        ],
        initial_event=str(ev),
        initial_heat=str(ht),
        initial_event_name=event_info.get_event_name(ev),
        initial_qt_groups=qt_results,
        initial_rec_sets=rec_set_results,
        initial_qt_key=qt_key,
        initial_qt_html=qt_html or '',
        initial_lanes=initial_lanes,
        initial_scores=team_scores,
        initial_race_state=race_fsm.state_name,
        initial_message_pages=initial_message_pages,
        initial_active_message_page=_message_rotation_index,
        initial_settings={
            'show_pr_tags': settings.get('show_pr_tags', True),
            'show_confetti': settings.get('show_confetti', True),
            'show_time_decorations': settings.get('show_time_decorations', False),
            'seed_time_label': settings.get('seed_time_label', 'Seed Time'),
            'message_overlay_enabled': settings.get('message_overlay_enabled', False),
            'message_rotation_interval': settings.get('message_rotation_interval', 30),
        },
    )

def has_no_empty_params(rule):
    defaults = rule.defaults if rule.defaults is not None else ()
    arguments = rule.arguments if rule.arguments is not None else ()
    return len(defaults) >= len(arguments)

@app.route("/")
def route_site_map():
    # Collect all browsable routes (keyed by endpoint) so we can group them
    all_links = {}
    for rule in app.url_map.iter_rules():
        if "GET" in rule.methods and has_no_empty_params(rule):
            url = flask.url_for(rule.endpoint, **(rule.defaults or {}))
            title = rule.endpoint.replace("_", " ")
            if title.startswith('route '):
                title = title[6:]
            if title in ['login', 'logout', 'site map']:
                continue
            # Hide these action-style endpoints from the site map
            if title in ['schedule clear', 'standards clear']:
                continue
            all_links[title] = (url, title.title())

    # Discover web/ scoreboard templates
    web_links = {}
    for file in glob.glob(os.path.join("templates", "web", "*.html")):
        name = os.path.basename(file).rsplit('.', 1)[0]
        url = file[file.startswith("templates") and len("templates"):].rsplit('.', 1)[0]
        web_links[name] = (url, "Web " + name)

    def _pop(d, key):
        return d.pop(key, None)

    sections = []

    # View Scoreboard: Web Home first, then any other web templates
    view_items = []
    home = _pop(web_links, 'home')
    if home:
        view_items.append(home)
    for key in sorted(web_links.keys()):
        view_items.append(web_links[key])
    if view_items:
        sections.append(("View Scoreboard", view_items))

    # Settings section: Settings, Combine Events, Schedule Preview (in that order)
    settings_items = []
    for key in ['settings', 'combine events', 'schedule preview']:
        link = _pop(all_links, key)
        if link:
            settings_items.append(link)
    if settings_items:
        sections.append(("Settings", settings_items))

    # Everything else falls into "Other" so nothing disappears accidentally
    other_items = [all_links[k] for k in sorted(all_links.keys())]
    if other_items:
        sections.append(("Other", other_items))

    return flask.render_template('site_map.html', sections=sections)
    

@app.context_processor
def inject_ad():
    return dict(ad_url=settings['ad_url'])
    
# callback to reload the user object        
@login_manager.user_loader
def load_user(userid):
    return User(userid)
    
    
# Module-level initialization: load settings and start rotation timer so the
# app is ready whether launched via ``python CTS_Scoreboard.py`` (dev) or
# imported by gunicorn (production).
load_settings()
_update_message_rotation()


def main():
    global in_file, out_file, in_speed, debug_console

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


if __name__ == '__main__':
    main()
        
