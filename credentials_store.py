"""Web-login credential store for the CTS Scoreboard.

Credentials live in a git-ignored, mode-0600 JSON file (``credentials.json``)
next to ``settings.json``. The password is never stored: only a
PBKDF2-HMAC-SHA256 hash with a per-password random salt. If the file is
absent, the defaults ``admin`` / ``password`` apply.

This module deliberately has no dependency on ``CTS_Scoreboard`` so the
``set_credentials.py`` CLI can import it without starting the app.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets

logger = logging.getLogger(__name__)

# Resolve against the script directory (same rule as settings_file in
# CTS_Scoreboard.py). Module-level so tests can monkeypatch.
_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
credentials_file = os.path.join(_REPO_DIR, "credentials.json")
# Flask session-signing key. Generated once per install and persisted (so
# sessions survive restarts) in a git-ignored, mode-0600 file next to the
# credential store. Module-level so tests can monkeypatch.
secret_key_file = os.path.join(_REPO_DIR, "secret_key")

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "password"
ALGORITHM = "pbkdf2_sha256"
# Iteration count balances brute-force cost against login latency on a
# Raspberry Pi, where a CPU-bound hash blocks the gevent event loop (and
# with it all live scoreboard websocket traffic) for the duration of each
# login attempt. The count is stored per-record and honoured on verify, so
# it can be raised later without invalidating existing files.
ITERATIONS = 200_000
_SALT_BYTES = 16


def _hash_password(password, salt, iterations):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def hash_record(password, iterations=ITERATIONS):
    """Return the storable hash fields for ``password`` with a fresh salt."""
    salt = secrets.token_bytes(_SALT_BYTES)
    return {
        "algorithm": ALGORITHM,
        "salt": salt.hex(),
        "iterations": iterations,
        "password_hash": _hash_password(password, salt, iterations).hex(),
    }


def load_store():
    """Return the stored credential record, or None if absent / unreadable."""
    if not os.path.exists(credentials_file):
        return None
    try:
        with open(credentials_file, "rt") as f:
            store = json.load(f)
        if not isinstance(store, dict) or "password_hash" not in store:
            raise ValueError("malformed credential store")
        return store
    except (ValueError, OSError) as e:
        logger.warning("could not load credentials from %s: %s", credentials_file, e)
        return None


def _write_store(record):
    """Persist the credential record atomically with mode 0600."""
    tmp = credentials_file + ".tmp"
    with open(tmp, "wt") as f:
        json.dump(record, f, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        # Windows test envs may not honour chmod; ignore.
        pass
    os.replace(tmp, credentials_file)


def save_credentials(username, password):
    """Hash ``password`` with a fresh salt and write the store."""
    record = {"username": username}
    record.update(hash_record(password))
    _write_store(record)


def get_or_create_secret_key():
    """Return this install's Flask secret key, generating it on first use.

    The key is persisted in ``secret_key_file`` (git-ignored, mode 0600) so
    that signed session cookies survive process restarts. A fresh random key
    is created the first time the app starts on a given install, replacing
    the formerly hard-coded value.
    """
    try:
        with open(secret_key_file, "rt") as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    tmp = secret_key_file + ".tmp"
    try:
        with open(tmp, "wt") as f:
            f.write(key)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            # Windows test envs may not honour chmod; ignore.
            pass
        os.replace(tmp, secret_key_file)
    except OSError as e:
        # If we can't persist (e.g. read-only fs), fall back to an
        # in-memory key for this process rather than failing to start.
        logger.warning("could not persist secret key to %s: %s", secret_key_file, e)
    return key


def get_username():
    store = load_store()
    if store is None:
        return DEFAULT_USERNAME
    return store.get("username", DEFAULT_USERNAME)


def set_username(username):
    """Change the login username, keeping the current password."""
    store = load_store()
    if store is None:
        # No store yet: materialise one that keeps the default password.
        save_credentials(username, DEFAULT_PASSWORD)
        return
    store["username"] = username
    _write_store(store)


def set_password(password):
    """Change the login password (fresh salt), keeping the current username."""
    save_credentials(get_username(), password)


def verify_login(username, password):
    """Constant-time check of ``username``/``password`` against the store.

    Falls back to DEFAULT_USERNAME/DEFAULT_PASSWORD when no store exists.
    The password hash is always computed before the username comparison so
    a wrong username costs the same time as a wrong password.
    """
    store = load_store()
    if store is None:
        store = {"username": DEFAULT_USERNAME}
        store.update(hash_record(DEFAULT_PASSWORD))
    if store.get("algorithm") != ALGORITHM:
        logger.warning("unknown credential algorithm %r", store.get("algorithm"))
        return False
    try:
        salt = bytes.fromhex(store["salt"])
        iterations = int(store["iterations"])
        stored_hash = bytes.fromhex(store["password_hash"])
    except (KeyError, ValueError, TypeError) as e:
        logger.warning("malformed credential store: %s", e)
        return False
    computed = _hash_password(password, salt, iterations)
    password_ok = hmac.compare_digest(computed, stored_hash)
    username_ok = hmac.compare_digest(
        username.encode("utf-8"),
        str(store.get("username", DEFAULT_USERNAME)).encode("utf-8"),
    )
    return password_ok and username_ok
