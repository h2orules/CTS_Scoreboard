from __future__ import annotations

import wifi_manager


class Runner:
    def __init__(self):
        self.calls: list[tuple] = []
        self.responses: list[tuple[tuple[str, ...], object]] = []

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


class TestCommandConstruction:
    def test_scan_networks_uses_one_rescan_then_lists_with_rescan_no(self):
        runner = Runner()
        runner.add(("dev", "wifi", "rescan", "ifname", "wlan0"), (0, "", ""))
        runner.add(
            ("-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE", "dev", "wifi", "list", "ifname", "wlan0", "--rescan", "no"),
            (0, "Cafe WiFi:77:wpa2:*\n", ""),
        )

        networks = wifi_manager.scan_networks(interface="wlan0", runner=runner)

        assert networks == [{"ssid": "Cafe WiFi", "signal": 77, "security": "wpa2", "in_use": True}]
        assert runner.calls[0][0] == ("dev", "wifi", "rescan", "ifname", "wlan0")
        assert runner.calls[1][0][-2:] == ("--rescan", "no")

    def test_show_connection_preserves_colon_backslash_and_whitespace_ssid(self):
        runner = Runner()
        runner.add(
            (
                "-g",
                "connection.id,connection.uuid,connection.type,connection.autoconnect,connection.interface-name,802-11-wireless.ssid,802-11-wireless.mode",
                "con",
                "show",
                "uuid",
                "11111111-1111-1111-1111-111111111111",
            ),
            (
                0,
                "Profile Name\n"
                "11111111-1111-1111-1111-111111111111\n"
                "802-11-wireless\n"
                "yes\n"
                "wlan0\n"
                "Cafe:Guest\\Lobby  \n"
                "infrastructure\n",
                "",
            ),
        )

        info = wifi_manager.show_connection("11111111-1111-1111-1111-111111111111", runner=runner)

        assert info["name"] == "Profile Name"
        assert info["ssid"] == "Cafe:Guest\\Lobby  "
        assert runner.calls[0][0][:2] == ("--escape", "no")

    def test_add_wifi_profile_uses_atomic_wpa3_sae_profile(self):
        runner = Runner()
        runner.add(("con", "add", "type", "wifi"), (0, "added\n", ""))

        success, message = wifi_manager.add_wifi_profile(
            "cts-stage-test",
            "Pool WiFi",
            "wlan0",
            secret="secret",
            hidden=True,
            autoconnect=False,
            security="wpa3",
            managed_stage=True,
            runner=runner,
        )

        assert success is True
        assert message == "cts-stage-test"
        add_args = runner.calls[0][0]
        assert add_args[:4] == ("con", "add", "type", "wifi")
        assert ("connection.autoconnect", "no") in tuple(zip(add_args, add_args[1:]))
        assert ("wifi-sec.key-mgmt", "sae") in tuple(zip(add_args, add_args[1:]))
        assert ("wifi-sec.psk", "secret") in tuple(zip(add_args, add_args[1:]))
        assert ("user.data", "org.cts-scoreboard.role=staging") in tuple(zip(add_args, add_args[1:]))
