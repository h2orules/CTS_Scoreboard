import pytest

import credentials_store
import CTS_Scoreboard as cts
import settings_routes
import wifi_manager


class FakeProvisioningClient:
    class ProvisioningError(Exception):
        pass

    def __init__(self, responses=None, enabled=True):
        self.enabled = enabled
        self.responses = responses or {}
        self.calls = []

    def is_enabled(self):
        return self.enabled

    def request(self, command, **params):
        self.calls.append((command, params))
        response = self.responses.get(command)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(command, params)
        if response is not None:
            return response
        return {"success": True, "message": "ok"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(wifi_manager, "get_saved_networks", lambda: [])
    monkeypatch.setattr(
        wifi_manager, "get_status",
        lambda: {"wifi_ssid": None, "wifi_signal": None, "ethernet": False},
    )
    monkeypatch.setattr(wifi_manager, "is_available", lambda: False)
    cts.app.config["TESTING"] = True
    with cts.app.test_client() as client:
        yield client


@pytest.fixture
def logged_in_client(client):
    client.post(
        "/login",
        data={
            "username": credentials_store.DEFAULT_USERNAME,
            "password": credentials_store.DEFAULT_PASSWORD,
        },
    )
    return client


def _set_wifi_csrf(client, token="csrf-test-token"):
    with client.session_transaction() as session:
        session["wifi_csrf_token"] = token
    return token


def test_login_and_setup_pages_do_not_require_external_assets(client, logged_in_client, monkeypatch):
    monkeypatch.setattr(settings_routes, "_wifi_get_client", lambda: None)

    login_html = client.get("/login").data.decode()
    assert "maxcdn.bootstrapcdn.com" not in login_html
    assert "ajax.googleapis.com" not in login_html
    assert "css/login.css" in login_html

    setup_html = logged_in_client.get("/wifi/setup").data.decode()
    assert "maxcdn.bootstrapcdn.com" not in setup_html
    assert "ajax.googleapis.com" not in setup_html
    assert "css/wifi.css" in setup_html
    assert "Wi-Fi provisioning is not enabled" in setup_html


def test_disabled_controller_uses_legacy_wifi_manager(logged_in_client, monkeypatch):
    monkeypatch.setattr(settings_routes, "_wifi_get_client", lambda: None)
    calls = {}

    def connect(ssid, password, **options):
        calls["connect"] = (ssid, password, options)
        return True, "legacy connected"

    monkeypatch.setattr(wifi_manager, "connect", connect)
    token = _set_wifi_csrf(logged_in_client)
    response = logged_in_client.post(
        "/wifi/connect",
        json={"ssid": "Home WiFi", "password": "secret"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    assert response.json == {"success": True, "message": "legacy connected"}
    assert calls["connect"] == (
        "Home WiFi", "secret", {"hidden": False, "saved": False},
    )


def test_controller_connect_prepares_join_and_requires_csrf(logged_in_client, monkeypatch):
    fake = FakeProvisioningClient(
        responses={
            "status": {
                "success": True,
                "message": "ready",
                "status": {
                    "state": "normal",
                    "wifi_ssid": "Home WiFi",
                    "local_url": "http://pool.local:5000/",
                    "setup_url": "http://192.168.4.1:5000/wifi/setup",
                    "ap_ssid": "CTS-Scoreboard",
                    "available": True,
                },
            },
            "prepare_join": {
                "success": True,
                "message": "Prepare complete",
                "operation_id": "op-123",
                "status": {
                    "state": "join_pending",
                    "operation_id": "op-123",
                    "wifi_ssid": "Home WiFi",
                    "local_url": "http://pool.local:5000/",
                    "setup_url": "http://192.168.4.1:5000/wifi/setup",
                    "ap_ssid": "CTS-Scoreboard",
                    "setup_active": True,
                },
            },
        }
    )
    monkeypatch.setattr(settings_routes, "_wifi_get_client", lambda: fake)
    token = _set_wifi_csrf(logged_in_client)

    response = logged_in_client.post(
        "/wifi/connect",
        json={"ssid": "Home WiFi", "password": "secret", "hidden": False, "security": "wpa2"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    assert response.json["success"] is True
    assert response.json["operation_id"] == "op-123"
    assert fake.calls[0][0] == "prepare_join"
    assert fake.calls[0][1]["ssid"] == "Home WiFi"
    assert fake.calls[0][1]["password"] == "secret"

    forbidden = logged_in_client.post(
        "/wifi/forget",
        json={"ssid": "Home WiFi"},
    )
    assert forbidden.status_code == 403


def test_setup_page_preserves_handoff_instructions_when_busy(logged_in_client, monkeypatch):
    fake = FakeProvisioningClient(
        responses={
            "status": {
                "success": True,
                "message": "Join in progress",
                "status": {
                    "state": "joining",
                    "operation_id": "op-9",
                    "wifi_ssid": "Home WiFi",
                    "local_url": "http://pool.local:5000/",
                    "setup_url": "http://192.168.4.1:5000/wifi/setup",
                    "ap_ssid": "CTS-Scoreboard",
                    "setup_active": True,
                },
            },
            "scan": {
                "success": True,
                "networks": [
                    {"ssid": "Home WiFi", "signal": 88, "security": "wpa2", "in_use": True}
                ],
                "saved": ["Home WiFi"],
                "profiles": [{"id": "123e4567-e89b-12d3-a456-426614174000", "ssid": "Home WiFi"}],
                "status": {
                    "state": "joining",
                    "operation_id": "op-9",
                    "wifi_ssid": "Home WiFi",
                    "local_url": "http://pool.local:5000/",
                    "setup_url": "http://192.168.4.1:5000/wifi/setup",
                    "ap_ssid": "CTS-Scoreboard",
                    "setup_active": True,
                },
            },
        }
    )
    monkeypatch.setattr(settings_routes, "_wifi_get_client", lambda: fake)

    html = logged_in_client.get("/wifi/setup").data.decode()
    assert "http://pool.local:5000/" in html
    assert "http://192.168.4.1:5000/wifi/setup" in html
    assert "Commit join" in html
    assert "Connected to" not in html


def test_transport_failures_return_503(logged_in_client, monkeypatch):
    fake = FakeProvisioningClient(
        responses={
            "status": FakeProvisioningClient.ProvisioningError("socket unavailable"),
            "scan": FakeProvisioningClient.ProvisioningError("socket unavailable"),
        }
    )
    monkeypatch.setattr(settings_routes, "_wifi_get_client", lambda: fake)

    assert logged_in_client.get("/settings").status_code == 503
    assert logged_in_client.get("/wifi/setup").status_code == 503


@pytest.mark.parametrize("params", [
    {"ssid": ""},
    {"ssid": "a" * 33},
    {"ssid": "\u00e9" * 17},
    {"ssid": "a\nb"},
    {"ssid": "home", "hidden": "false"},
    {"ssid": "home", "hidden": []},
    {"ssid": "home", "security": {}},
    {"ssid": "home", "security": "enterprise"},
    {"ssid": "home", "password": []},
    {"ssid": "home", "profile_id": False},
    {"ssid": "home", "profile_id": "not-a-uuid"},
    {"ssid": "home", "ap_network": "10.1.1.0/24"},
])
def test_join_validation_precedes_controller(
    logged_in_client, monkeypatch, params,
):
    fake = FakeProvisioningClient()
    monkeypatch.setattr(settings_routes, "_wifi_get_client", lambda: fake)
    token = _set_wifi_csrf(logged_in_client)
    response = logged_in_client.post(
        "/wifi/prepare", json=params, headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 400
    assert fake.calls == []


def test_join_preserves_ssid_whitespace(logged_in_client, monkeypatch):
    fake = FakeProvisioningClient()
    monkeypatch.setattr(settings_routes, "_wifi_get_client", lambda: fake)
    token = _set_wifi_csrf(logged_in_client)
    response = logged_in_client.post(
        "/wifi/connect", json={"ssid": " Home ", "password": "test password"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    assert fake.calls[0][1]["ssid"] == " Home "


@pytest.mark.parametrize("confirmed", ["true", "false", 1, {}, False, None])
def test_refresh_requires_explicit_boolean(
    logged_in_client, monkeypatch, confirmed,
):
    fake = FakeProvisioningClient()
    monkeypatch.setattr(settings_routes, "_wifi_get_client", lambda: fake)
    token = _set_wifi_csrf(logged_in_client)
    response = logged_in_client.post(
        "/wifi/refresh", json={"confirmed": confirmed},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 400
    assert fake.calls == []


def test_controller_rejection_is_not_success(logged_in_client, monkeypatch):
    fake = FakeProvisioningClient(responses={
        "prepare_join": {"success": False, "message": "Controller is busy"},
    })
    monkeypatch.setattr(settings_routes, "_wifi_get_client", lambda: fake)
    token = _set_wifi_csrf(logged_in_client)
    response = logged_in_client.post(
        "/wifi/connect", json={"ssid": "home"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 409
    assert response.json["success"] is False
    assert response.json["message"] == "Controller is busy"


@pytest.mark.parametrize("payload", [
    {}, {"success": "yes"}, {"success": True, "status": []},
])
def test_invalid_controller_status_is_explicit(
    logged_in_client, monkeypatch, payload,
):
    fake = FakeProvisioningClient(responses={"status": payload})
    monkeypatch.setattr(settings_routes, "_wifi_get_client", lambda: fake)
    assert logged_in_client.get("/wifi/status").status_code == 503


@pytest.mark.parametrize("endpoint", [
    "connect", "prepare", "commit", "cancel", "refresh", "retry",
    "forget", "update_password",
])
def test_wifi_mutations_require_login(client, endpoint):
    response = client.post("/wifi/" + endpoint, json={})
    assert response.status_code in (302, 401)


@pytest.mark.parametrize("endpoint", [
    "connect", "prepare", "commit", "cancel", "refresh", "retry",
    "forget", "update_password",
])
def test_wifi_mutations_require_csrf(logged_in_client, monkeypatch, endpoint):
    fake = FakeProvisioningClient()
    monkeypatch.setattr(settings_routes, "_wifi_get_client", lambda: fake)
    response = logged_in_client.post("/wifi/" + endpoint, json={})
    assert response.status_code == 403
    assert fake.calls == []


def test_legacy_open_hidden_connect_uses_new_profile(logged_in_client, monkeypatch):
    monkeypatch.setattr(settings_routes, "_wifi_get_client", lambda: None)
    calls = []
    monkeypatch.setattr(
        wifi_manager, "connect",
        lambda *args, **kwargs: (calls.append((args, kwargs)) or (True, "connected")),
    )
    token = _set_wifi_csrf(logged_in_client)
    response = logged_in_client.post(
        "/wifi/connect", json={"ssid": "Hidden Guest", "security": "open", "hidden": True},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    assert calls == [(("Hidden Guest", None), {"hidden": True, "saved": False})]


def test_legacy_saved_uuid_is_not_replaced_by_ssid(logged_in_client, monkeypatch):
    monkeypatch.setattr(settings_routes, "_wifi_get_client", lambda: None)
    calls = []
    monkeypatch.setattr(
        wifi_manager, "connect",
        lambda *args, **kwargs: (calls.append((args, kwargs)) or (True, "connected")),
    )
    token = _set_wifi_csrf(logged_in_client)
    profile_id = "123e4567-e89b-12d3-a456-426614174000"
    response = logged_in_client.post(
        "/wifi/connect", json={"ssid": "Home", "profile_id": profile_id},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    assert calls == [((profile_id, None), {"hidden": False, "saved": True})]


def test_non_ascii_csrf_token_is_rejected_not_server_error(logged_in_client):
    _set_wifi_csrf(logged_in_client)
    response = logged_in_client.post(
        "/wifi/connect", json={"ssid": "Home"},
        headers={"X-CSRF-Token": "\u00e9"},
    )
    assert response.status_code == 403
