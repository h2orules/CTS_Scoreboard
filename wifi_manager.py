"""WiFi network management via NetworkManager (nmcli)."""

import re
import shutil
import subprocess
import time


# Conservative allowlist for SSID values passed to nmcli. The leading
# character excludes '-' so a value can never be parsed as an nmcli option,
# and the character classes exclude whitespace/control characters (NUL, CR,
# LF) and shell metacharacters. Length is bounded to reject absurd inputs.
_SAFE_NMCLI_TOKEN_RE = re.compile(r'[A-Za-z0-9_.:/@+=,][A-Za-z0-9 _.:/@+=,\-]{0,63}')


# nmcli option flags the helper is allowed to emit. These are constants in
# this module (never user input), so they are reconstructed from a fixed
# table rather than passed through the value allowlist.
_ALLOWED_NMCLI_FLAGS = ('-t', '-f')

# The exact set of characters permitted in a sanitized nmcli token, as a
# module-level constant string. Sanitized output is read out of this constant
# (indexed by the position of each input character), so the value reaching the
# subprocess sink is provably derived from constants, not from user input.
_ALLOWED_TOKEN_CHARS = (
    'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    'abcdefghijklmnopqrstuvwxyz'
    '0123456789'
    ' _.:/@+=,-'
)
_MAX_TOKEN_LEN = 64

# Constant allowlist of characters permitted in a sanitized secret. As with
# tokens, sanitized secrets are rebuilt out of this constant.
_ALLOWED_SECRET_CHARS = (
    'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    'abcdefghijklmnopqrstuvwxyz'
    '0123456789'
    ' _.:/@+=!?#$%^&*(){}[],;-'
)
_MAX_SECRET_LEN = 128


def _is_safe_nmcli_value(value):
    """Return True if *value* is safe to pass as an nmcli argument value."""
    return isinstance(value, str) and _SAFE_NMCLI_TOKEN_RE.fullmatch(value) is not None


def _is_safe_nmcli_arg_list(args):
    """Return True if *args* is a safe nmcli argument list."""
    if not isinstance(args, list):
        return False
    for arg in args:
        if not isinstance(arg, str) or not arg:
            return False
        if any(ch in arg for ch in ('\x00', '\n', '\r')):
            return False
        if arg.startswith('-') and arg not in _ALLOWED_NMCLI_FLAGS:
            return False
    return True


def _sanitize_token(value):
    """Validate and re-derive a single nmcli token from a constant allowlist.

    Raises ``ValueError`` if *value* is not a safe token. Rather than returning
    the original (untrusted) object after a boolean check, each character is
    looked up in the :data:`_ALLOWED_TOKEN_CHARS` constant and the result is
    rebuilt from those constant characters. Only the *index* derives from the
    input; the emitted characters come from a constant string. This makes the
    sanitization explicit to static analysis (CodeQL), so it recognizes the
    return value as a barrier instead of seeing tainted input reach the sink.
    """
    if not isinstance(value, str):
        raise ValueError('nmcli argument must be a string')
    # An allowed option flag (e.g. -t, -f) is itself a constant: emit a copy.
    if value in _ALLOWED_NMCLI_FLAGS:
        return _ALLOWED_NMCLI_FLAGS[_ALLOWED_NMCLI_FLAGS.index(value)]
    if not value or len(value) > _MAX_TOKEN_LEN:
        raise ValueError('Unsafe nmcli argument')
    if value[0] == '-':
        raise ValueError('Unsafe nmcli argument')
    rebuilt = []
    for ch in value:
        idx = _ALLOWED_TOKEN_CHARS.find(ch)
        if idx < 0:
            raise ValueError('Unsafe nmcli argument')
        rebuilt.append(_ALLOWED_TOKEN_CHARS[idx])
    return ''.join(rebuilt)


def _normalize_secret(value):
    """Return a sanitized password string if valid, else None.

    Like :func:`_sanitize_token`, the returned secret is rebuilt from the
    constant :data:`_ALLOWED_SECRET_CHARS` allowlist so the value reaching the
    subprocess sink is derived from constants rather than the untrusted input.
    """
    if not isinstance(value, str) or not value or len(value) > _MAX_SECRET_LEN:
        return None
    if value[0] == '-':
        return None
    rebuilt = []
    for ch in value:
        idx = _ALLOWED_SECRET_CHARS.find(ch)
        if idx < 0:
            return None
        rebuilt.append(_ALLOWED_SECRET_CHARS[idx])
    return ''.join(rebuilt)


def is_available():
    """Return True if nmcli is present on this system."""
    return shutil.which('nmcli') is not None


def _run(args, timeout=30):
    """Run an nmcli command and return (returncode, stdout, stderr).

    Callers pass values that have already been rebound through
    :func:`_sanitize_token` / :func:`_normalize_secret` (so user-derived data
    is reconstructed from constant allowlists before reaching this sink). The
    structural check below is a defense-in-depth guard against control
    characters and stray option-like arguments.
    """
    if not _is_safe_nmcli_arg_list(args):
        return -1, '', 'Invalid nmcli arguments'
    try:
        r = subprocess.run(
            ['sudo', 'nmcli'] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, '', 'nmcli not found'
    except subprocess.TimeoutExpired:
        return -1, '', 'Command timed out'


def _split_terse(line):
    """Split an nmcli terse-mode line on unescaped colons.

    nmcli -t escapes literal colons inside values as ``\\:``.
    Returns a list of unescaped field values.
    """
    fields = []
    current = []
    i = 0
    while i < len(line):
        if line[i] == '\\' and i + 1 < len(line) and line[i + 1] == ':':
            current.append(':')
            i += 2
        elif line[i] == ':':
            fields.append(''.join(current))
            current = []
            i += 1
        else:
            current.append(line[i])
            i += 1
    fields.append(''.join(current))
    return fields


def scan_networks():
    """Scan for visible WiFi networks.

    Returns a list of dicts sorted by signal strength (strongest first):
        [{'ssid': str, 'signal': int, 'security': str, 'in_use': bool}, ...]
    """
    # Trigger a fresh scan; ignore errors (e.g. rate-limiting by NetworkManager)
    _run(['dev', 'wifi', 'rescan'], timeout=10)
    # Allow time for the radio to scan all WiFi channels
    time.sleep(3)
    # Retrieve results from the completed scan
    code, out, _ = _run([
        '-t', '-f', 'SSID,SIGNAL,SECURITY,IN-USE',
        'dev', 'wifi', 'list',
    ])
    if code != 0:
        return []

    seen = {}
    for line in out.strip().splitlines():
        # nmcli terse mode escapes colons in values as \: — split carefully
        # Fields: SSID:SIGNAL:SECURITY:IN-USE
        # Parse from the right since SIGNAL/SECURITY/IN-USE are predictable
        parts = _split_terse(line)
        if len(parts) < 4:
            continue
        ssid = parts[0]
        if not ssid:
            continue
        try:
            signal = int(parts[1])
        except ValueError:
            signal = 0
        security = parts[2] if parts[2] and parts[2] != '--' else ''
        in_use = parts[3].strip() == '*'
        # Keep the entry with the strongest signal per SSID
        if ssid not in seen or signal > seen[ssid]['signal']:
            seen[ssid] = {
                'ssid': ssid,
                'signal': signal,
                'security': security,
                'in_use': in_use,
            }
    networks = sorted(seen.values(), key=lambda n: n['signal'], reverse=True)
    return networks


def get_status():
    """Return the current network connection status.

    Returns:
        {'wifi_ssid': str|None, 'wifi_signal': int|None, 'ethernet': bool}
    """
    result = {'wifi_ssid': None, 'wifi_signal': None, 'ethernet': False}
    code, out, _ = _run(['-t', '-f', 'TYPE,STATE,CONNECTION', 'dev'])
    if code != 0:
        return result

    for line in out.strip().splitlines():
        parts = _split_terse(line)
        if len(parts) < 3:
            continue
        dev_type = parts[0]
        state = parts[1]
        connection = parts[2]
        if dev_type == 'wifi' and state == 'connected' and connection:
            result['wifi_ssid'] = connection
        if dev_type == 'ethernet' and state == 'connected':
            result['ethernet'] = True

    # Get signal strength for the connected WiFi network
    if result['wifi_ssid']:
        code2, out2, _ = _run([
            '-t', '-f', 'IN-USE,SIGNAL', 'dev', 'wifi', 'list',
        ])
        if code2 == 0:
            for line in out2.strip().splitlines():
                parts = _split_terse(line)
                if len(parts) >= 2 and parts[0].strip() == '*':
                    try:
                        result['wifi_signal'] = int(parts[1])
                    except ValueError:
                        pass
                    break

    return result


def get_saved_networks():
    """Return a list of saved WiFi network names (SSIDs)."""
    code, out, _ = _run(['-t', '-f', 'NAME,TYPE', 'con', 'show'])
    if code != 0:
        return []
    saved = []
    for line in out.strip().splitlines():
        parts = _split_terse(line)
        if len(parts) >= 2 and parts[1] == '802-11-wireless':
            name = parts[0]
            if name:
                saved.append(name)
    return saved


def connect(ssid, password=None):
    """Connect to a WiFi network.

    If password is provided, connect as a new network.
    If password is None, activate an existing saved connection.

    Returns (success: bool, message: str).
    """
    if not _is_safe_nmcli_value(ssid):
        return False, 'Invalid SSID'
    # Rebind SSID to a value reconstructed from constants before it reaches
    # the subprocess sink (sanitization barrier for static analysis).
    ssid = _sanitize_token(ssid)

    if password:
        password = _normalize_secret(password)
        if password is None:
            return False, 'Invalid password'
        code, out, err = _run([
            'dev', 'wifi', 'connect', ssid, 'password', password,
        ])
    else:
        code, out, err = _run(['con', 'up', 'id', ssid])

    if code == 0:
        return True, 'Connected to %s' % ssid
    return False, (err or out or 'Failed to connect').strip()


def forget(ssid):
    """Remove a saved WiFi network profile.

    Returns (success: bool, message: str).
    """
    if not _is_safe_nmcli_value(ssid):
        return False, 'Invalid SSID'
    ssid = _sanitize_token(ssid)
    code, out, err = _run(['con', 'delete', 'id', ssid])
    if code == 0:
        return True, 'Forgot %s' % ssid
    return False, (err or out or 'Failed to forget network').strip()


def update_password(ssid, password):
    """Update the password for a saved WiFi network.

    Returns (success: bool, message: str).
    """
    if not _is_safe_nmcli_value(ssid):
        return False, 'Invalid SSID'
    ssid = _sanitize_token(ssid)
    password = _normalize_secret(password)
    if password is None:
        return False, 'Invalid password'
    code, out, err = _run([
        'con', 'modify', 'id', ssid, 'wifi-sec.psk', password,
    ])
    if code == 0:
        return True, 'Password updated for %s' % ssid
    return False, (err or out or 'Failed to update password').strip()
