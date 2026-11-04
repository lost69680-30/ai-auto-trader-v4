const READ_ONLY_PATHS = new Set([
  '/api/v1/status',
  '/api/v1/positions',
  '/api/v1/orders',
  '/api/v1/fills',
  '/api/v1/risk',
  '/api/v1/audit',
  '/api/v1/funding',
  '/api/v1/markets',
  '/api/v1/markets/candles',
  '/api/v1/markets/history',
  '/api/v1/markets/assessment',
  '/api/v1/opportunities',
  '/api/v1/binance-spot-testnet/account',
]);

function isAllowedReadPath(path) {
  return READ_ONLY_PATHS.has(path) ||
    /^\/api\/v1\/opportunities\/[A-Za-z0-9_-]+(?:\/(?:events|assessments|evidence))?$/.test(path);
}

function json(value, status = 200, headers = {}) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', ...headers },
  });
}

function configuredOrigin(value) {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || url.pathname !== '/') return null;
    return url.origin;
  } catch {
    return null;
  }
}

function maxAgeSeconds(env) {
  const raw = Number(env.MAX_MARKET_AGE_SECONDS ?? 120);
  return Number.isInteger(raw) && raw >= 30 && raw <= 300 ? raw : 120;
}

function validMarketItem(item, now, maxAge) {
  if (!item || typeof item !== 'object' || item.orders_authorized !== false || item.status !== 'HEALTHY') return false;
  if (typeof item.symbol !== 'string' || !/^[A-Z0-9]{3,30}$/.test(item.symbol)) return false;
  if (typeof item.mark_price !== 'number' || !Number.isFinite(item.mark_price) || item.mark_price <= 0) return false;
  if (item.market_type !== 'FUTURES' || item.price_type !== 'FUTURES_MARK_PRICE') return false;
  if (typeof item.index_price !== 'number' || !Number.isFinite(item.index_price) || item.index_price <= 0) return false;
  if (typeof item.funding_rate !== 'number' || !Number.isFinite(item.funding_rate)) return false;
  const observed = Date.parse(item.mark_observed_at);
  const funding = Date.parse(item.next_funding_at);
  const age = (now - observed) / 1000;
  return Number.isFinite(observed) && age >= -30 && age <= maxAge &&
    Number.isFinite(funding) && funding > now && (item.candles == null || Array.isArray(item.candles));
}

function marketPayloadExplicitlyUnsafe(payload) {
  return !payload || typeof payload !== 'object' || payload.orders_authorized !== false ||
    !Array.isArray(payload.items) ||
    payload.items.some((item) => !item || typeof item !== 'object' || item.orders_authorized !== false);
}

function validMarketPayload(payload, now, maxAge) {
  return Boolean(payload && typeof payload === 'object' &&
    ['HEALTHY', 'DEGRADED'].includes(payload.status) && payload.orders_authorized === false &&
    Array.isArray(payload.items) && payload.items.length > 0 && payload.items.length <= 30 &&
    payload.items.every((item) => validMarketItem(item, now, maxAge)));
}

async function readLimitedBody(request, maxBytes) {
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > maxBytes) {
      await reader.cancel();
      throw new Error('request body too large');
    }
    chunks.push(value);
  }
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
  return body;
}

async function readJson(response) {
  const text = await response.text();
  if (text.length > 2_000_000) throw new Error('upstream response too large');
  return JSON.parse(text);
}

async function upstreamFetch(fetchImpl, origin, request, method, body = undefined) {
  const target = new URL(request.url);
  const upstream = new URL(target.pathname + target.search, origin);
  const headers = new Headers();
  const authorization = request.headers.get('authorization');
  if (authorization) headers.set('authorization', authorization);
  headers.set('accept', 'application/json');
  const contentType = request.headers.get('content-type');
  if (contentType && method === 'POST') headers.set('content-type', contentType);
  return fetchImpl(upstream.toString(), {
    method,
    headers,
    body,
    redirect: 'manual',
  });
}

async function fetchSecondaryMarkets(fetchImpl, secondary, token, now, ageLimit) {
  if (!secondary || typeof token !== 'string' || token.length < 32) return null;
  try {
    const fallbackUrl = new URL('/api/v1/markets', secondary);
    const response = await fetchImpl(fallbackUrl, { method: 'GET', headers: { accept: 'application/json', authorization: `Bearer ${token}` }, redirect: 'manual' });
    if (!response.ok || (response.status >= 300 && response.status < 400)) return null;
    const payload = await readJson(response);
    if (validMarketPayload(payload, now, ageLimit) && payload.data_mode === 'POLLED_FALLBACK') return payload;
  } catch { /* safe unavailable response below */ }
  return null;
}

export async function handleRequest(request, env, dependencies = {}) {
  const fetchImpl = dependencies.fetchImpl ?? fetch;
  const now = dependencies.now ?? (() => Date.now());
  const url = new URL(request.url);
  const path = url.pathname;
  const primary = configuredOrigin(env.PRIMARY_API_ORIGIN);
  const secondary = configuredOrigin(env.SECONDARY_MARKET_ORIGIN);

  if (path === '/healthz' && request.method === 'GET') {
    return json({ status: 'ok', service: 'solo-edge', mode: 'PAPER_ONLY', orders_authorized: false });
  }

  if (path === '/readyz' && request.method === 'GET') {
    if (!primary) return json({ status: 'not_ready', primary: 'NOT_CONFIGURED', orders_authorized: false }, 503);
    let primaryReady = false;
    let secondaryReady = false;
    try {
      const response = await fetchImpl(new URL('/readyz', primary), { method: 'GET', redirect: 'manual' });
      primaryReady = response.status === 200;
    } catch { /* fail closed below */ }
    if (secondary && typeof env.SECONDARY_MARKET_TOKEN === 'string' && env.SECONDARY_MARKET_TOKEN.length >= 32) {
      try {
        const response = await fetchImpl(new URL('/readyz', secondary), { method: 'GET', headers: { authorization: `Bearer ${env.SECONDARY_MARKET_TOKEN}` }, redirect: 'manual' });
        secondaryReady = response.status === 200;
      } catch { /* fail closed below */ }
    }
    const status = primaryReady ? 'ready' : secondaryReady ? 'degraded' : 'not_ready';
    return json({
      status,
      primary: primaryReady ? 'READY' : 'UNAVAILABLE',
      market_fallback: secondaryReady ? 'READY' : 'UNAVAILABLE',
      orders_authorized: false,
    }, primaryReady ? 200 : 503);
  }

  if (path === '/ws/v1/markets') {
    if (request.method !== 'GET' || request.headers.get('upgrade')?.toLowerCase() !== 'websocket') {
      return json({ error: 'websocket_upgrade_required', orders_authorized: false }, 426);
    }
    if (!request.headers.get('authorization')?.startsWith('Bearer ')) {
      return json({ error: 'unauthorized', orders_authorized: false }, 401);
    }
    if (!primary) return json({ error: 'primary_unavailable', orders_authorized: false }, 503);
    try {
      const target = new URL(url.pathname + url.search, primary);
      const headers = new Headers();
      for (const name of ['authorization', 'upgrade', 'sec-websocket-key', 'sec-websocket-version', 'sec-websocket-protocol', 'sec-websocket-extensions']) {
        const value = request.headers.get(name);
        if (value) headers.set(name, value);
      }
      headers.set('upgrade', 'websocket');
      headers.set('accept', 'application/json');
      const upstreamRequest = new Request(target, { method: 'GET', headers, redirect: 'manual' });
      const response = await fetchImpl(upstreamRequest);
      if (response.status >= 300 && response.status < 400) {
        return json({ error: 'upstream_redirect_rejected', orders_authorized: false }, 502);
      }
      return response;
    } catch {
      // WebSocket streams are never routed to the REST fallback provider.
      return json({ error: 'primary_websocket_unavailable', orders_authorized: false }, 503);
    }
  }

  if (path === '/api/v1/status' && request.method === 'GET') {
    if (!request.headers.get('authorization')?.startsWith('Bearer ')) {
      return json({ error: 'unauthorized', orders_authorized: false }, 401);
    }
    if (!primary) return json({ error: 'primary_unavailable', orders_authorized: false }, 503);
    try {
      const response = await upstreamFetch(fetchImpl, primary, request, 'GET');
      if (response.status >= 300 && response.status < 400) {
        return json({ error: 'upstream_redirect_rejected', orders_authorized: false }, 502);
      }
      if (response.ok || response.status < 500) {
        const headers = new Headers(response.headers);
        headers.set('cache-control', 'no-store');
        headers.set('x-solo-upstream', 'render-primary');
        return new Response(response.body, { status: response.status, headers });
      }
    } catch { /* market-only status fallback below */ }

    const fallback = await fetchSecondaryMarkets(fetchImpl, secondary, env.SECONDARY_MARKET_TOKEN, now(), maxAgeSeconds(env));
    if (!fallback) return json({ error: 'primary_and_market_fallback_unavailable', orders_authorized: false }, 503);
    return json({
      service: 'solo-edge-market-fallback',
      status: 'DEGRADED_MARKET_ONLY',
      mode: 'PAPER_ONLY',
      orders_authorized: false,
      feed: {
        status: 'DEGRADED',
        worker_status: 'UNAVAILABLE',
        data_mode: 'POLLED_FALLBACK',
        source: 'DENO_SECONDARY_BINANCE_USDM_TESTNET_REST',
        orders_authorized: false,
      },
      state_storage: {
        durability: 'UNAVAILABLE',
        survives_restart: null,
        warning: 'Primary ledger/API is unavailable. No ledger, positions, risk or paper-processing state is available from this fallback.',
      },
      analysis_runtime: {
        enabled: false,
        status: 'MARKET_FALLBACK_ONLY',
        reason: 'Independent public Testnet REST snapshots are display-only. Research, risk assessment, opportunities and paper processing are unavailable.',
        orders_authorized: false,
      },
      fallback_market_symbols: fallback.items.map((item) => item.symbol),
      reason: 'Primary Render API/ledger is unavailable. Solo is showing only independently fetched, polled Testnet market snapshots; this is not a live stream and must not be used for paper decisions.',
    }, 200, { 'x-solo-upstream': 'deno-secondary', 'x-solo-data-mode': 'POLLED_FALLBACK' });
  }

  if (path === '/api/v1/emergency-stop' && request.method === 'POST') {
    if (!primary) return json({ error: 'primary_unavailable', orders_authorized: false }, 503);
    if (!request.headers.get('authorization')?.startsWith('Bearer ')) {
      return json({ error: 'unauthorized', orders_authorized: false }, 401);
    }
    const declaredLength = Number(request.headers.get('content-length') ?? 0);
    if (Number.isFinite(declaredLength) && declaredLength > 2048) {
      return json({ error: 'invalid_request_size', orders_authorized: false }, 400);
    }
    let body;
    try { body = await readLimitedBody(request, 2048); } catch {
      return json({ error: 'invalid_request_size', orders_authorized: false }, 400);
    }
    if (body.byteLength === 0 || body.byteLength > 2048) {
      return json({ error: 'invalid_request_size', orders_authorized: false }, 400);
    }
    try {
      const response = await upstreamFetch(fetchImpl, primary, request, 'POST', body);
      if (response.status >= 300 && response.status < 400) {
        return json({ error: 'upstream_redirect_rejected', orders_authorized: false }, 502);
      }
      const headers = new Headers(response.headers);
      headers.set('cache-control', 'no-store');
      headers.set('x-solo-upstream', 'render-primary');
      return new Response(response.body, { status: response.status, headers });
    } catch {
      // Never retry a mutation or send it to a secondary provider.
      return json({ error: 'primary_unavailable', orders_authorized: false }, 503);
    }
  }

  if (request.method !== 'GET') return json({ error: 'method_not_allowed', orders_authorized: false }, 405);
  if (!isAllowedReadPath(path)) return json({ error: 'not_found', orders_authorized: false }, 404);
  if (!request.headers.get('authorization')?.startsWith('Bearer ')) {
    return json({ error: 'unauthorized', orders_authorized: false }, 401);
  }
  if (!primary) return json({ error: 'primary_unavailable', orders_authorized: false }, 503);

  const ageLimit = maxAgeSeconds(env);
  if (path === '/api/v1/markets') {
    try {
      const response = await upstreamFetch(fetchImpl, primary, request, 'GET');
      if (response.status >= 300 && response.status < 400) {
        return json({ error: 'upstream_redirect_rejected', orders_authorized: false }, 502);
      }
      if (response.ok) {
        const payload = await readJson(response);
        if (marketPayloadExplicitlyUnsafe(payload)) {
          return json({ error: 'unsafe_primary_market_response', orders_authorized: false }, 502);
        }
        if (validMarketPayload(payload, now(), ageLimit)) {
          return json(payload, response.status, { 'x-solo-upstream': 'render-primary', 'x-solo-data-mode': 'PRIMARY' });
        }
      } else if (response.status < 500) {
        return response;
      }
    } catch { /* the independent fallback is attempted only for market snapshots */ }

    const fallback = await fetchSecondaryMarkets(fetchImpl, secondary, env.SECONDARY_MARKET_TOKEN, now(), ageLimit);
    if (fallback) {
      return json(fallback, 200, { 'x-solo-upstream': 'deno-secondary', 'x-solo-data-mode': 'POLLED_FALLBACK' });
    }
    return json({ status: 'UNAVAILABLE', items: [], reason: 'primary and independent market fallback unavailable or stale', orders_authorized: false }, 503);
  }

  try {
    const response = await upstreamFetch(fetchImpl, primary, request, 'GET');
    if (response.status >= 300 && response.status < 400) {
      return json({ error: 'upstream_redirect_rejected', orders_authorized: false }, 502);
    }
    const headers = new Headers(response.headers);
    headers.set('cache-control', 'no-store');
    headers.set('x-solo-upstream', 'render-primary');
    return new Response(response.body, { status: response.status, headers });
  } catch {
    return json({ error: 'primary_unavailable', orders_authorized: false }, 503);
  }
}

export default {
  fetch(request, env) {
    return handleRequest(request, env);
  },
};
