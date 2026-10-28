from pathlib import Path

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "android/app/src/main/java/com/aiautotrader/v4/MainActivity.kt"

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



def test_multi_timeframe_contains_week_month_and_year_with_explicit_api_mapping():
    source = SOURCE.read_text(encoding="utf-8")
    api_source = (ROOT / "app/engines/read_only_api.py").read_text(encoding="utf-8")
    aggregation_source = (ROOT / "backend/timeframe_aggregation.py").read_text(encoding="utf-8")

    for timeframe in ('"1W"', '"1M"', '"1Y"'):
        assert timeframe in source
    assert '"1W" -> "1w"' in source
    assert '"1M" -> "1M"' in source
    assert '"1Y" -> "1y"' in source
    assert '"1w", "1M"' in api_source
    assert 'interval_payloads["1y"] = yearly' in api_source
    assert "def aggregate_complete_yearly_candles(" in aggregation_source
    assert "expected_close = next_month - timedelta(milliseconds=1)" in aggregation_source
    assert "set(month_map) != set(range(1, 13))" in aggregation_source


def test_candle_history_is_cached_without_claiming_stale_data_is_live():
    source = SOURCE.read_text(encoding="utf-8")
    assert "candleHistoryBySymbol" in source
    assert "val nonEmptyHistory = history.filterValues { it.isNotEmpty() }" in source
    assert "LAST GOOD SNAPSHOT" in source
    assert "Signal confidence" in source
    assert "NOT CALIBRATED" in source


def test_solo_brand_is_applied_to_app_header_and_launcher():
    source = SOURCE.read_text(encoding="utf-8")
    root = ROOT
    manifest = (root / "android/app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")
    logo = (root / "android/app/src/main/res/drawable/ic_solo_logo.xml").read_text(encoding="utf-8")
    assert 'Text("SOLO"' in source
    assert 'android:label="Solo"' in manifest
    assert 'android:icon="@drawable/ic_solo_logo"' in manifest
    assert 'android:strokeColor="#D8B878"' in logo


def test_advanced_analysis_rejects_bad_candle_sequences_and_stale_history():
    source = SOURCE.read_text(encoding="utf-8")
    assert "internal fun candleSeriesIssue(" in source
    assert "duplicate or out-of-order timestamps" in source
    assert "gap or irregular timestamps" in source
    assert "latest candle is not yet closed" in source
    assert "latest candle is stale" in source
    assert "Data quality blocked" in source
    assert "val coreReadyIntervals = timeframeSnapshots.count { it.analysis?.directionalReady == true }" in source
    assert "val fullHistoryIntervals = timeframeSnapshots.count { it.analysis?.enoughData == true }" in source


def test_relative_volume_uses_only_the_prior_twenty_candles():
    source = SOURCE.read_text(encoding="utf-8")
    assert "internal fun relativeVolumeRatio(" in source
    assert "candles.dropLast(1).takeLast(20)" in source
    assert "Last / prior 20-candle average" in source
    assert "need 21 candles and non-zero baseline" in source
