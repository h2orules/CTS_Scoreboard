import pytest

import wifi_manager


class Runner:
    def __init__(self):
        self.calls = []
        self.responses = []

    def add(self, prefix, response):
        self.responses.append((tuple(prefix), response))

    def __call__(self, args, timeout=30):
        self.calls.append((tuple(args), timeout))
        if args[:2] == ["--escape", "no"]:
            args = args[2:]
        for prefix, response in self.responses:
            if tuple(args[: len(prefix)]) == prefix:
                return response(args) if callable(response) else response
        return 0, "", ""


class TestValidationHelpers:
    def test_safe_nmcli_value_rejects_empty_control_and_leading_dash(self):
        assert wifi_manager._is_safe_nmcli_value("Pool WiFi")
        assert not wifi_manager._is_safe_nmcli_value("")
        assert not wifi_manager._is_safe_nmcli_value("line\nbreak")
        assert not wifi_manager._is_safe_nmcli_value("-option")

    def test_normalize_secret_allows_printable_passwords(self):
        assert wifi_manager._normalize_secret("Lead-With-Dash") == "Lead-With-Dash"
        assert wifi_manager._normalize_secret("bad\x00nul") is None
        assert wifi_manager._normalize_secret(None) is None

    def test_sanitize_token_rejects_shellish_characters(self):
        assert wifi_manager._sanitize_token("uuid-name 5g") == "uuid-name 5g"
        with pytest.raises(ValueError):
            wifi_manager._sanitize_token("bad;token")

    def test_normalize_ssid_preserves_whitespace_and_enforces_32_utf8_bytes(self):
        assert wifi_manager._normalize_ssid("  Lane 1 Guest  ") == "  Lane 1 Guest  "
        assert wifi_manager._normalize_ssid("é" * 16) == "é" * 16
        assert wifi_manager._normalize_ssid("é" * 17) is None


class TestProfileAndStatusQueries:
    def test_list_connection_profiles_resolves_real_ssid_and_active_uuid(self):
        runner = Runner()
        runner.add(
            ("-t", "-f", "UUID,TYPE,NAME,AUTOCONNECT,DEVICE", "con", "show"),
            (
                0,
                "11111111-1111-1111-1111-111111111111:802-11-wireless:Home Profile:yes:wlan0\n"
                "22222222-2222-2222-2222-222222222222:802-11-wireless:Meet AP:no:\n"
                "33333333-3333-3333-3333-333333333333:ethernet:Wired:yes:eth0\n",
                "",
            ),
        )
        runner.add(
            ("-t", "-f", "UUID,NAME,TYPE,DEVICE", "con", "show", "--active"),
            (0, "11111111-1111-1111-1111-111111111111:Home Profile:802-11-wireless:wlan0\n", ""),
        )
        runner.add(
            (
                "-g",
                "connection.id,connection.uuid,connection.type,connection.autoconnect,connection.interface-name,802-11-wireless.ssid,802-11-wireless.mode",
                "con",
                "show",
                "uuid",
                "11111111-1111-1111-1111-111111111111",
            ),
            (0, "Home Profile\n11111111-1111-1111-1111-111111111111\n802-11-wireless\nyes\nwlan0\nHome WiFi\ninfrastructure\n", ""),
        )
        runner.add(
            (
                "-g",
                "connection.id,connection.uuid,connection.type,connection.autoconnect,connection.interface-name,802-11-wireless.ssid,802-11-wireless.mode",
                "con",
                "show",
                "uuid",
                "22222222-2222-2222-2222-222222222222",
            ),
            (0, "Meet AP\n22222222-2222-2222-2222-222222222222\n802-11-wireless\nno\n\nCTS-Scoreboard\nap\n", ""),
        )

        profiles = wifi_manager.list_connection_profiles(runner=runner)

        assert profiles == [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "ssid": "Home WiFi",
                "name": "Home Profile",
                "type": "802-11-wireless",
                "autoconnect": True,
                "device": "wlan0",
                "active": True,
                "mode": "infrastructure",
            },
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "ssid": "CTS-Scoreboard",
                "name": "Meet AP",
                "type": "802-11-wireless",
                "autoconnect": False,
                "device": None,
                "active": False,
                "mode": "ap",
            },
        ]

    def test_get_saved_networks_deduplicates_real_ssids(self):
        runner = Runner()
        runner.add(
            ("-t", "-f", "UUID,TYPE,NAME,AUTOCONNECT,DEVICE", "con", "show"),
            (
                0,
                "11111111-1111-1111-1111-111111111111:802-11-wireless:Home Profile:yes:wlan0\n"
                "44444444-4444-4444-4444-444444444444:802-11-wireless:Backup Profile:no:\n",
                "",
            ),
        )
        runner.add(("-t", "-f", "UUID,NAME,TYPE,DEVICE", "con", "show", "--active"), (0, "", ""))
        runner.add(
            (
                "-g",
                "connection.id,connection.uuid,connection.type,connection.autoconnect,connection.interface-name,802-11-wireless.ssid,802-11-wireless.mode",
                "con",
                "show",
                "uuid",
                "11111111-1111-1111-1111-111111111111",
            ),
            (0, "Home Profile\n11111111-1111-1111-1111-111111111111\n802-11-wireless\nyes\nwlan0\nHome WiFi\ninfrastructure\n", ""),
        )
        runner.add(
            (
                "-g",
                "connection.id,connection.uuid,connection.type,connection.autoconnect,connection.interface-name,802-11-wireless.ssid,802-11-wireless.mode",
                "con",
                "show",
                "uuid",
                "44444444-4444-4444-4444-444444444444",
            ),
            (0, "Backup Profile\n44444444-4444-4444-4444-444444444444\n802-11-wireless\nno\n\nHome WiFi\ninfrastructure\n", ""),
        )

        assert wifi_manager.get_saved_networks(runner=runner) == ["Home WiFi"]

    def test_get_status_uses_profile_ssid_and_suppresses_link_local_ethernet(self):
        runner = Runner()
        runner.add(
            ("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev", "status"),
            (0, "wlan0:wifi:connected:Home Profile\neth0:ethernet:connected:Wired\n", ""),
        )
        runner.add(
            ("-t", "-f", "UUID,NAME,TYPE,DEVICE", "con", "show", "--active"),
            (
                0,
                "11111111-1111-1111-1111-111111111111:Home Profile:802-11-wireless:wlan0\n"
                "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee:Wired:ethernet:eth0\n",
                "",
            ),
        )
        runner.add(
            (
                "-g",
                "connection.id,connection.uuid,connection.type,connection.autoconnect,connection.interface-name,802-11-wireless.ssid,802-11-wireless.mode",
                "con",
                "show",
                "uuid",
                "11111111-1111-1111-1111-111111111111",
            ),
            (0, "Home Profile\n11111111-1111-1111-1111-111111111111\n802-11-wireless\nyes\nwlan0\nHome WiFi\ninfrastructure\n", ""),
        )
        runner.add(("-t", "-f", "IN-USE,SIGNAL", "dev", "wifi", "list"), (0, "*:78\n", ""))
        runner.add(("-t", "-f", "IP4.ADDRESS", "dev", "show", "eth0"), (0, "IP4.ADDRESS[1]:169.254.23.11/16\n", ""))

        status = wifi_manager.get_status(runner=runner)

        assert status == {"wifi_ssid": "Home WiFi", "wifi_signal": 78, "ethernet": False}


class TestMutations:
    def test_password_update_preserves_wpa3_sae(self):
        runner = Runner()
        runner.add(("-g", "802-11-wireless-security.key-mgmt"), (0, "sae\n", ""))
        success, _ = wifi_manager.update_password("profile", "new secret", runner=runner)
        assert success
        assert "sae" in runner.calls[-1][0]
        assert "wpa-psk" not in runner.calls[-1][0]

    def test_password_update_rejects_enterprise_profile(self):
        runner = Runner()
        runner.add(("-g", "802-11-wireless-security.key-mgmt"), (0, "wpa-eap\n", ""))
        success, message = wifi_manager.update_password("profile", "new secret", runner=runner)
        assert not success
        assert "personal WPA" in message
        assert len(runner.calls) == 1

    def test_connect_saved_uuid_activates_by_uuid(self):
        runner = Runner()

        success, message = wifi_manager.connect(
            "11111111-1111-1111-1111-111111111111",
            saved=True,
            runner=runner,
        )

        assert success is True
        assert message == "Connected to 11111111-1111-1111-1111-111111111111"
        assert runner.calls[-1][0] == (
            "con",
            "up",
            "uuid",
            "11111111-1111-1111-1111-111111111111",
        )

    def test_connect_saved_ssid_resolves_matching_profile_uuid(self):
        runner = Runner()
        runner.add(
            ("-t", "-f", "UUID,TYPE,NAME,AUTOCONNECT,DEVICE", "con", "show"),
            (0, "11111111-1111-1111-1111-111111111111:802-11-wireless:Profile:no:wlan0\n", ""),
        )
        runner.add(
            ("-t", "-f", "UUID,NAME,TYPE,DEVICE", "con", "show", "--active"),
            (0, "", ""),
        )
        runner.add(
            (
                "-g",
                "connection.id,connection.uuid,connection.type,connection.autoconnect,connection.interface-name,802-11-wireless.ssid,802-11-wireless.mode",
                "con",
                "show",
                "uuid",
                "11111111-1111-1111-1111-111111111111",
            ),
            (0, "Profile\n11111111-1111-1111-1111-111111111111\n802-11-wireless\nno\nwlan0\n  Pool WiFi  \ninfrastructure\n", ""),
        )

        success, message = wifi_manager.connect("  Pool WiFi  ", saved=True, runner=runner)

        assert success is True
        assert message == "Connected to   Pool WiFi  "
        assert runner.calls[-1][0] == (
            "con",
            "up",
            "uuid",
            "11111111-1111-1111-1111-111111111111",
        )

    def test_connect_hidden_open_network_uses_direct_wifi_command(self):
        runner = Runner()

        success, message = wifi_manager.connect(
            "Guest WiFi",
            hidden=True,
            saved=False,
            runner=runner,
        )

        assert success is True
        assert message == "Connected to Guest WiFi"
        assert runner.calls[-1][0] == (
            "dev",
            "wifi",
            "connect",
            "Guest WiFi",
            "hidden",
            "yes",
        )

    def test_add_wifi_profile_creates_atomically_without_partial_secret_profile(self):
        runner = Runner()
        runner.add(("con", "add", "type", "wifi"), (10, "", "bad settings including secret"))

        success, message = wifi_manager.add_wifi_profile(
            "cts-stage-test",
            "Pool WiFi",
            "wlan0",
            secret="secret",
            hidden=True,
            runner=runner,
        )

        assert success is False
        assert "Failed to create" in message
        assert "secret" not in message
        assert len(runner.calls) == 1
        assert "wifi-sec.psk" in runner.calls[0][0]

    def test_activate_connection_uses_uuid_selector(self):
        runner = Runner()

        success, message = wifi_manager.activate_connection(
            "11111111-1111-1111-1111-111111111111",
            interface="wlan0",
            runner=runner,
        )

        assert success is True
        assert message.startswith("Activated")
        assert runner.calls[0][0] == (
            "con",
            "up",
            "uuid",
            "11111111-1111-1111-1111-111111111111",
            "ifname",
            "wlan0",
        )

    def test_set_device_autoconnect_updates_interface_policy(self):
        runner = Runner()

        success, message = wifi_manager.set_device_autoconnect("wlan0", False, runner=runner)

        assert success is True
        assert message == "Updated device autoconnect for wlan0"
        assert runner.calls[0][0] == ("device", "set", "wlan0", "autoconnect", "no")
