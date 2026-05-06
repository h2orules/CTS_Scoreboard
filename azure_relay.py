"""Pi-side relay client to the Azure scoreboard front-end.

The relay client runs in a background thread, holds a single outbound
Socket.IO connection to the Azure relay app, and exposes a small synchronous
API to the Flask main thread:

    relay = AzureRelayClient(creds_file='azure_credentials.json')
    relay.start()
    relay.forward_event('update_scoreboard', payload)   # fire-and-forget
    relay.request_login(scope='api://<audience>/.default')
    relay.complete_login()
    relay.logout()
    relay.force_reconnect()
    relay.rotate_meet_id()
    relay.status                                        # current state name
    relay.snapshot()                                    # dict of public state

Design constraints
------------------
* The Pi runs Flask-SocketIO under gunicorn+gevent in production. The relay
  client owns a daemon ``threading.Thread``; outbound events are pushed via a
  ``queue.Queue`` so the gevent main loop never blocks on network I/O.
* No work happens until ``start()`` is called. This module is safe to import
  at app load time even when the relay is disabled.
* Phase 2 ships the state machine, exponential backoff, OAuth2 device-code
  login (msal), credential persistence, and the public API. The actual
  Socket.IO connection to Azure is a single seam method ``_open_socket()``
  that Phase 3 wires to ``socketio.Client``.
* Credentials live in a 0600-mode JSON file (default ``azure_credentials.json``)
  that is in ``.gitignore``. They are never written to ``settings.json``.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import string
import threading
import time
from dataclasses import asdict, dataclass, field
from queue import Empty, Queue
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Backoff schedule in seconds. Caps at 300s. Reset on successful connect.
BACKOFF_SCHEDULE: tuple[int, ...] = (1, 2, 4, 8, 16, 32, 60, 120, 300)
HEARTBEAT_INTERVAL_S = 10
HEARTBEAT_DEGRADED_AFTER_S = 30
MEET_ID_LENGTH = 15
# Alphabet: alphanumeric, no ambiguous chars (no 0/O, no 1/l/I).
MEET_ID_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz"

# State names (intentional simple strings; mirrored in tests).
STATE_DISCONNECTED = "disconnected"
STATE_NEEDS_AUTH = "needs_auth"
STATE_AUTHENTICATING = "authenticating"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_DEGRADED = "degraded"
STATE_BACKOFF = "backoff"
STATE_STOPPED = "stopped"


def generate_meet_id(length: int = MEET_ID_LENGTH) -> str:
    """Generate a 15-char URL-safe meet ID using a no-confusion alphabet."""
    return "".join(secrets.choice(MEET_ID_ALPHABET) for _ in range(length))


def compute_backoff(attempt: int, schedule: tuple[int, ...] = BACKOFF_SCHEDULE) -> int:
    """Return the backoff delay (seconds) for a given attempt index (0-based).

    attempt=0 returns the first delay; attempts beyond the schedule cap at the
    last value.
    """
    if attempt < 0:
        attempt = 0
    if attempt >= len(schedule):
        return schedule[-1]
    return schedule[attempt]


@dataclass
class AzureCredentials:
    """Serializable Azure credential bundle (refresh token + meet binding)."""

    tenant_id: str
    client_id: str
    audience: str
    refresh_token: str
    account_id: str
    home_account_id: str
    meet_id: str
    upn: str | None = None
    scopes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AzureCredentials":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def load_credentials(path: str) -> AzureCredentials | None:
    """Load credentials from disk, or None if absent / unreadable."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return AzureCredentials.from_dict(json.load(f))
    except (json.JSONDecodeError, TypeError, KeyError, OSError) as e:
        logger.warning("could not load azure credentials from %s: %s", path, e)
        return None


def save_credentials(path: str, creds: AzureCredentials) -> None:
    """Persist credentials atomically with mode 0600."""
    tmp = path + ".tmp"
    payload = json.dumps(creds.to_dict(), indent=2)
    with open(tmp, "w") as f:
        f.write(payload)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        # Windows test envs may not honour chmod; ignore.
        pass
    os.replace(tmp, path)


def clear_credentials(path: str) -> None:
    """Remove the credentials file if present."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


@dataclass
class DeviceCodeFlow:
    """In-flight device-code flow record (returned to the operator)."""

    user_code: str
    verification_uri: str
    expires_at: float
    message: str
    # Opaque flow handle stored by msal; not surfaced.
    _flow: dict[str, Any] = field(default_factory=dict, repr=False)


class AzureRelayClient:
    """Background relay client. Thread-safe public API."""

    def __init__(
        self,
        *,
        creds_file: str = "azure_credentials.json",
        relay_url: str = "",
        protocol_version: int = 1,
        backoff_schedule: tuple[int, ...] = BACKOFF_SCHEDULE,
        msal_app_factory: Callable[..., Any] | None = None,
        bundle_provider: Callable[[], dict[str, Any] | None] | None = None,
        context_provider: Callable[[], dict[str, Any] | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.creds_file = creds_file
        self.relay_url = relay_url
        self.protocol_version = protocol_version
        self.backoff_schedule = backoff_schedule
        self._msal_factory = msal_app_factory
        self._bundle_provider = bundle_provider
        self._context_provider = context_provider
        self._clock = clock

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._queue: Queue[tuple[str, dict[str, Any]]] = Queue(maxsize=1000)
        self._status_subscribers: list[Callable[[dict[str, Any]], None]] = []

        # Public state (read under self._lock or via snapshot()).
        self._creds = load_credentials(creds_file)
        self._state: str = STATE_NEEDS_AUTH if self._creds is None else STATE_DISCONNECTED
        self._last_error: str | None = None
        self._last_connected_at: float | None = None
        self._last_heartbeat_at: float | None = None
        self._next_retry_at: float | None = None
        self._attempt: int = 0
        self._active_client_count: int = 0
        self._device_flow: DeviceCodeFlow | None = None
        self._last_pushed_bundle_id: str | None = None

    # ---------------- public API ----------------

    @property
    def status(self) -> str:
        with self._lock:
            return self._state

    @property
    def meet_id(self) -> str | None:
        with self._lock:
            return self._creds.meet_id if self._creds else None

    def snapshot(self) -> dict[str, Any]:
        """Dict of all public state, safe to JSON-serialize."""
        with self._lock:
            return {
                "state": self._state,
                "meet_id": self._creds.meet_id if self._creds else None,
                "upn": self._creds.upn if self._creds else None,
                "last_error": self._last_error,
                "last_connected_at": self._last_connected_at,
                "last_heartbeat_at": self._last_heartbeat_at,
                "next_retry_at": self._next_retry_at,
                "attempt": self._attempt,
                "active_client_count": self._active_client_count,
                "protocol_version": self.protocol_version,
                "device_flow": (
                    {
                        "user_code": self._device_flow.user_code,
                        "verification_uri": self._device_flow.verification_uri,
                        "expires_at": self._device_flow.expires_at,
                        "message": self._device_flow.message,
                    }
                    if self._device_flow
                    else None
                ),
            }

    def subscribe_status(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback fired (best-effort) on every state change."""
        with self._lock:
            self._status_subscribers.append(fn)

    def start(self) -> None:
        """Start the background worker thread (idempotent)."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="AzureRelayClient", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the background thread to stop and wait briefly."""
        self._stop.set()
        with self._lock:
            t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
        self._set_state(STATE_STOPPED)

    def forward_event(self, name: str, payload: dict[str, Any]) -> bool:
        """Enqueue an event to forward to Azure. Returns False if the queue is full."""
        try:
            self._queue.put_nowait((name, payload))
            return True
        except Exception:
            return False

    def force_reconnect(self) -> None:
        """Reset backoff so the next loop iteration tries to reconnect immediately."""
        with self._lock:
            self._attempt = 0
            self._next_retry_at = None
            if self._state in (STATE_BACKOFF, STATE_DEGRADED, STATE_DISCONNECTED):
                self._set_state(STATE_CONNECTING)
        # Wake the queue.
        self.forward_event("__noop__", {})

    def request_login(
        self,
        *,
        tenant_id: str,
        client_id: str,
        audience: str,
        scopes: list[str] | None = None,
    ) -> DeviceCodeFlow:
        """Initiate a device-code flow. Returns the user code + verification URL.

        The operator visits the URL on a separate device, enters the code, and
        completes sign-in. Then call ``complete_login()`` to harvest the
        refresh token.
        """
        if self._msal_factory is None:
            from msal import PublicClientApplication

            authority = f"https://login.microsoftonline.com/{tenant_id}"
            app = PublicClientApplication(client_id, authority=authority)
        else:
            app = self._msal_factory(client_id=client_id, tenant_id=tenant_id)

        flow_scopes = scopes or [f"{audience}/.default"]
        flow = app.initiate_device_flow(scopes=flow_scopes)
        if "user_code" not in flow:
            raise RuntimeError(f"failed to start device flow: {flow}")

        record = DeviceCodeFlow(
            user_code=flow["user_code"],
            verification_uri=flow.get("verification_uri", ""),
            expires_at=self._clock() + flow.get("expires_in", 900),
            message=flow.get("message", ""),
            _flow=flow,
        )
        with self._lock:
            self._device_flow = record
            self._pending_msal = (app, tenant_id, client_id, audience, flow_scopes)
            self._set_state(STATE_AUTHENTICATING)
        return record

    def complete_login(self) -> bool:
        """Block until the in-flight device flow completes (or fails / times out).

        On success, persists credentials and transitions to CONNECTING.
        Returns True on success, False otherwise (with ``last_error`` set).
        """
        with self._lock:
            if not self._device_flow or not getattr(self, "_pending_msal", None):
                self._last_error = "no device flow in progress"
                return False
            app, tenant_id, client_id, audience, scopes = self._pending_msal
            flow = self._device_flow._flow

        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            err = result.get("error_description") or result.get("error") or "unknown"
            with self._lock:
                self._last_error = f"device flow failed: {err}"
                self._device_flow = None
                self._set_state(STATE_NEEDS_AUTH)
            return False

        # Pull the cached account so we can extract a refresh token.
        accounts = app.get_accounts()
        account = accounts[0] if accounts else None
        token_cache = app.token_cache.serialize()
        # Extract refresh token from the cache JSON.
        refresh_token = ""
        try:
            cache_obj = json.loads(token_cache)
            for entry in cache_obj.get("RefreshToken", {}).values():
                refresh_token = entry.get("secret", "")
                if refresh_token:
                    break
        except (json.JSONDecodeError, AttributeError):
            pass

        creds = AzureCredentials(
            tenant_id=tenant_id,
            client_id=client_id,
            audience=audience,
            refresh_token=refresh_token,
            account_id=(account.get("local_account_id", "") if account else ""),
            home_account_id=(account.get("home_account_id", "") if account else ""),
            upn=(account.get("username") if account else None),
            scopes=scopes,
            meet_id=generate_meet_id(),
        )
        save_credentials(self.creds_file, creds)
        with self._lock:
            self._creds = creds
            self._device_flow = None
            self._pending_msal = None  # type: ignore[assignment]
            self._last_error = None
            self._attempt = 0
            self._set_state(STATE_CONNECTING)
        return True

    def logout(self) -> None:
        """Drop credentials, send meet_close (best-effort), reset state."""
        self.forward_event("meet_close", {})
        clear_credentials(self.creds_file)
        with self._lock:
            self._creds = None
            self._set_state(STATE_NEEDS_AUTH)

    def rotate_meet_id(self) -> str | None:
        """Generate a new meet ID, persist, and force a reconnect.

        Returns the new ID, or None if not signed in.
        """
        with self._lock:
            if not self._creds:
                return None
            new_id = generate_meet_id()
            self._creds.meet_id = new_id
            save_credentials(self.creds_file, self._creds)
        self.force_reconnect()
        return new_id

    # ---------------- internals ----------------

    def _set_state(self, new_state: str) -> None:
        prev = self._state
        if prev == new_state:
            return
        self._state = new_state
        snap = self.snapshot()
        for fn in list(self._status_subscribers):
            try:
                fn(snap)
            except Exception:
                logger.exception("status subscriber raised")

    def _run(self) -> None:
        """Main worker loop. Runs in a daemon thread."""
        while not self._stop.is_set():
            with self._lock:
                creds = self._creds
                state = self._state
                next_retry = self._next_retry_at

            if state == STATE_NEEDS_AUTH or creds is None:
                # Wait for the operator to log in; check periodically.
                self._stop.wait(1.0)
                continue

            if next_retry is not None and self._clock() < next_retry:
                self._stop.wait(0.5)
                continue

            try:
                self._connect_and_serve(creds)
            except Exception as e:
                logger.warning("relay connection failed: %s", e)
                with self._lock:
                    self._last_error = str(e)
                    delay = compute_backoff(self._attempt, self.backoff_schedule)
                    self._next_retry_at = self._clock() + delay
                    self._attempt += 1
                    self._set_state(STATE_BACKOFF)

    def _connect_and_serve(self, creds: AzureCredentials) -> None:
        """Open the Socket.IO connection and serve until disconnected.

        Acquires a fresh access token via msal (using the cached refresh token),
        connects to the relay's ``/pi`` namespace, sends ``meet_open``, then
        runs the event-pump loop:
          - drain self._queue and emit() each event
          - send periodic heartbeats
          - if no heartbeat ack within HEARTBEAT_DEGRADED_AFTER_S, mark degraded
          - on disconnect / exception, raise so the outer loop backs off

        For testability, the socketio client and msal app can be injected via
        the constructor's ``msal_app_factory`` and the protected
        ``_socketio_client_factory`` hook (overridden in tests).
        """
        if not self.relay_url:
            raise ConnectionError("relay_url is not configured")

        access_token = self._acquire_access_token(creds)
        client = self._socketio_client_factory()

        connected_evt = threading.Event()
        ack_evt = threading.Event()
        ack_evt.set()  # don't trip degraded immediately
        last_ack_time = [self._clock()]

        @client.event(namespace="/pi")  # type: ignore[misc]
        def connect() -> None:  # noqa: ARG001
            connected_evt.set()

        @client.event(namespace="/pi")  # type: ignore[misc]
        def heartbeat_ack(data: dict[str, Any]) -> None:  # noqa: ARG001
            last_ack_time[0] = self._clock()
            ack_evt.set()
            if isinstance(data, dict) and "active_client_count" in data:
                with self._lock:
                    self._active_client_count = int(data["active_client_count"])

        @client.event(namespace="/pi")  # type: ignore[misc]
        def disconnect() -> None:  # noqa: ARG001
            connected_evt.clear()

        # Connect with bearer token in the auth payload.
        client.connect(
            self.relay_url,
            namespaces=["/pi"],
            auth={
                "access_token": access_token,
                "meet_id": creds.meet_id,
                "protocol_version": self.protocol_version,
            },
            wait_timeout=10,
        )

        # Send meet_open handshake so Azure registers the meet.
        client.emit("meet_open", {
            "meet_id": creds.meet_id,
            "protocol_version": self.protocol_version,
        }, namespace="/pi")

        # Push the current template bundle, if a provider is registered. The
        # bundle is content-addressed; Azure will skip re-storing if the
        # bundle_id matches what it already has.
        bundle = self._bundle_provider() if self._bundle_provider else None
        if bundle is not None:
            client.emit("template_push", bundle, namespace="/pi")
            with self._lock:
                self._last_pushed_bundle_id = bundle.get("bundle_id")

        # Push the current render context (Phase 4) so Azure can render the
        # /m/{meet_id} page immediately on first browser hit.
        context = self._context_provider() if self._context_provider else None
        if context is not None:
            client.emit("meet_context", context, namespace="/pi")

        with self._lock:
            self._set_state(STATE_CONNECTED)
            self._last_connected_at = self._clock()
            self._last_heartbeat_at = self._clock()
            self._attempt = 0
            self._next_retry_at = None
            self._last_error = None

        last_heartbeat = self._clock()
        try:
            while not self._stop.is_set() and connected_evt.is_set():
                # Drain the outbound queue.
                try:
                    name, payload = self._queue.get(timeout=0.5)
                    if name != "__noop__":
                        client.emit(name, payload, namespace="/pi")
                except Empty:
                    pass

                now = self._clock()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                    client.emit("heartbeat", {"ts": now}, namespace="/pi")
                    last_heartbeat = now

                if now - last_ack_time[0] > HEARTBEAT_DEGRADED_AFTER_S:
                    with self._lock:
                        if self._state == STATE_CONNECTED:
                            self._set_state(STATE_DEGRADED)
                else:
                    with self._lock:
                        if self._state == STATE_DEGRADED:
                            self._set_state(STATE_CONNECTED)

                with self._lock:
                    self._last_heartbeat_at = now
        finally:
            try:
                client.disconnect()
            except Exception:
                pass
            with self._lock:
                if self._state in (STATE_CONNECTED, STATE_DEGRADED):
                    self._set_state(STATE_DISCONNECTED)

        # If we exited because the server closed us, raise so the outer loop
        # treats it as a disconnect that needs backoff.
        if not self._stop.is_set():
            raise ConnectionError("relay connection closed")

    def _acquire_access_token(self, creds: AzureCredentials) -> str:
        """Use the cached refresh token to mint a fresh access token."""
        if self._msal_factory is not None:
            app = self._msal_factory(client_id=creds.client_id, tenant_id=creds.tenant_id)
        else:
            from msal import PublicClientApplication

            authority = f"https://login.microsoftonline.com/{creds.tenant_id}"
            app = PublicClientApplication(creds.client_id, authority=authority)

        # acquire_token_by_refresh_token is the explicit refresh flow.
        result = app.acquire_token_by_refresh_token(
            refresh_token=creds.refresh_token,
            scopes=creds.scopes or [f"{creds.audience}/.default"],
        )
        if "access_token" not in result:
            err = result.get("error_description") or result.get("error") or "unknown"
            raise ConnectionError(f"refresh token rejected: {err}")
        return str(result["access_token"])

    def _socketio_client_factory(self):
        """Return a fresh socketio.Client. Override in tests."""
        import socketio  # type: ignore[import-not-found]

        return socketio.Client(reconnection=False, logger=False, engineio_logger=False)

