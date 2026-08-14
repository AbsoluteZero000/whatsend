from app.config import TIMEZONE_CHOICES, settings
from app.main import app


def test_timezone_choices_include_complete_iana_catalog():
    assert TIMEZONE_CHOICES[0] == "UTC"
    assert len(TIMEZONE_CHOICES) > 500
    assert "Africa/Cairo" in TIMEZONE_CHOICES
    assert "America/New_York" in TIMEZONE_CHOICES
    assert "Asia/Tokyo" in TIMEZONE_CHOICES
    assert "Pacific/Auckland" in TIMEZONE_CHOICES
    assert len(TIMEZONE_CHOICES) == len(set(TIMEZONE_CHOICES))


def test_api_key_feature_is_disabled_by_default():
    assert settings.api_keys_enabled is False
    paths = {route.path for route in app.routes}
    assert "/api/send" not in paths
    assert "/api-keys" not in paths
