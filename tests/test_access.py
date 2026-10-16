"""Unit tests for bot.access — fail-secure access control."""

from bot.access import is_authorized


class TestIsAuthorized:
    def test_authorized_user_allowed(self):
        assert is_authorized(111, {111, 222}) is True

    def test_unauthorized_user_denied(self):
        assert is_authorized(333, {111, 222}) is False

    def test_empty_set_denies_everyone(self):
        assert is_authorized(111, set()) is False

    def test_none_override_denies(self):
        # None falls back to the module-level (config-loaded) set; with
        # no AUTHORIZED_USERS configured the bot denies everyone.
        assert is_authorized(999, None) is False

    def test_defaults_to_empty_when_no_config(self):
        assert is_authorized(1) is False
