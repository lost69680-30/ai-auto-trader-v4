from pathlib import Path

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "android/app/src/main/java/com/aiautotrader/v4/MainActivity.kt"


def test_authenticated_market_polling_belongs_to_foreground_app_session():
    source = SOURCE.read_text(encoding="utf-8")
    trader_app = source.split("@Composable\nprivate fun TraderApp()", 1)[1].split(
        "@Composable\nprivate fun AgentNetworkScreen()", 1
    )[0]
    analysis_screen = source.split("@Composable\nprivate fun AnalysisScreen()", 1)[1].split(
        "@Composable\nprivate fun AnalyticsChartCard", 1
    )[0]

    assert "BackendMarketSessionEffect()" in trader_app
    assert "val activeToken = mutableStateOf(\"\")" in source
    assert "val appInForeground = mutableStateOf(false)" in source
    assert "override fun onStart()" in source
    assert "override fun onStop()" in source
    assert "LaunchedEffect(sessionToken, isForeground)" in source
    assert "delay(30_000)" in source
    assert "LaunchedEffect(sessionToken)" not in analysis_screen


def test_foreground_market_poller_retries_network_errors_but_stops_on_bad_auth():
    source = SOURCE.read_text(encoding="utf-8")
    poller = source.split("@Composable\nprivate fun BackendMarketSessionEffect()", 1)[1].split(
        "@Composable\nprivate fun TraderApp()", 1
    )[0]

    assert "BackendSession.activeToken.value" in poller
    assert "BackendSession.appInForeground.value" in poller
    assert "fetchMarketSnapshot(sessionToken)" in poller
    assert 'response.backendStatus == "UNAUTHORIZED"' in poller
    assert 'response.backendStatus == "UNSAFE RESPONSE"' in poller
    assert 'response.feedStatus == "HEALTHY"' in poller
    assert "BackendSession.marketItems.value = response.items" in poller
    assert 'statusJson.optString("mode") == "PAPER_ONLY"' in source
    assert 'statusJson.optBoolean("orders_authorized", true)' in source
