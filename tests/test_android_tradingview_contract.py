from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_tradingview_lightweight_charts_is_used_as_renderer_with_attribution():
    gradle = (ROOT / "android/app/build.gradle.kts").read_text(encoding="utf-8")
    activity = (
        ROOT / "android/app/src/main/java/com/aiautotrader/v4/MainActivity.kt"
    ).read_text(encoding="utf-8")
    chart = (
        ROOT / "android/app/src/main/java/com/aiautotrader/v4/TradingViewCandlestickChart.kt"
    ).read_text(encoding="utf-8")
    mapping = (
        ROOT / "android/app/src/main/java/com/aiautotrader/v4/TradingViewChartData.kt"
    ).read_text(encoding="utf-8")

    assert 'implementation("com.tradingview:lightweightcharts:5.2.1")' in gradle
    assert "TradingViewCandlestickChart(" in activity
    assert "ChartsView(context)" in chart
    assert "addCandlestickSeries" in chart
    assert "attributionLogo = true" in chart
    assert "TradingView Lightweight Charts™" in chart
    assert "https://www.tradingview.com/" in chart
    assert "Time.Utc(timestamp)" in mapping
    assert "OffsetDateTime.parse(candle.openTime)" in mapping
    assert "api.tradingview.com" not in chart.lower()
    assert "orders_authorized" not in chart.lower()


def test_tradingview_chart_keeps_custom_chart_as_webview_failure_fallback():
    activity = (
        ROOT / "android/app/src/main/java/com/aiautotrader/v4/MainActivity.kt"
    ).read_text(encoding="utf-8")
    chart = (
        ROOT / "android/app/src/main/java/com/aiautotrader/v4/TradingViewCandlestickChart.kt"
    ).read_text(encoding="utf-8")

    assert "FallbackCandlestickChart" in activity
    assert "TradingView unavailable · basic chart fallback" in chart
    assert "No validated candle data available" in chart
