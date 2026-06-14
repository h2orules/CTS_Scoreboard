import wifi_manager


class TestIsSafeNmcliValue:
    def test_accepts_normal_ssid(self):
        assert wifi_manager._is_safe_nmcli_value("MyWiFi")

    def test_accepts_ssid_with_spaces_and_allowed_punctuation(self):
        assert wifi_manager._is_safe_nmcli_value("Home_Network 5G")
        assert wifi_manager._is_safe_nmcli_value("cafe-net.5g")

    def test_rejects_non_string(self):
        assert not wifi_manager._is_safe_nmcli_value(None)
        assert not wifi_manager._is_safe_nmcli_value(123)

    def test_rejects_empty(self):
        assert not wifi_manager._is_safe_nmcli_value("")

    def test_rejects_leading_dash_option_injection(self):
        assert not wifi_manager._is_safe_nmcli_value("-rf")
        assert not wifi_manager._is_safe_nmcli_value("--help")

    def test_rejects_control_characters(self):
        assert not wifi_manager._is_safe_nmcli_value("a\nb")
        assert not wifi_manager._is_safe_nmcli_value("a\rb")
        assert not wifi_manager._is_safe_nmcli_value("a\x00b")

    def test_rejects_shell_metacharacters(self):
        for bad in ("$(rm -rf)", "a & b", "a | b", "a; b", "a`b`", "a>b"):
            assert not wifi_manager._is_safe_nmcli_value(bad)

    def test_rejects_overlong_value(self):
        assert not wifi_manager._is_safe_nmcli_value("a" * 65)


class TestNormalizeSecret:
    def test_accepts_typical_password(self):
        assert wifi_manager._normalize_secret("Passw0rd!#$") == "Passw0rd!#$"

    def test_rejects_non_string(self):
        assert wifi_manager._normalize_secret(None) is None

    def test_rejects_control_characters(self):
        assert wifi_manager._normalize_secret("bad\nnewline") is None
        assert wifi_manager._normalize_secret("bad\x00nul") is None

    def test_rejects_leading_dash(self):
        assert wifi_manager._normalize_secret("-leading") is None

    def test_rejects_overlong_secret(self):
        assert wifi_manager._normalize_secret("a" * 129) is None
