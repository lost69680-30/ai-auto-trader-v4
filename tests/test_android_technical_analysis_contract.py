from pathlib import Path

SOURCE = Path(__file__).parents[1] / "android/app/src/main/java/com/aiautotrader/v4/MainActivity.kt"

def test_technical_indicators_are_wired_to_validated_market_candles():
    source = SOURCE.read_text(encoding="utf-8")
    for required in ("private fun analyzeCandles", "ema(closes, 20)", "ema(closes, 50)", "RSI 14", "MACD 12/26/9", "ATR 14", "Support / resistance · 20 candles"):
        assert required in source

def test_analysis_remains_advisory_and_does_not_authorize_orders():
    source = SOURCE.read_text(encoding="utf-8")
    assert "HOLD · no approved strategy signal" in source
    assert "do not authorize orders" in source
    assert "orders_authorized" in source

def test_no_mock_market_values_are_added_by_analysis_patch():
    source = SOURCE.read_text(encoding="utf-8")
    assert "Clearly labelled illustrative values" not in source
    assert "fetchCandleHistory(sessionToken, selectedMarket.symbol)" in source
    assert "buildTimeframeSnapshots(candleHistoryByTimeframe)" in source
    assert "api/v1/markets/candles?symbol=" in source
    assert 'root.optString("mode") != "PAPER_ONLY"' in source
    assert "candlesForTimeframe" not in source
    assert "loaded directly from validated, closed backend candles" in source
    assert "CONFLICT · bullish and bearish timeframes disagree" in source
    assert "do not authorize orders" in source
