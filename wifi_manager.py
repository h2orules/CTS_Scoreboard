"""WiFi network management via NetworkManager (nmcli)."""

import shutil
import subprocess
import time


def is_available():
    """Return True if nmcli is present on this system."""
    return shutil.which('nmcli') is not None


def _run(args, timeout=30):
    """Run an nmcli command and return (returncode, stdout, stderr)."""
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
    if password:
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
    code, out, err = _run(['con', 'delete', 'id', ssid])
    if code == 0:
        return True, 'Forgot %s' % ssid
    return False, (err or out or 'Failed to forget network').strip()


def update_password(ssid, password):
    """Update the password for a saved WiFi network.

    Returns (success: bool, message: str).
    """
    code, out, err = _run([
        'con', 'modify', 'id', ssid, 'wifi-sec.psk', password,
    ])
    if code == 0:
        return True, 'Password updated for %s' % ssid
    return False, (err or out or 'Failed to update password').strip()
