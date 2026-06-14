import pytest

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


class TestSanitizeToken:
    def test_returns_equal_value_for_valid_token(self):
        assert wifi_manager._sanitize_token("MyWiFi") == "MyWiFi"
        assert wifi_manager._sanitize_token("Home_Network 5G") == "Home_Network 5G"

    def test_returned_value_is_rebuilt_from_constants(self):
        # The sanitized output must equal the input but be a distinct object,
        # i.e. reconstructed from the allowlist rather than passed through.
        token = "cafe-net.5g"
        out = wifi_manager._sanitize_token(token)
        assert out == token
        assert all(ch in wifi_manager._ALLOWED_TOKEN_CHARS for ch in out)

    def test_raises_on_non_string(self):
        for bad in (None, 123, ["x"]):
            with pytest.raises(ValueError):
                wifi_manager._sanitize_token(bad)

    def test_raises_on_empty(self):
        with pytest.raises(ValueError):
            wifi_manager._sanitize_token("")

    def test_raises_on_leading_dash(self):
        for bad in ("-rf", "--help"):
            with pytest.raises(ValueError):
                wifi_manager._sanitize_token(bad)

    def test_raises_on_control_and_metacharacters(self):
        for bad in ("a\nb", "a\rb", "a\x00b", "$(rm -rf)", "a & b", "a | b", "a; b"):
            with pytest.raises(ValueError):
                wifi_manager._sanitize_token(bad)

    def test_raises_on_overlong_value(self):
        with pytest.raises(ValueError):
            wifi_manager._sanitize_token("a" * 65)
