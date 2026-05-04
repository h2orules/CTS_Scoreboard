# Copilot Instructions — CTS Scoreboard

## Project Overview

A Python/Flask application that snoops the serial link between a Colorado Time Systems (CTS) timing console and scoreboard, then renders live race data to HTML via WebSockets (Flask-SocketIO). Designed to run on a Raspberry Pi 5. Supports loading HyTek meet files (.hy3), time standards (.st2), and swim records (.rec).

## Build & Test

```bash
# Install dependencies (uses uv, not pip)
uv sync

# Run the app in development
uv run cts-scoreboard

# Run all Python tests
uv run pytest

# Run a single Python test file
uv run pytest tests/test_race_state_machine.py

# Run a single Python test by name
uv run pytest tests/test_race_state_machine.py -k "test_name"

# Run JavaScript tests (vitest)
npm test

# Run a single JS test file
npx vitest run tests/js/scoreboard.test.js

# Production (gunicorn + gevent)
./start.sh
```

## Architecture

### Backend (Python)

- **`CTS_Scoreboard.py`** — Main module. Contains the Flask app, serial protocol parser, WebSocket event handlers, and all shared global state (lane data, event/heat info, race FSM, settings, content cache). This is a large file (~1000 lines); most server-side logic lives here.
- **`settings_routes.py`** — Settings page routes, extracted from the main module. Uses a `register(flask_app, app_module)` pattern to wire routes and access shared globals from `CTS_Scoreboard`.
- **`sim.py`** — Demo/simulation mode. Also uses `register(socketio, app_module)` to set up WebSocket handlers on the `/scoreboard` namespace.
- **`race_state_machine.py`** — Finite state machine (using the `transitions` library) tracking race lifecycle: PreRace → Running → Finished → Clear, with blank states. Uses `LockedMachine` for thread safety.
- **`hytek_event_loader.py`** — Parses HyTek `.hy3` meet files to load event/entry data. Includes a monkey-patch for the `hytek-parser` library to handle malformed date fields.
- **`hytek_st2_parser.py`** / **`hytek_rec_parser.py`** — Parse HyTek time standards (.st2) and swim records (.rec) files.
- **`wifi_manager.py`** — NetworkManager (nmcli) wrapper for Wi-Fi configuration on the Pi.
- **`ap.py`** — Low-level console display helper (legacy, rarely modified).

### Frontend (JavaScript)

- **`static/js/scoreboard.js`** — Core scoreboard logic (time parsing, qualifying-time evaluation, record matching, seed-time formatting). Uses an IIFE pattern that exports to `window` in the browser and `module.exports` in Node.js, enabling Vitest testing without bundlers.
- **`static/js/msg_formatting.js`** — Message formatting utilities, same IIFE dual-export pattern.
- **`templates/`** — Jinja2 templates. `templates/web/home.html` is the main scoreboard display; `templates/settings.html` is the admin UI.

### Communication

Real-time data flows from serial port → Python parser → Flask-SocketIO → browser via WebSocket events on the `/scoreboard` namespace. The settings page uses standard HTTP POST routes.

## Key Conventions

- **Module registration pattern**: `sim.py` and `settings_routes.py` use a `register()` function called at startup, receiving references to the Flask app and/or the main `CTS_Scoreboard` module to access shared globals. Follow this pattern when extracting new route groups.
- **Settings persistence**: All settings live in `settings.json` (flat file, loaded at startup). The `settings` dict in `CTS_Scoreboard.py` defines defaults; `load_settings()` merges saved values on top. Includes migration logic for old formats.
- **JS dual-export IIFE**: Frontend JS files that need testing use `(function(exports) { ... })(typeof module !== 'undefined' ? module.exports : window)` so they work both as browser globals and Node.js modules.
- **Test fixtures**: Python tests use a shared `conftest.py` with `samples_dir` and `hytek_dir` fixtures pointing to `samples/` and `samples/HyTek/`.
- **Content caching**: Server-rendered HTML fragments are cached with SHA-256-based keys (`_cache_put` / `_cache_get` in `CTS_Scoreboard.py`).
