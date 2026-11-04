const DEFAULT_SYMBOLS = [
  'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
  'ADAUSDT', 'DOGEUSDT', 'TRXUSDT', 'LINKUSDT', 'AVAXUSDT',
];

function json(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
  });
}

export function parseSymbols(raw) {
  if (raw == null || String(raw).trim() === '') return [...DEFAULT_SYMBOLS];
  const values = String(raw).split(',').map((value) => value.trim().toUpperCase());
  if (values.length > 30) throw new Error('maximum 30 symbols allowed');
  if (values.some((value) => !/^[A-Z0-9]{3,30}$/.test(value))) {
    throw new Error('invalid symbol allowlist');
  }
  const unique = [...new Set(values)];
  if (unique.length === 0) throw new Error('empty symbol allowlist');
  return unique;
}

function finitePositive(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

function finiteNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function buildMarketPayload(rows, symbols, now = Date.now()) {
  const allowed = new Set(symbols);
  const items = [];
  for (const row of Array.isArray(rows) ? rows : []) {
    if (!row || !allowed.has(row.symbol)) continue;
    const mark = finitePositive(row.markPrice);
    const index = finitePositive(row.indexPrice);
    const funding = finiteNumber(row.lastFundingRate);
    const observedAt = Number(row.time);
    const nextFunding = Number(row.nextFundingTime);
    const age = (now - observedAt) / 1000;
    if (!mark || !index || funding === null || !Number.isFinite(observedAt) ||
        age < -30 || age > 120 || !Number.isFinite(nextFunding) || nextFunding <= now) continue;
    items.push({
      symbol: row.symbol,
      status: 'HEALTHY',
      mark_price: mark,
      mark_observed_at: new Date(observedAt).toISOString(),
      market_type: 'FUTURES',
      price_type: 'FUTURES_MARK_PRICE',
      index_price: index,
      funding_rate: funding,
      next_funding_at: new Date(nextFunding).toISOString(),
      candles: [],
      source: 'BINANCE_USDM_TESTNET_REST',
      data_mode: 'POLLED_FALLBACK',
      orders_authorized: false,
    });
  }
  return {
    status: items.length ? 'DEGRADED' : 'UNAVAILABLE',
    items,
    checked_at: new Date(now).toISOString(),
    data_mode: 'POLLED_FALLBACK',
    reason: items.length
      ? 'Independent REST snapshot fallback; not a continuous WebSocket stream. Candles and ledger data remain on the primary service.'
      : 'No fresh, valid allowlisted Testnet market snapshots were available.',
    mode: 'PAPER_ONLY',
    orders_authorized: false,
  };
}

export function createHandler({ env, fetchImpl = fetch, now = () => Date.now() } = {}) {
  const readEnv = env ?? ((name) => Deno.env.get(name));
  return async function handle(request) {
    const url = new URL(request.url);
    if (url.pathname === '/healthz' && request.method === 'GET') {
      return json({ status: 'ok', service: 'solo-market-fallback', mode: 'PAPER_ONLY', orders_authorized: false });
    }
    if (url.pathname !== '/api/v1/markets') return json({ error: 'not_found', orders_authorized: false }, 404);
    if (request.method !== 'GET') return json({ error: 'method_not_allowed', orders_authorized: false }, 405);

    let symbols;
    let origin;
    try {
      symbols = parseSymbols(readEnv('SOLO_MARKET_SYMBOLS'));
      origin = new URL(readEnv('BINANCE_USDM_TESTNET_REST_ORIGIN') || 'https://demo-fapi.binance.com');
      if (origin.protocol !== 'https:' || origin.username || origin.password || origin.search || origin.hash || origin.pathname !== '/') {
        throw new Error('invalid Testnet REST origin');
      }
    } catch {
      return json({ status: 'UNAVAILABLE', items: [], orders_authorized: false }, 503);
    }

    try {
      const endpoint = new URL('/fapi/v1/premiumIndex', origin);
      const response = await fetchImpl(endpoint, {
        method: 'GET',
        headers: { accept: 'application/json' },
        redirect: 'error',
        signal: AbortSignal.timeout(8000),
      });
      if (!response.ok) throw new Error(`Testnet returned HTTP ${response.status}`);
      const rows = await response.json();
      const payload = buildMarketPayload(rows, symbols, now());
      return json(payload, payload.items.length ? 200 : 503);
    } catch {
      return json({
        status: 'UNAVAILABLE', items: [], data_mode: 'POLLED_FALLBACK',
        reason: 'Independent Testnet REST snapshot request failed or returned invalid data.',
        mode: 'PAPER_ONLY', orders_authorized: false,
      }, 503);
    }
  };
}

if (import.meta.main && typeof Deno !== 'undefined') {
  Deno.serve(createHandler());
}
