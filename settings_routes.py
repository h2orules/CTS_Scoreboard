"""Settings page routes for the CTS Scoreboard.

Call ``register(flask_app, app_module)`` once after the Flask app instance
and all referenced globals exist.  This wires up ``/settings``, ``/wifi/*``,
``/schedule_clear``, ``/standards_clear``, ``/records_remove``, ``/shutdown``,
``/combine_events``, ``/schedule_preview``, ``/login``, and ``/logout``.
"""

import flask
import flask_login
import json
import os
import os.path
import re
import serial
import serial.tools.list_ports

import wifi_manager
from hytek_st2_parser import parse_st2_file
from hytek_rec_parser import parse_rec_file

# ---------------------------------------------------------------------------
# Module-level refs — set by register()
# ---------------------------------------------------------------------------
_flask_app = None   # Flask app instance
_app = None         # reference to CTS_Scoreboard module

# ---------------------------------------------------------------------------
# Shutdown nonce management
# ---------------------------------------------------------------------------
_shutdown_nonces = []

def _new_shutdown_nonce():
    import secrets
    nonce = secrets.token_hex(16)
    _shutdown_nonces.append(nonce)
    if len(_shutdown_nonces) > 10:
        del _shutdown_nonces[:-10]
    return nonce

# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------

def register(flask_app, app_module):
    """Register all settings-related routes on *flask_app*.

    *app_module* is a reference to the ``CTS_Scoreboard`` module so we can
    access shared globals (``settings``, ``event_info``, ``time_standards``,
    ``swim_record_sets``, etc.) at runtime.
    """
    global _flask_app, _app
    _flask_app = flask_app
    _app = app_module

    # -- Settings main route -------------------------------------------------

    @flask_app.route('/settings', methods=['POST', 'GET'])
    @flask_login.login_required
    def route_settings():
        schedule_error = None
        standards_error = None
        records_error = None
        settings = _app.settings

        if flask.request.method == 'POST':
            modified = False

            # --- File uploads ------------------------------------------------

            if 'meet_schedule' in flask.request.files:
                file = flask.request.files['meet_schedule']
                if file and file.filename and file.filename.endswith('.hy3'):
                    try:
                        _app.event_info.load_from_bytestream(file.stream)
                    except Exception as e:
                        detail = str(e)
                        schedule_error = 'Failed to parse the schedule file'
                        if detail:
                            schedule_error += ': ' + detail
                    else:
                        settings['event_info'] = _app.event_info.to_object()
                        settings['schedule_filename'] = file.filename
                        _app.send_event_info()
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
                        _app.time_standards = parse_st2_file(tmp_path)
                    except Exception as e:
                        detail = str(e)
                        standards_error = 'Failed to parse the time standards file'
                        if detail:
                            standards_error += ': ' + detail
                    else:
                        settings['time_standards'] = base64.b64encode(pickle.dumps(_app.time_standards)).decode('ascii')
                        settings['standards_filename'] = file.filename
                        new_tags = {s.tag for s in _app.time_standards.header.standards}
                        overrides = settings.get('std_desc_overrides', {})
                        for std in _app.time_standards.header.standards:
                            if std.tag not in overrides:
                                overrides[std.tag] = std.description
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
                        new_rec = parse_rec_file(tmp_path)
                    except Exception as e:
                        detail = str(e)
                        records_error = 'Failed to parse the records file'
                        if detail:
                            records_error += ': ' + detail
                    else:
                        _app.swim_record_sets.append({
                            'rec_file': new_rec,
                            'filename': file.filename,
                            'team_tag': 'ALL',
                            'set_id': _app._next_rec_set_id,
                        })
                        _app._next_rec_set_id += 1
                        settings['swim_record_sets'] = base64.b64encode(pickle.dumps(_app.swim_record_sets)).decode('ascii')
                        modified = True
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except:
                            pass

            # --- Record set team tag dropdowns -------------------------------

            for rec_set in _app.swim_record_sets:
                form_key = 'rec_team_%d' % rec_set['set_id']
                if form_key in flask.request.form:
                    new_tag = flask.request.form[form_key]
                    if new_tag != rec_set['team_tag']:
                        rec_set['team_tag'] = new_tag
                        import pickle, base64
                        settings['swim_record_sets'] = base64.b64encode(pickle.dumps(_app.swim_record_sets)).decode('ascii')
                        modified = True

            # --- Time standard description overrides -------------------------

            if _app.time_standards is not None:
                overrides = settings.get('std_desc_overrides', {})
                for std in _app.time_standards.header.standards:
                    form_key = 'std_desc_' + std.tag
                    if form_key in flask.request.form:
                        new_desc = flask.request.form[form_key].strip()[:15]
                        if new_desc and new_desc != overrides.get(std.tag):
                            overrides[std.tag] = new_desc
                            modified = True
                settings['std_desc_overrides'] = overrides

            # --- Team tag auto-fill ------------------------------------------

            for team_base in ['team_home', 'team_guest1', 'team_guest2', 'team_guest3']:
                tag_key = team_base + '_tag'
                if team_base in flask.request.form:
                    name_val = flask.request.form.get(team_base, '').strip()
                    tag_val = flask.request.form.get(tag_key, '').strip()
                    if name_val and not tag_val:
                        tag_val = name_val[:5].upper()
                    elif not name_val:
                        tag_val = ''
                    tag_val = tag_val[:5]
                    if settings.get(tag_key) != tag_val:
                        settings[tag_key] = tag_val
                        modified = True

            # --- General settings keys from form -----------------------------

            for k in settings.keys():
                if k in flask.request.form and settings[k] != flask.request.form.get(k):
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
                        settings[k] = val
                        modified = True

            # --- Display options (combined form) -----------------------------

            if 'display_options_form' in flask.request.form:
                new_seed = flask.request.form.get('seed_time_label', 'Seed Time')
                if settings.get('seed_time_label') != new_seed:
                    settings['seed_time_label'] = new_seed
                    modified = True
                for opt_key in ('show_pr_tags', 'show_confetti', 'show_time_decorations'):
                    new_val = opt_key in flask.request.form
                    if settings.get(opt_key) != new_val:
                        settings[opt_key] = new_val
                        modified = True

            # Legacy individual checkbox forms (backwards compat)
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

            if 'show_time_decorations_form' in flask.request.form:
                new_val = 'show_time_decorations' in flask.request.form
                if settings.get('show_time_decorations') != new_val:
                    settings['show_time_decorations'] = new_val
                    modified = True

            # --- Message pages -----------------------------------------------

            if 'message_pages_form' in flask.request.form:
                def _visible_len(line):
                    s = re.sub(r'^\s*#{1,4}\s+', '', line)
                    s = re.sub(r'^\s*(\d+\.|[-*])\s+', '', s)
                    s = re.sub(r'`([^`\n]+)`', r'\1', s)
                    s = re.sub(r'\*\*([^*\n]+)\*\*', r'\1', s)
                    s = re.sub(r'~~([^~\n]+)~~', r'\1', s)
                    s = re.sub(r'(^|[^*])\*([^*\n]+)\*(?!\*)', r'\1\2', s)
                    s = re.sub(r'(^|[^_])_([^_\n]+)_(?!_)', r'\1\2', s)
                    return len(s)
                page_count = int(flask.request.form.get('page_count', '1'))
                page_count = max(1, min(5, page_count))
                new_pages = []
                for idx in range(page_count):
                    raw = flask.request.form.get('page_text_%d' % idx, '')
                    lines = raw.replace('\r\n', '\n').replace('\r', '\n').split('\n')[:10]
                    trimmed = []
                    for ln in lines:
                        while _visible_len(ln) > 60:
                            ln = ln[:-1]
                        trimmed.append(ln)
                    text = '\n'.join(trimmed)
                    align = flask.request.form.get('page_align_%d' % idx, 'left')
                    if align not in ('left', 'center', 'right'):
                        align = 'left'
                    enabled = ('page_enabled_%d' % idx) in flask.request.form
                    new_pages.append({'text': text, 'align': align, 'enabled': enabled})
                new_overlay_enabled = 'message_overlay_enabled' in flask.request.form
                new_interval = int(flask.request.form.get('message_rotation_interval', '30'))
                if new_interval < 5 or new_interval > 60 or new_interval % 5 != 0:
                    new_interval = 30

                msg_changed = False
                if settings.get('message_pages') != new_pages:
                    settings['message_pages'] = new_pages
                    modified = True
                    msg_changed = True
                if settings.get('message_overlay_enabled', False) != new_overlay_enabled:
                    settings['message_overlay_enabled'] = new_overlay_enabled
                    modified = True
                    msg_changed = True
                if settings.get('message_rotation_interval', 30) != new_interval:
                    settings['message_rotation_interval'] = new_interval
                    modified = True
                    msg_changed = True
                if msg_changed:
                    overlay_needs_broadcast = True
                else:
                    overlay_needs_broadcast = False
            else:
                overlay_needs_broadcast = False

            # --- Persist & broadcast -----------------------------------------

            if modified:
                with open(_app.settings_file, "wt") as f:
                    json.dump(settings, f, sort_keys=True, indent=4)

            if overlay_needs_broadcast:
                _app.send_message_overlay_state()
                _app._update_message_rotation()

        # --- GET: build template context -------------------------------------

        comm_port_list = [(port, "%s: %s" % (port, desc)) for port, desc, id in serial.tools.list_ports.comports()]
        if settings['serial_port'] not in [port for port, desc in comm_port_list]:
            comm_port_list.insert(0, (settings['serial_port'], settings['serial_port']))

        ad_url_list = []
        for dirpath, dir, file in os.walk(os.path.join("static", "ad")):
            ad_url_list.extend(file)

        schedule_loaded = bool(_app.event_info.event_names)
        standards_loaded = _app.time_standards is not None

        rec_set_info = []
        for rs in _app.swim_record_sets:
            rec_set_info.append({
                'set_id': rs['set_id'],
                'filename': rs['filename'],
                'set_name': rs['rec_file'].header.record_set_name or '',
                'team_tag': rs['team_tag'],
            })

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
                    ad_url_list=ad_url_list,
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
                    std_tag_info=[{'tag': s.tag, 'original_desc': s.description, 'desc_override': settings.get('std_desc_overrides', {}).get(s.tag, s.description)} for s in _app.time_standards.header.standards] if _app.time_standards else [],
                    rec_set_info=rec_set_info,
                    records_error=records_error,
                    team_tag_options=team_tag_options,
                    show_pr_tags=settings.get('show_pr_tags', True),
                    show_confetti=settings.get('show_confetti', True),
                    show_time_decorations=settings.get('show_time_decorations', False),
                    message_pages=settings.get('message_pages', [{'text': '', 'align': 'left', 'enabled': False}]),
                    message_overlay_enabled=settings.get('message_overlay_enabled', False),
                    message_rotation_interval=settings.get('message_rotation_interval', 30),
                    team_home=settings.get('team_home', ''),
                    team_home_tag=settings.get('team_home_tag', ''),
                    team_guest1=settings.get('team_guest1', ''),
                    team_guest1_tag=settings.get('team_guest1_tag', ''),
                    team_guest2=settings.get('team_guest2', ''),
                    team_guest2_tag=settings.get('team_guest2_tag', ''),
                    team_guest3=settings.get('team_guest3', ''),
                    team_guest3_tag=settings.get('team_guest3_tag', ''),
                    shutdown_nonce=_new_shutdown_nonce(),
                    wifi_available=wifi_manager.is_available())

    # -- WiFi management API -------------------------------------------------

    @flask_app.route('/wifi/status')
    @flask_login.login_required
    def route_wifi_status():
        status = wifi_manager.get_status()
        status['available'] = wifi_manager.is_available()
        return flask.jsonify(status)

    @flask_app.route('/wifi/scan')
    @flask_login.login_required
    def route_wifi_scan():
        networks = wifi_manager.scan_networks()
        saved = wifi_manager.get_saved_networks()
        return flask.jsonify({'networks': networks, 'saved': saved})

    @flask_app.route('/wifi/connect', methods=['POST'])
    @flask_login.login_required
    def route_wifi_connect():
        data = flask.request.get_json(force=True)
        ssid = data.get('ssid', '')
        password = data.get('password') or None
        if not ssid:
            return flask.jsonify({'success': False, 'message': 'SSID is required'}), 400
        ok, msg = wifi_manager.connect(ssid, password)
        return flask.jsonify({'success': ok, 'message': msg})

    @flask_app.route('/wifi/forget', methods=['POST'])
    @flask_login.login_required
    def route_wifi_forget():
        data = flask.request.get_json(force=True)
        ssid = data.get('ssid', '')
        if not ssid:
            return flask.jsonify({'success': False, 'message': 'SSID is required'}), 400
        ok, msg = wifi_manager.forget(ssid)
        return flask.jsonify({'success': ok, 'message': msg})

    @flask_app.route('/wifi/update_password', methods=['POST'])
    @flask_login.login_required
    def route_wifi_update_password():
        data = flask.request.get_json(force=True)
        ssid = data.get('ssid', '')
        password = data.get('password', '')
        if not ssid or not password:
            return flask.jsonify({'success': False, 'message': 'SSID and password are required'}), 400
        ok, msg = wifi_manager.update_password(ssid, password)
        return flask.jsonify({'success': ok, 'message': msg})

    # -- Clear / remove routes -----------------------------------------------

    @flask_app.route('/schedule_clear')
    @flask_login.login_required
    def route_schedule_clear():
        _app.event_info.clear()
        _app.settings['event_info'] = _app.event_info.to_object()
        _app.settings.pop('schedule_filename', None)
        with open(_app.settings_file, "wt") as f:
            json.dump(_app.settings, f, sort_keys=True, indent=4)
        return flask.redirect('/settings')

    @flask_app.route('/standards_clear')
    @flask_login.login_required
    def route_standards_clear():
        _app.time_standards = None
        _app.settings.pop('time_standards', None)
        _app.settings.pop('standards_filename', None)
        _app.settings.pop('std_desc_overrides', None)
        with open(_app.settings_file, "wt") as f:
            json.dump(_app.settings, f, sort_keys=True, indent=4)
        return flask.redirect('/settings')

    @flask_app.route('/records_remove/<int:set_id>')
    @flask_login.login_required
    def route_records_remove(set_id):
        _app.swim_record_sets = [s for s in _app.swim_record_sets if s['set_id'] != set_id]
        import pickle, base64
        if _app.swim_record_sets:
            _app.settings['swim_record_sets'] = base64.b64encode(pickle.dumps(_app.swim_record_sets)).decode('ascii')
        else:
            _app.settings.pop('swim_record_sets', None)
        with open(_app.settings_file, "wt") as f:
            json.dump(_app.settings, f, sort_keys=True, indent=4)
        return flask.redirect('/settings')

    # -- Shutdown ------------------------------------------------------------

    @flask_app.route('/shutdown', methods=['POST'])
    @flask_login.login_required
    def route_shutdown():
        nonce = flask.request.form.get('nonce', '')
        if not nonce or nonce not in _shutdown_nonces:
            return 'Invalid request', 403
        _shutdown_nonces.clear()
        import threading
        def _exit():
            import time
            time.sleep(0.5)
            os._exit(0)
        threading.Thread(target=_exit, daemon=True).start()
        return 'Server shutting down...', 200

    # -- Schedule preview / combine events -----------------------------------

    @flask_app.route('/combine_events')
    @flask_login.login_required
    def route_combine_events():
        event_heat = list(_app.event_info.events_uncombined.keys())
        event_heat.sort()
        return flask.render_template('schedule_preview.html',
                    event_heat=event_heat,
                    event_names=_app.event_info.event_names,
                    events=_app.event_info.events_uncombined,
                    combined=_app.event_info.combined,
                    show_combine_select=True)

    @flask_app.route('/schedule_preview', methods=["GET", "POST"])
    @flask_login.login_required
    def route_schedule_preview():
        if flask.request.method == 'POST':
            combined = {}
            for key, value in flask.request.form.items():
                if key.startswith('combine_') and value.strip():
                    k = key.split('_')
                    v = value.split(',')
                    combined[(int(k[1]), int(k[2]))] = (int(v[0]), int(v[1]))
            _app.event_info.combine_events(combined)
            _app.settings['event_info'] = _app.event_info.to_object()
            with open(_app.settings_file, "wt") as f:
                json.dump(_app.settings, f, sort_keys=True, indent=4)
        event_heat = list(_app.event_info.events.keys())
        event_heat.sort()
        return flask.render_template('schedule_preview.html',
                    event_heat=event_heat,
                    event_names=_app.event_info.event_names,
                    events=_app.event_info.events,
                    show_combine_select=False)

    # -- Login / logout ------------------------------------------------------

    @flask_app.route("/login", methods=["GET", "POST"])
    def route_login():
        if flask.request.method == 'POST':
            if ((flask.request.form['username'] == _app.settings['username']) and
                (flask.request.form['password'] == _app.settings['password'])):
                user = _app.User(0)
                flask_login.login_user(user)
                return flask.redirect(flask.request.args.get("next"))
            else:
                return flask.abort(401)
        else:
            return flask.render_template('login.html')

    @flask_app.route("/logout")
    @flask_login.login_required
    def route_logout():
        flask_login.logout_user()
        return flask.redirect('/')

    @flask_app.errorhandler(401)
    def page_not_found(e):
        return flask.render_template('login.html', login_failed=True)
