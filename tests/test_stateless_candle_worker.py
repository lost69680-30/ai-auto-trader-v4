import pytest

from backend.shared_market_worker import _env_bool


def test_raw_candle_archive_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("PAPER_CANDLE_ARCHIVE_ENABLED", raising=False)
    assert _env_bool("PAPER_CANDLE_ARCHIVE_ENABLED", default=False) is False


def test_raw_candle_archive_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("PAPER_CANDLE_ARCHIVE_ENABLED", "true")
    assert _env_bool("PAPER_CANDLE_ARCHIVE_ENABLED", default=False) is True


def test_raw_candle_archive_rejects_invalid_configuration(monkeypatch):
    monkeypatch.setenv("PAPER_CANDLE_ARCHIVE_ENABLED", "sometimes")
    with pytest.raises(ValueError, match="PAPER_CANDLE_ARCHIVE_ENABLED"):
        _env_bool("PAPER_CANDLE_ARCHIVE_ENABLED", default=False)
