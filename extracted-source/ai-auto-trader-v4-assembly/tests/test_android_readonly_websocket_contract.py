from pathlib import Path

ROOT = Path(__file__).parents[1]
MAIN = ROOT / "android/app/src/main/java/com/aiautotrader/v4/MainActivity.kt"
CLIENT = ROOT / "android/app/src/main/java/com/aiautotrader/v4/MarketWebSocketClient.kt"
GRADLE = ROOT / "android/app/build.gradle.kts"


def test_android_websocket_client_uses_only_authenticated_tls_read_only_feed():
    source = CLIENT.read_text(encoding="utf-8")
    assert "marketWebSocketUrl(apiBaseUrl)" in source
    assert 'url.startsWith("wss://")' in source
    assert '.header("Authorization", "Bearer $bearerToken")' in source
    assert '"/ws/v1/markets"' in source
    assert 'private const val KEEPALIVE_MESSAGE = """{"type":"keepalive"}"""' in source
    assert "webSocket.send(KEEPALIVE_MESSAGE)" in source
    assert "webSocketReconnectDelayMs(reconnectAttempts)" in source
    assert "parseMarketSnapshotEvent(text)" in source
    assert "onUnauthorized" in source


def test_android_app_owns_websocket_only_while_authenticated_and_foregrounded():
    source = MAIN.read_text(encoding="utf-8")
    effect = source.split("@Composable\\nprivate fun BackendMarketWebSocketEffect()", 1)[1].split(
        "@Composable\\nprivate fun TraderApp()", 1
    )[0]
    trader_app = source.split("@Composable\\nprivate fun TraderApp()", 1)[1].split(
        "@Composable\\nprivate fun AgentNetworkScreen()", 1
    )[0]

    assert "BackendMarketWebSocketEffect()" in trader_app
    assert "BackendSession.activeToken.value" in effect
    assert "BackendSession.appInForeground.value" in effect
    assert "MarketWebSocketClient(" in effect
    assert "BuildConfig.PAPER_API_BASE_URL" in effect
    assert "stream.close()" in effect
    assert "awaitCancellation()" in effect


def test_stream_snapshots_update_only_validated_market_state_and_never_order_state():
    source = MAIN.read_text(encoding="utf-8")
    client = CLIENT.read_text(encoding="utf-8")
    for field in ("schema_version", "market.snapshot", "orders_authorized", "observed_at", "market_type"):
        assert field in client
    assert "parseMarketSnapshotEvent" in client
    assert "validateMarketDataContext" in client
    assert "BackendSession.marketItems.value" in source
    assert "BackendSession.orders" not in source
    assert "order" not in client.lower()


def test_websocket_dependency_is_pinned_for_android_and_local_tests():
    gradle = GRADLE.read_text(encoding="utf-8")
    assert 'implementation("com.squareup.okhttp3:okhttp:4.12.1")' in gradle
    assert 'testImplementation("org.json:json:20250517")' in gradle
