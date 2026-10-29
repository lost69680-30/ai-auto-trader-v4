import pytest

from backend.asgi_runtime import websocket_enabled


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE "])
def test_websocket_flag_accepts_explicit_true_values(value):
    assert websocket_enabled({"PAPER_WEBSOCKET_ENABLED": value}) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_websocket_flag_accepts_explicit_false_values(value):
    assert websocket_enabled({"PAPER_WEBSOCKET_ENABLED": value}) is False


def test_websocket_flag_is_disabled_when_not_configured():
    assert websocket_enabled({}) is False


@pytest.mark.parametrize("value", ["enabled", "maybe", "2", "treu"])
def test_websocket_flag_rejects_malformed_values(value):
    with pytest.raises(ValueError, match="PAPER_WEBSOCKET_ENABLED"):
        websocket_enabled({"PAPER_WEBSOCKET_ENABLED": value})
