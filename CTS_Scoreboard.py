#! /usr/bin/python3
import flask
import flask_login
import flask_socketio
import datetime
import traceback
import ctypes
import serial
import serial.tools.list_ports
import logging
import re
import time
import json
import os
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
from azure_relay import AzureRelayClient
from template_bundle import build_bundle
from qr_utils import build_meet_url, render_overlay_svg, substitute_qr_tokens, QR_TOKEN

DEBUG = False
#DEBUG = True
# Resolve settings paths against the script directory so that running the
# command from any cwd (e.g. `cts-scoreboard` from $HOME) still finds and
# writes the same files the systemd unit uses.
_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
settings_file = os.path.join(_REPO_DIR, 'settings.json')
# Azure-related settings live in a separate file that is git-ignored. Operator
# values (tenant ID, relay URLs, etc.) are environment-specific and should
# never be committed alongside the rest of the scoreboard configuration.
azure_settings_file = os.path.join(_REPO_DIR, 'azure_settings.json')
AZURE_SETTINGS_KEYS = (
    'azure_enabled',
    'azure_environment',
    'azure_tenant_id',
    'azure_client_id',
    'azure_audience',
    'azure_relay_url_preprod',
    'azure_public_url_preprod',
    'azure_relay_url_prod',
    'azure_public_url_prod',
    'azure_template_path',
    # Legacy single-env keys are migrated out, but listed here so any
    # straggler value gets routed to the right file on next save.
    'azure_relay_url',
    'azure_public_url',
)


def is_dev_mode():
    """True when running outside production (gunicorn).

    Controlled by env var SCOREBOARD_MODE; defaults to 'production'.
    Set SCOREBOARD_MODE=development for `flask run`, pytest, or VS Code launch.
    """
    return os.environ.get('SCOREBOARD_MODE', 'production').lower() == 'development'

settings = {
    'meet_title': '',
    'serial_port': 'COM1',
    'username': 'admin',
    'password': 'password',
    # Ad rotation: list of dicts {'filename': str, 'enabled': bool}.
    # Files live in static/ad/. Order in the list is rotation order.
    'ad_images': [],
    'ad_rotation_interval': 30,
    # Max dimension (px) for the longer edge after upload-time resize.
    'ad_max_dimension': 1920,
    'num_lanes': 6,
    'pool_course': 'SCY',
    'show_pr_tags': True,
    'show_confetti': True,
    'show_time_decorations': False,
    'seed_time_label': 'Seed Time',
    # Visual style for the public scoreboard. 'Classic' is the original
    # look; 'Modern' uses a contemporary CSS theme. Same template/JS.
    'ui_style': 'Classic',
    'message_pages': [{'text': '', 'align': 'left', 'enabled': False}],
    'message_overlay_enabled': False,
    'message_rotation_interval': 30,
    # Footer messages: list of dicts shown as a row at the bottom of the
    # scoreboard table. Each entry is gated by optional selectors (Gender,
    # Distance, Stroke, Age Group); see _select_footer_message() for the
    # matching rule. Schema per entry:
    #   {id: str, text: str, align: 'left'|'center'|'right', is_default: bool,
    #    genders: [str], distances: [int], strokes: [str], age_groups: [str],
    #    created_at: float}
    'footer_messages': [],
    'team_home': '',
    'team_home_tag': '',
    'team_guest1': '',
    'team_guest1_tag': '',
    'team_guest2': '',
    'team_guest2_tag': '',
    'team_guest3': '',
    'team_guest3_tag': '',
    'std_desc_overrides': {},
    # Azure relay (Phase 2)
    'azure_enabled': False,
    'azure_environment': 'preprod',  # 'preprod' or 'prod' — picks which URL pair to use.
    'azure_tenant_id': '',
    'azure_client_id': '',
    'azure_audience': '',
    'azure_relay_url_preprod': '',
    'azure_public_url_preprod': '',  # Public URL viewers see (often == relay).
    'azure_relay_url_prod': '',
    'azure_public_url_prod': '',
    # Legacy single-environment URLs; migrated into the *_preprod slot on load.
    'azure_relay_url': '',
    'azure_public_url': '',
    'azure_template_path': 'web/home',
    # QR (Phase 5)
    # qr_overlay_visibility: 'off' | 'between_races' | 'always'
    'qr_overlay_visibility': 'off',
    'qr_overlay_corner': 'top-right',
    # Legacy boolean migrated into qr_overlay_visibility on load.
    'qr_overlay_enabled': False,
    # Tracks whether the auto QR message page has already been injected for
    # the current sign-in (cleared on sign-out so the next sign-in re-injects
    # if the operator removed it).
    'qr_message_page_injected': False,
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

# Ad rotation state. Index into settings['ad_images']; clamped to a currently
# enabled entry by _update_ad_rotation().
_ad_rotation_index = 0
_ad_rotation_running = False

app = flask.Flask(__name__)
# config
app.config.update(
    DEBUG = False,
    SECRET_KEY = 'redacted-secret-key',
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024,
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
    """Store rendered HTML in the content cache. Returns the content key.

    Also forwards the rendered fragment to Azure (if the relay client is
    initialized) so the cloud-side viewer can serve the same HTML by name —
    the Pi's /api/<fragment> endpoints aren't reachable from Azure.
    """
    key = hashlib.sha256(html.encode('utf-8')).hexdigest()[:12]
    _content_cache[resource] = {'key': key, 'html': html}
    client = globals().get('azure_relay_client')
    if client is not None:
        client.forward_event('fragment', {'name': resource, 'key': key, 'html': html})
    return key

def _cache_get(resource):
    """Return (key, html) for a cached resource, or (None, None) if missing."""
    entry = _content_cache.get(resource)
    if entry:
        return entry['key'], entry['html']
    return None, None

def load_settings():
    global settings, time_standards, swim_record_sets, _next_rec_set_id
    _raw_settings_on_disk = {}
    try:
        with open(settings_file, "rt") as f:
            _raw_settings_on_disk = json.load(f)
            settings.update(_raw_settings_on_disk)
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
            save_settings()
        # Migrate old flat blank_message keys → message_pages array
        if 'blank_message' in settings and 'message_pages' not in settings:
            settings['message_pages'] = [{
                'text': settings.pop('blank_message', ''),
                'align': settings.pop('blank_message_align', 'left'),
                'enabled': bool(settings.pop('blank_message_visible', False)),
            }]
            settings['message_overlay_enabled'] = settings['message_pages'][0]['enabled']
            settings.setdefault('message_rotation_interval', 30)
            save_settings()
        # Drop the legacy single-image ``ad_url`` key. Ad images are now a list
        # of {filename, enabled} dicts in settings['ad_images']; the old value
        # is intentionally not migrated (operators re-select images via upload).
        if 'ad_url' in settings:
            settings.pop('ad_url', None)
            save_settings()
        # Normalise ad_images entries (older saves or hand-edits may produce
        # plain filename strings instead of the {'filename', 'enabled'} dict).
        raw_ads = settings.get('ad_images') or []
        norm_ads = []
        for entry in raw_ads:
            if isinstance(entry, str):
                norm_ads.append({'filename': entry, 'enabled': True})
            elif isinstance(entry, dict) and entry.get('filename'):
                norm_ads.append({
                    'filename': entry['filename'],
                    'enabled': bool(entry.get('enabled', True)),
                })
        if norm_ads != raw_ads:
            settings['ad_images'] = norm_ads
            save_settings()
        # Migrate legacy single-environment Azure URLs into the preprod slot.
        if settings.get('azure_relay_url') and not settings.get('azure_relay_url_preprod'):
            settings['azure_relay_url_preprod'] = settings['azure_relay_url']
        if settings.get('azure_public_url') and not settings.get('azure_public_url_preprod'):
            settings['azure_public_url_preprod'] = settings['azure_public_url']
        # Legacy keys are kept in defaults but cleared on disk after migration.
        if settings.get('azure_relay_url') or settings.get('azure_public_url'):
            settings['azure_relay_url'] = ''
            settings['azure_public_url'] = ''
            save_settings()
        # Migrate legacy boolean qr_overlay_enabled → qr_overlay_visibility.
        # Only migrate when the on-disk file actually carried the boolean
        # (i.e. an upgrade path); fresh installs use the default 'off'.
        if 'qr_overlay_enabled' in _raw_settings_on_disk and \
                'qr_overlay_visibility' not in _raw_settings_on_disk:
            settings['qr_overlay_visibility'] = (
                'always' if _raw_settings_on_disk.get('qr_overlay_enabled')
                else 'off'
            )
        # Legacy ui_style "Modern" → "Modern Dark" (the original Modern
        # theme was always dark; we now offer Light/Dark/Auto variants).
        if settings.get('ui_style') == 'Modern':
            settings['ui_style'] = 'Modern Dark'
    except: pass
    # Azure settings live in their own (git-ignored) file. Load it on top of
    # whatever defaults / migrated values are already in `settings`.
    azure_on_disk = {}
    try:
        with open(azure_settings_file, "rt") as f:
            azure_on_disk = json.load(f) or {}
        settings.update({k: v for k, v in azure_on_disk.items() if k in AZURE_SETTINGS_KEYS})
    except FileNotFoundError:
        pass
    except Exception:
        pass
    # If settings.json still carries azure_* entries (older installs),
    # split them out into azure_settings.json and strip from settings.json.
    leaked = [k for k in AZURE_SETTINGS_KEYS if k in _raw_settings_on_disk]
    if leaked:
        for k in AZURE_SETTINGS_KEYS:
            # Trust the value already in `settings` (azure_settings.json
            # wins over a stale duplicate in settings.json).
            azure_on_disk[k] = settings.get(k, '')
        try:
            save_azure_settings()
        except Exception:
            pass
        # Rewrite settings.json without the azure keys.
        cleaned = {k: v for k, v in _raw_settings_on_disk.items() if k not in AZURE_SETTINGS_KEYS}
        try:
            with open(settings_file, "wt") as f:
                json.dump(cleaned, f, sort_keys=True, indent=4)
        except Exception:
            pass


def save_azure_settings():
    """Persist only the AZURE_SETTINGS_KEYS subset to ``azure_settings_file``."""
    payload = {k: settings.get(k, '') for k in AZURE_SETTINGS_KEYS}
    with open(azure_settings_file, "wt") as f:
        json.dump(payload, f, sort_keys=True, indent=4)


def save_settings():
    """Persist the in-memory settings dict to ``settings_file``.

    Strips the ``AZURE_SETTINGS_KEYS`` subset before writing so the Azure
    relay credentials/URLs (which are sensitive and live in their own
    git-ignored ``azure_settings.json``) never leak back into the
    repo-tracked ``settings.json`` on a normal save.
    """
    public = {k: v for k, v in settings.items() if k not in AZURE_SETTINGS_KEYS}
    with open(settings_file, "wt") as f:
        json.dump(public, f, sort_keys=True, indent=4)

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
            broadcast_scoreboard(update)
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


# ---- Footer messages -------------------------------------------------------
# Footer-message selector vocab. Stored values match these labels exactly.
FOOTER_GENDER_LABELS = ['Female', 'Male', 'Mixed']
FOOTER_STROKE_LABELS = ['Freestyle', 'Backstroke', 'Breaststroke', 'Butterfly', 'Medley']
FOOTER_DISTANCE_VALUES = [25, 50, 100, 200, 400, 500, 800, 1000, 1500, 1650]
FOOTER_AGE_GROUP_LABELS = ['8-Under', '9-10', '11-12', '13-14', '15-18', 'Open']
# (min, max) inclusive; None means unbounded on that side.
FOOTER_AGE_GROUP_RANGES = {
    '8-Under': (None, 8),
    '9-10': (9, 10),
    '11-12': (11, 12),
    '13-14': (13, 14),
    '15-18': (15, 18),
    'Open': (None, None),
}
# st2 stroke_code -> label (matches FOOTER_STROKE_LABELS).
_FOOTER_STROKE_CODE_TO_LABEL = {
    1: 'Freestyle', 2: 'Backstroke', 3: 'Breaststroke',
    4: 'Butterfly', 5: 'Medley',
}


def _footer_event_gender_label(meta):
    """Map event_meta.sex_codes to a footer-vocab gender label, or None."""
    codes = meta.get('sex_codes') or []
    if len(codes) > 1:
        return 'Mixed'
    if codes == [1]:
        return 'Male'
    if codes == [2]:
        return 'Female'
    return None


def _footer_age_groups_overlap(group_label, event_age_min, event_age_max):
    """True if the selector's age range overlaps the event's age range."""
    grange = FOOTER_AGE_GROUP_RANGES.get(group_label)
    if grange is None:
        return False
    gmin, gmax = grange
    NEG = -10 ** 9
    POS = 10 ** 9
    emin = event_age_min if event_age_min is not None else NEG
    emax = event_age_max if event_age_max is not None else POS
    smin = gmin if gmin is not None else NEG
    smax = gmax if gmax is not None else POS
    return not (emax < smin or emin > smax)


def _event_matches_footer(msg, meta):
    """True if this footer message's non-empty selectors all match *meta*.

    A category with no selected values contributes True (matches anything).
    Within a category, OR across selected values. Across categories, AND.
    Default messages (``is_default``) are handled separately by the
    selector — this function only evaluates the explicit selector match.
    """
    if meta is None:
        return False
    sel = msg.get('genders') or []
    if sel:
        if _footer_event_gender_label(meta) not in sel:
            return False
    sel = msg.get('distances') or []
    if sel:
        d = meta.get('distance')
        try:
            d_int = int(d)
        except (TypeError, ValueError):
            return False
        if d_int not in [int(x) for x in sel if x is not None]:
            return False
    sel = msg.get('strokes') or []
    if sel:
        label = _FOOTER_STROKE_CODE_TO_LABEL.get(meta.get('stroke_code'))
        if label not in sel:
            return False
    sel = msg.get('age_groups') or []
    if sel:
        emin = meta.get('age_min')
        emax = meta.get('age_max')
        if not any(_footer_age_groups_overlap(g, emin, emax) for g in sel):
            return False
    return True


def _footer_specificity(msg):
    """Number of selector categories that have at least one value set."""
    n = 0
    for k in ('genders', 'distances', 'strokes', 'age_groups'):
        if msg.get(k):
            n += 1
    return n


def _select_footer_message(meta):
    """Pick the best matching footer message for the given event_meta.

    Rules:
      * Non-default messages are matched first. Highest specificity wins;
        ties broken by most recent ``created_at``.
      * If no non-default message matches, the most recently added default
        is used.
      * Returns None when there are no eligible messages.
    """
    msgs = settings.get('footer_messages') or []
    if not msgs:
        return None
    matches = []
    defaults = []
    for m in msgs:
        if m.get('is_default'):
            defaults.append(m)
        elif _event_matches_footer(m, meta):
            matches.append(m)
    if matches:
        matches.sort(
            key=lambda m: (_footer_specificity(m), m.get('created_at') or 0),
            reverse=True,
        )
        return matches[0]
    if defaults:
        defaults.sort(key=lambda m: m.get('created_at') or 0, reverse=True)
        return defaults[0]
    return None


def _render_footer_message_html(msg):
    """Render a footer message dict to HTML. Strips [[QR]] tokens."""
    if not msg:
        return ''
    text = (msg.get('text') or '').replace(QR_TOKEN, '')
    if not text.strip():
        return ''
    inner = _render_blank_message_html(text)
    align = msg.get('align', 'left')
    if align not in ('left', 'center', 'right'):
        align = 'left'
    return ('<div class="scoreboard-footer-message align-%s">%s</div>'
            % (align, inner))


def _current_event_meta():
    """Return the event_meta dict for the currently-selected event, or None."""
    try:
        ev = last_event_sent[0]
    except Exception:
        return None
    try:
        return event_info.event_meta.get(ev)
    except Exception:
        return None


def _render_and_cache_footer_message():
    """Render the active footer message and cache under 'footer_message'.

    Returns the content cache key. Empty cache (no matching message) still
    gets a stable key so clients can detect the no-message state.
    """
    msg = _select_footer_message(_current_event_meta())
    html = _render_footer_message_html(msg) if msg else ''
    return _cache_put('footer_message', html)


def broadcast_footer_message_refresh():
    """Re-render the active footer message and broadcast the new key.

    Called whenever the saved footer-messages list changes (add/remove) or
    when the live event/heat changes (so the selector logic re-evaluates).
    """
    key = _render_and_cache_footer_message()
    broadcast_scoreboard({'footer_message_key': key})


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
    relay_url, public_url = _active_azure_urls()
    qr_target = build_meet_url(
        public_base=public_url or relay_url,
        meet_id=getattr(azure_relay_client, 'meet_id', '') if 'azure_relay_client' in globals() else '',
    )
    keys = []
    for i, page in enumerate(pages):
        html = _render_blank_message_html(page.get('text', ''))
        html = substitute_qr_tokens(html, target_url=qr_target)
        key = _cache_put('message_page_%d' % i, html)
        keys.append(key)
    return keys


# --- Message rotation timer ---
_message_rotation_index = 0   # index into the full message_pages list (the currently shown page)
_message_rotation_running = False


def _enabled_page_indices():
    """Return list of indices of enabled message pages."""
    return [i for i, p in enumerate(settings.get('message_pages', [])) if p.get('enabled')]


_QR_AUTO_PAGE_TEXT = (
    "# View the scoreboard live\n"
    "Scan with your mobile device\n"
    "[[QR]]"
)


def _inject_qr_message_page() -> bool:
    """Append the auto QR message page once per fresh sign-in.

    The new page is disabled, center-aligned, and placed at the end of the
    rotation. Returns True if a page was actually appended (caller can then
    decide to broadcast/persist). Skipped when:
      * there are already 5 pages (the per-form maximum), or
      * any existing page already contains the ``[[QR]]`` token.
    """
    pages = list(settings.get('message_pages', []) or [])
    if len(pages) >= 5:
        return False
    for p in pages:
        if QR_TOKEN in (p.get('text') or ''):
            return False
    pages.append({
        'text': _QR_AUTO_PAGE_TEXT,
        'align': 'center',
        'enabled': False,
    })
    settings['message_pages'] = pages
    try:
        save_settings()
    except Exception:
        traceback.print_exc()
    return True


def broadcast_qr_overlay_refresh():
    """Re-render the overlay SVG + cached message pages, then broadcast.

    Called after any change that affects the QR target URL (Azure URL save,
    meet-id rotate/set, environment switch) or the overlay visibility/corner
    settings. Pushes a single ``update_scoreboard`` payload that connected
    browsers will use to swap the overlay content and re-evaluate gating.
    """
    relay_url, public_url = _active_azure_urls()
    qr_target = build_meet_url(
        public_base=public_url or relay_url,
        meet_id=getattr(azure_relay_client, 'meet_id', '') if 'azure_relay_client' in globals() else '',
    )
    qr_visibility = settings.get('qr_overlay_visibility', 'off')
    qr_corner = settings.get('qr_overlay_corner', 'top-right')
    overlay_svg = render_overlay_svg(qr_target) if qr_visibility != 'off' and qr_target else ''
    # Invalidate cached message-page HTML so [[QR]] tokens pick up the new URL.
    page_keys = _render_and_cache_message_pages()
    pages = settings.get('message_pages', [])
    broadcast_scoreboard({
        'qr_overlay_svg': overlay_svg,
        'qr_overlay_corner': qr_corner,
        'qr_overlay_visibility': qr_visibility,
        'message_pages': [
            {'text': p.get('text', ''), 'align': p.get('align', 'left'),
             'enabled': p.get('enabled', False),
             'key': page_keys[i] if i < len(page_keys) else None}
            for i, p in enumerate(pages)
        ],
    })


def broadcast_settings_changed():
    """Notify all connected scoreboard browsers that settings changed.

    Many settings (meet title, team names, num_lanes, ad image, display
    options, etc.) are baked into the rendered HTML at request time rather
    than driven by the live update_scoreboard stream, so the cleanest way
    to reflect them is a soft client reload.

    Locally: emit ``reload_clients`` on the /scoreboard namespace.
    Azure: re-push the latest meet_context (so the re-rendered page picks
    up the new values) and forward the same ``reload_clients`` event so
    Azure can fan it out to its connected viewers.
    """
    socketio.emit('reload_clients', {}, namespace='/scoreboard')
    client = globals().get('azure_relay_client')
    if client is not None:
        try:
            bundle = _azure_bundle_provider()
            if bundle is not None:
                client.forward_event('template_push', bundle)
        except Exception:
            traceback.print_exc()
        try:
            ctx = _azure_context_provider()
            if ctx is not None:
                client.forward_event('meet_context', ctx)
        except Exception:
            traceback.print_exc()
        client.forward_event('reload_clients', {})


def _on_azure_status(snap):
    """Status-subscriber callback: react to Azure connection-state changes.

    On the first transition into the connected state (per fresh sign-in),
    inject the auto QR message page and broadcast a refresh. Sign-out
    resets the latch so the next sign-in re-injects (only if absent).
    """
    state = snap.get('state')
    if state == 'connected':
        if not settings.get('qr_message_page_injected'):
            injected = _inject_qr_message_page()
            settings['qr_message_page_injected'] = True
            try:
                save_settings()
            except Exception:
                traceback.print_exc()
            if injected:
                _update_message_rotation()
        broadcast_qr_overlay_refresh()
    elif state == 'needs_auth':
        # Operator signed out — clear the latch so the next sign-in can
        # re-inject the auto QR page if it's no longer present.
        if settings.get('qr_message_page_injected'):
            settings['qr_message_page_injected'] = False
            try:
                save_settings()
            except Exception:
                traceback.print_exc()


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
        broadcast_scoreboard({
            'active_message_page': _message_rotation_index,
            'active_message_key': key,
        })


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


# --- Ad image rotation timer ---

def _enabled_ad_indices():
    """Return list of indices into settings['ad_images'] where enabled is True."""
    return [i for i, a in enumerate(settings.get('ad_images', []) or [])
            if a.get('enabled')]


def _start_ad_rotation():
    """Start the background ad rotation task if 2+ enabled images exist."""
    global _ad_rotation_running
    if _ad_rotation_running:
        return
    if len(_enabled_ad_indices()) < 2:
        return
    _ad_rotation_running = True
    socketio.start_background_task(_ad_rotation_tick)


def _stop_ad_rotation():
    """Signal the ad rotation timer to stop."""
    global _ad_rotation_running
    _ad_rotation_running = False


def _ad_rotation_tick():
    """Background task: rotate through enabled ads and broadcast changes."""
    global _ad_rotation_index, _ad_rotation_running
    while _ad_rotation_running:
        interval = settings.get('ad_rotation_interval', 30)
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            interval = 30
        if interval < 5 or interval > 60 or interval % 5 != 0:
            interval = 30
        socketio.sleep(interval)
        if not _ad_rotation_running:
            break
        enabled = _enabled_ad_indices()
        if len(enabled) < 2:
            _ad_rotation_running = False
            break
        try:
            cur_pos = enabled.index(_ad_rotation_index)
            next_pos = (cur_pos + 1) % len(enabled)
        except ValueError:
            next_pos = 0
        _ad_rotation_index = enabled[next_pos]
        broadcast_scoreboard({
            'active_ad_index': _ad_rotation_index,
            'ad_images': list(settings.get('ad_images', []) or []),
        })


def _update_ad_rotation():
    """Re-evaluate whether the ad rotation timer should be running.

    Call after any change to ad_images (add/remove/reorder/toggle) or to
    ad_rotation_interval. Clamps the active index to a currently-enabled
    entry and starts/stops the timer based on the enabled count.
    """
    global _ad_rotation_index
    enabled = _enabled_ad_indices()
    if not enabled:
        _ad_rotation_index = 0
    elif _ad_rotation_index not in enabled:
        _ad_rotation_index = enabled[0]
    if len(enabled) >= 2:
        _start_ad_rotation()
    else:
        _stop_ad_rotation()


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
    update["footer_message_key"] = _render_and_cache_footer_message()
    update["race_state"] = race_fsm.state_name

    broadcast_scoreboard(update)

def send_scores_info():
    update = {}
    update["score_home"] = team_scores['score_home']
    update["score_guest1"] = team_scores['score_guest1']
    update["score_guest2"] = team_scores['score_guest2']
    update["score_guest3"] = team_scores['score_guest3']
    update["race_state"] = race_fsm.state_name
    broadcast_scoreboard(update)

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
    broadcast_scoreboard(update)
            
@socketio.on('connect', namespace='/scoreboard')
def ws_scoreboard():
    print("Client connected to scoreboard namespace")
    global main_thread
    if(main_thread is None):
        main_thread = socketio.start_background_task(target=main_thread_worker)
        
    send_event_info()
    send_scores_info()
    # Replay snapshot to JUST this client so a fresh page load reflects the
    # current race (e.g. finished lane times when reloading mid-Finished).
    # send_event_info/send_scores_info above are broadcasts that updated the
    # snapshot too; replaying it now adds the per-race fields they omit.
    if _last_scoreboard_state:
        flask_socketio.emit('update_scoreboard', dict(_last_scoreboard_state))

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

def _build_render_context():
    """Build the dict of variables home.html needs.

    Shared between Flask's route_web (which adds dev/test-mode flags on top)
    and the Azure relay (which forwards a frozen copy after meet_open).
    """
    ev = last_event_sent[0]
    ht = last_event_sent[1]
    qt_results, qt_show_age = _get_qualifying_times(ev)
    rec_set_results, rec_show_age = _get_matching_records(ev)
    show_age_codes = qt_show_age or rec_show_age
    qt_key = _render_qualifying_html(qt_results, rec_set_results)
    page_keys = _render_and_cache_message_pages()
    footer_key = _render_and_cache_footer_message()
    _, qt_html = _cache_get('qualifying_info')
    _, footer_html = _cache_get('footer_message')
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

    relay_url, public_url = _active_azure_urls()
    qr_target = build_meet_url(
        public_base=public_url or relay_url,
        meet_id=getattr(azure_relay_client, 'meet_id', '') if 'azure_relay_client' in globals() else '',
    )
    qr_visibility = settings.get('qr_overlay_visibility', 'off')
    qr_overlay_svg = (
        render_overlay_svg(qr_target)
        if qr_visibility != 'off' and qr_target
        else ''
    )

    ad_images = list(settings.get('ad_images', []) or [])

    return dict(
        meet_title=settings['meet_title'],
        num_lanes=num_lanes,
        ad_images=ad_images,
        active_ad_index=_ad_rotation_index,
        ad_rotation_interval=settings.get('ad_rotation_interval', 30),
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
        initial_footer_key=footer_key,
        initial_footer_html=footer_html or '',
        initial_lanes=initial_lanes,
        initial_scores=team_scores,
        initial_race_state=race_fsm.state_name,
        initial_message_pages=initial_message_pages,
        initial_active_message_page=_message_rotation_index,
        initial_qr_overlay_svg=qr_overlay_svg,
        initial_qr_overlay_corner=settings.get('qr_overlay_corner', 'top-right'),
        initial_qr_overlay_visibility=qr_visibility,
        initial_settings={
            'show_pr_tags': settings.get('show_pr_tags', True),
            'show_confetti': settings.get('show_confetti', True),
            'show_time_decorations': settings.get('show_time_decorations', False),
            'seed_time_label': settings.get('seed_time_label', 'Seed Time'),
            'ui_style': settings.get('ui_style', 'Classic'),
            'message_overlay_enabled': settings.get('message_overlay_enabled', False),
            'message_rotation_interval': settings.get('message_rotation_interval', 30),
        },
    )


# Azure relay client (Phase 2). Constructed eagerly so settings/azure routes
# can introspect status, but not started unless settings.azure_enabled is True.
def _active_azure_urls():
    """Return (relay_url, public_url) for the currently selected environment.

    The Azure section of the settings stores per-environment URL pairs
    (preprod and prod). This helper centralises the lookup so callers don't
    need to know about the underlying key naming.
    """
    env = settings.get('azure_environment', 'preprod')
    if env == 'prod':
        return (settings.get('azure_relay_url_prod', '') or '',
                settings.get('azure_public_url_prod', '') or '')
    return (settings.get('azure_relay_url_preprod', '') or '',
            settings.get('azure_public_url_preprod', '') or '')


def _azure_bundle_provider():
    """Build the current template bundle for the relay to push to Azure.

    Returns a JSON-serializable dict, or None if bundling fails (the relay
    will treat None as 'no template change')."""
    try:
        rel = settings.get('azure_template_path', 'web/home') or 'web/home'
        if not rel.endswith('.html'):
            rel = rel + '.html'
        repo_root = os.path.dirname(os.path.abspath(__file__))
        # The template references the ad image via a runtime expression
        # (url_for('static', filename='ad/' + ad_url)), which the bundler
        # can't auto-discover. Pass the currently selected ad image as an
        # explicit extra so it lands in the cache served by Azure.
        # The template references ad images via runtime expressions
        # (url_for('static', filename='ad/' + name)), which the bundler can't
        # auto-discover. Pass every uploaded ad filename (enabled or not) as
        # an explicit extra so toggling an image on at runtime doesn't
        # require a fresh bundle push.
        extra_static = []
        for ad in (settings.get('ad_images') or []):
            name = (ad.get('filename') or '').strip() if isinstance(ad, dict) else ''
            if name:
                extra_static.append('ad/' + name)
        bundle = build_bundle(
            template_root=os.path.join(repo_root, 'templates'),
            static_root=os.path.join(repo_root, 'static'),
            template_relpath=rel,
            extra_static=extra_static,
        )
        return bundle.to_dict()
    except Exception:
        traceback.print_exc()
        return None


def _azure_context_provider():
    """Build the initial render context for the Azure relay (Phase 4).

    Mirrors the kwargs passed to render_template in route_web, minus dev-mode
    fields. Returns a JSON-serializable dict, or None if the snapshot can't
    be built. Runs inside a Flask app context because _build_render_context
    calls render_template, which requires `current_app`. The relay worker
    invokes this provider from a background thread that has no request or
    app context of its own."""
    try:
        with app.app_context():
            ctx = _build_render_context()
        # Force browser-friendly defaults: dev-only gates off; serving_context
        # marks the page as served via Azure.
        ctx['is_dev_mode'] = False
        ctx['serving_context'] = 'azure'
        ctx['test_background'] = False
        ctx['test_event'] = None
        ctx['test_heat'] = None
        # The Pi-only QR overlay is intentionally suppressed when serving via
        # Azure: the public viewer should never see the local overlay.
        ctx['initial_qr_overlay_svg'] = ''
        ctx['initial_qr_overlay_visibility'] = 'off'
        return ctx
    except Exception:
        traceback.print_exc()
        return None


azure_relay_client = AzureRelayClient(
    creds_file='azure_credentials.json',
    relay_url=_active_azure_urls()[0],
    bundle_provider=_azure_bundle_provider,
    context_provider=_azure_context_provider,
    host_team_name_provider=lambda: settings.get('team_home', '') or '',
)
# Worker thread is started later, after load_settings(), so the relay URL is
# populated first. See the block near the bottom of this module.


# Rolling snapshot of the most recent scoreboard fields, kept so that a
# late-connecting (or reloading) client can be brought up to date without
# waiting for the next serial broadcast. Per-race fields (lane_time/place/
# running + running_time) are dropped when current_event or current_heat
# changes so we never replay times from a prior race.
_last_scoreboard_state: dict = {}
_PER_RACE_KEY_PREFIXES = ('lane_time', 'lane_place', 'lane_running')


def _update_scoreboard_snapshot(update):
    new_event = update.get('current_event')
    new_heat = update.get('current_heat')
    drop_stale = (
        (new_event is not None and new_event != _last_scoreboard_state.get('current_event'))
        or (new_heat is not None and new_heat != _last_scoreboard_state.get('current_heat'))
    )
    if drop_stale:
        for k in list(_last_scoreboard_state.keys()):
            if k.startswith(_PER_RACE_KEY_PREFIXES) or k == 'running_time':
                del _last_scoreboard_state[k]
    _last_scoreboard_state.update(update)


def broadcast_scoreboard(update):
    """Emit an update_scoreboard payload to local browsers AND forward to Azure.

    The local emit happens unconditionally on the /scoreboard namespace. The
    Azure forward is also unconditional and fire-and-forget: it enqueues the
    payload on the relay's background thread queue. If the relay isn't
    signed in or isn't connected, the queue silently absorbs events (bounded
    at 1000; the most recent ones win when full) and they're delivered once
    the connection is established. Sign in via the settings UI and the
    relay's ``meet_open`` handshake is enough — no separate "enabled"
    toggle.
    """
    _update_scoreboard_snapshot(update)
    socketio.emit('update_scoreboard', update, namespace='/scoreboard')
    # forward_event() is non-blocking and thread-safe; safe to call even
    # when the relay is in NEEDS_AUTH or DISCONNECTED.
    azure_relay_client.forward_event('update_scoreboard', dict(update))


# Register settings/admin routes
settings_routes.register(app, sys.modules[__name__])


# REST API endpoints for cached HTML fragments. URLs are content-addressed:
# the 12-hex SHA-256 key from _cache_put is part of the path, so responses
# are immutable and any mismatch (Pi has moved on to a newer key) returns
# 404 \u2014 the browser falls back to its inline initial render and re-fetches
# on the next update_scoreboard event.
def _serve_cached_fragment(resource, key):
    cur_key, html = _cache_get(resource)
    if cur_key is None or cur_key != key:
        return flask.Response('', status=404)
    resp = flask.Response(html, status=200, content_type='text/html; charset=utf-8')
    resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    resp.headers['ETag'] = '"' + key + '"'
    return resp

@app.route('/api/qualifying-info/<key>')
def api_qualifying_info(key):
    return _serve_cached_fragment('qualifying_info', key)

@app.route('/api/message-page/<int:index>/<key>')
def api_message_page(index, key):
    return _serve_cached_fragment('message_page_%d' % index, key)

@app.route('/api/footer-message/<key>')
def api_footer_message(key):
    return _serve_cached_fragment('footer_message', key)


@app.after_request
def _long_cache_ad_files(resp):
    # Ad filenames are UUID-based on upload (settings_routes.py), so a
    # given URL always maps to the same bytes for the life of the file.
    # Mark them immutable so browsers don't keep revalidating each
    # rotation step. The settings UI's delete path makes the URL go
    # away rather than reusing it with new content.
    path = flask.request.path or ''
    if path.startswith('/static/ad/'):
        resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return resp


@app.errorhandler(413)
def _ad_upload_too_large(_e):
    # Flask aborts large uploads before our route runs. Return a small
    # message with a back link rather than the default HTML error page.
    return (
        '<!doctype html><meta charset="utf-8">'
        '<title>Upload too large</title>'
        '<h1>Upload too large</h1>'
        '<p>Ad uploads are limited to 5 MB per file. '
        '<a href="/settings">Back to Settings</a></p>',
        413,
    )


@app.route("/test")
@flask_login.login_required
def route_test():
    """Local-only test/simulator page. Disabled in production."""
    if not is_dev_mode():
        return flask.abort(404)
    return flask.render_template('test.html', meet_title=settings.get('meet_title', ''))


# Scoreboard Templates    
@app.route('/web/<name>')
def route_web(name):
    web_name = "web/" + name + '.html'
    test_event = flask.request.args.get('event', None)
    test_heat = flask.request.args.get('heat', None)

    ctx = _build_render_context()
    return flask.render_template(web_name,
        test_background='test' in flask.request.args.keys(),
        is_dev_mode=is_dev_mode(),
        serving_context='pi',
        test_event=test_event,
        test_heat=test_heat,
        **ctx,
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
            # Hide JSON/API endpoints that aren't meaningful site-map pages.
            # Azure relay endpoints (/azure/status, /azure/config, ...) are
            # AJAX targets used from the Settings page, not destinations.
            if url.startswith('/azure/'):
                continue
            # Same for the Wi-Fi JSON endpoints and the qualifying-info API.
            if url.startswith('/wifi/') or url.startswith('/api/'):
                continue
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
    return dict(
        ad_images=list(settings.get('ad_images', []) or []),
        active_ad_index=_ad_rotation_index,
        ad_rotation_interval=settings.get('ad_rotation_interval', 30),
    )
    
# callback to reload the user object        
@login_manager.user_loader
def load_user(userid):
    return User(userid)
    
    
# Module-level initialization: load settings and start rotation timer so the
# app is ready whether launched via ``python CTS_Scoreboard.py`` (dev) or
# imported by gunicorn (production).
load_settings()
# Configure logging so azure_relay (and other module loggers) actually emit
# to stderr. Honour SCOREBOARD_MODE=development for verbose DEBUG-level logs.
_log_level = (
    logging.DEBUG if os.environ.get('SCOREBOARD_MODE', '').lower() == 'development'
    else logging.INFO
)
logging.basicConfig(
    level=_log_level,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
# socketio + engineio are extremely chatty at DEBUG; cap them at INFO.
logging.getLogger('engineio').setLevel(logging.INFO)
logging.getLogger('socketio').setLevel(logging.INFO)
# geventwebsocket logs every poll request as 'Initializing WebSocket' /
# 'Validating WebSocket request' at DEBUG. Cap at INFO so polling endpoints
# (/azure/status, /wifi/status) don't drown out real signal.
logging.getLogger('geventwebsocket').setLevel(logging.INFO)
logging.getLogger('geventwebsocket.handler').setLevel(logging.INFO)
# azure_relay was constructed above settings load, so its relay_url
# was empty. Push the now-loaded URL into it.
azure_relay_client.update_relay_url(_active_azure_urls()[0])
# React to relay state transitions for QR-page injection + overlay refresh.
azure_relay_client.subscribe_status(_on_azure_status)
# Start the worker thread whenever we have credentials. The worker just sits
# in needs_auth/disconnected if there's nothing to do, but having it alive
# means the Reconnect button and post-sign-in flow can work without a server
# restart. Once signed in and connected, broadcast_scoreboard's forward to
# Azure is automatic — there is no separate enable toggle.
if azure_relay_client.meet_id:
    azure_relay_client.start()
_update_message_rotation()
_update_ad_rotation()


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
        
