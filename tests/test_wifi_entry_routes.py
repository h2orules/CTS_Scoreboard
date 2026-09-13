"""Captive entry and loopback-only kiosk integration; no real network control."""

import pytest

import CTS_Scoreboard as cts


@pytest.fixture
def status(monkeypatch):
    value = {
        "state": "setup",
        "setup_active": True,
        "available": True,
        "ap_ssid": "CTS-Scoreboard",
        "ap_network": "192.168.4.0/24",
        "setup_url": "http://192.168.4.1:5000/wifi/setup",
        "hostname": "pool-pi",
        "local_url": "http://pool-pi.local:5000/",
        "message": "Join CTS-Scoreboard to configure Wi-Fi.",
    }
    monkeypatch.setattr(cts.wifi_provisioning_client, "is_enabled", lambda: True)
    monkeypatch.setattr(
        cts.wifi_provisioning_client, "request",
        lambda command, **params: {"success": True, "status": value},
    )
    return value


@pytest.fixture
def client():
    with cts.app.test_client() as test_client:
        yield test_client


def test_root_enters_setup(client, status):
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"] == "/wifi/setup"
    assert response.headers["Cache-Control"] == "no-store"


def test_disabled_root_is_ordinary_site_map(client, monkeypatch):
    monkeypatch.setattr(cts.wifi_provisioning_client, "is_enabled", lambda: False)
    response = client.get("/")
    assert response.status_code == 200
    assert b"/web/kiosk" not in response.data


def test_normal_root_is_ordinary_site_map(client, status):
    status.update(state="normal", setup_active=False)
    assert client.get("/").status_code == 200


@pytest.mark.parametrize("path", ["/", "/login", "/generate_204", "/hotspot-detect.html"])
def test_captive_host_redirects_before_auth(client, status, path):
    response = client.get(
        path, base_url="http://captive.example",
        environ_overrides={"REMOTE_ADDR": "192.168.4.23"},
    )
    assert response.status_code == 302
    assert response.headers["Location"] == status["setup_url"]
    assert response.headers["Cache-Control"] == "no-store"
    assert "Set-Cookie" not in response.headers


def test_ap_port_80_redirects_to_canonical_port(client, status):
    response = client.get(
        "/login", base_url="http://192.168.4.1",
        environ_overrides={"REMOTE_ADDR": "192.168.4.23"},
    )
    assert response.headers["Location"] == status["setup_url"]


def test_canonical_login_does_not_loop(client, status):
    response = client.get(
        "/login", base_url="http://192.168.4.1:5000",
        environ_overrides={"REMOTE_ADDR": "192.168.4.23"},
    )
    assert response.status_code == 200


def test_canonical_unknown_path_enters_setup(client, status):
    response = client.get(
        "/anything", base_url="http://192.168.4.1:5000",
        environ_overrides={"REMOTE_ADDR": "192.168.4.23"},
    )
    assert response.headers["Location"] == status["setup_url"]


def test_foreign_host_post_cannot_mutate(client, status):
    response = client.post(
        "/login", base_url="http://captive.example",
        environ_overrides={"REMOTE_ADDR": "192.168.4.23"},
        data={"username": "admin", "password": "password"},
    )
    assert response.status_code == 400
    assert "Set-Cookie" not in response.headers


def test_wired_client_unknown_url_not_intercepted(client, status):
    response = client.get(
        "/generate_204", base_url="http://venue.example",
        environ_overrides={"REMOTE_ADDR": "10.0.0.23"},
    )
    assert response.status_code == 404


def test_normal_mode_does_not_intercept(client, status):
    status.update(state="normal", setup_active=False)
    response = client.get(
        "/generate_204", base_url="http://venue.example",
        environ_overrides={"REMOTE_ADDR": "192.168.4.23"},
    )
    assert response.status_code == 404


def test_bad_controller_portal_url_is_not_an_open_redirect(client, status):
    status["setup_url"] = "http://attacker.example/wifi/setup"
    response = client.get(
        "/", environ_overrides={"REMOTE_ADDR": "192.168.4.23"},
    )
    assert response.status_code == 503


@pytest.mark.parametrize("remote", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_loopback_kiosk_status(client, status, remote):
    response = client.get(
        "/wifi/kiosk-status", environ_overrides={"REMOTE_ADDR": remote},
    )
    assert response.status_code == 200
    assert response.json["status"]["state"] == "setup"
    assert response.headers["Cache-Control"] == "no-store"


def test_forwarded_header_does_not_allow_local_kiosk_access(client, status):
    response = client.get(
        "/wifi/kiosk-status",
        environ_overrides={"REMOTE_ADDR": "10.0.0.23"},
        headers={"X-Forwarded-For": "127.0.0.1", "Host": "localhost:5000"},
    )
    assert response.status_code == 403


def test_remote_display_redirects_to_authenticated_setup(client, status):
    response = client.get(
        "/wifi/display", environ_overrides={"REMOTE_ADDR": "10.0.0.23"},
    )
    assert response.headers["Location"] == "/wifi/setup"


def test_remote_kiosk_does_not_expose_local_wrapper(client, status):
    response = client.get(
        "/web/kiosk", environ_overrides={"REMOTE_ADDR": "10.0.0.23"},
    )
    assert response.headers["Location"] == "/web/home"


@pytest.mark.parametrize("display", [
    "https://attacker.example", "//attacker.example/web/home",
    "/web/kiosk", "/login", "/web/home\\x", "http://[broken",
    "/web/not-existing",
])
def test_kiosk_rejects_non_scoreboard_targets(client, status, display):
    response = client.get("/web/kiosk", query_string={"display": display})
    assert response.status_code == 400


def test_kiosk_preserves_local_scoreboard_query(client, status):
    response = client.get(
        "/web/kiosk", query_string={"display": "/web/home?event=2"},
    )
    assert response.status_code == 200
    assert b"/web/home?event=2" in response.data


def test_local_display_uses_offline_qr(client, status):
    response = client.get("/wifi/display")
    assert response.status_code == 200
    assert b"<svg" in response.data
    assert b"CTS-Scoreboard" in response.data


@pytest.mark.parametrize("state", ["joining", "refreshing", "error"])
def test_inactive_hotspot_display_does_not_invite_join(client, status, state):
    status.update(state=state, setup_active=False, target_ssid="Venue <pool>")
    response = client.get("/wifi/display")
    assert response.status_code == 200
    assert b"Join the setup network" not in response.data
    assert b"Venue &lt;pool&gt;" in response.data
    if state == "joining":
        assert b"The hotspot is off while the Pi joins" in response.data
    else:
        assert b"The setup hotspot is not active" in response.data


def test_service_failure_is_explicit_without_breaking_scoreboard(
    client, status, monkeypatch,
):
    def fail(command, **params):
        raise cts.wifi_provisioning_client.ProvisioningError("socket unavailable")

    monkeypatch.setattr(cts.wifi_provisioning_client, "request", fail)
    assert client.get("/").status_code == 503
    assert client.get("/wifi/kiosk-status").status_code == 503
    assert client.get("/wifi/display").status_code == 503
    assert client.get(
        "/web/home", environ_overrides={"REMOTE_ADDR": "10.0.0.23"},
    ).status_code == 200
