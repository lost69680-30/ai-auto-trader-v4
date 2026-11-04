import test from 'node:test';
import assert from 'node:assert/strict';
import { handleRequest } from '../src/index.js';

const NOW = Date.parse('2026-11-05T00:00:00.000Z');
const freshMarket = {
  status: 'HEALTHY',
  checked_at: new Date(NOW).toISOString(),
  orders_authorized: false,
  items: [{
    symbol: 'BTCUSDT', status: 'HEALTHY', mark_price: 70000,
    mark_observed_at: new Date(NOW).toISOString(), market_type: 'FUTURES',
    price_type: 'FUTURES_MARK_PRICE', index_price: 69990,
    funding_rate: 0.0001, next_funding_at: '2026-11-05T08:00:00.000Z',
    candles: [], orders_authorized: false,
  }],
};

function env(overrides = {}) {
  return {
    PRIMARY_API_ORIGIN: 'https://solo-primary.example',
    SECONDARY_MARKET_ORIGIN: 'https://solo-market-fallback.example',
    MAX_MARKET_AGE_SECONDS: '120',
    ...overrides,
  };
}

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

test('healthz is public, local, and explicitly paper-only', async () => {
  const response = await handleRequest(new Request('https://edge.example/healthz'), env(), {
    fetchImpl: async () => { throw new Error('healthz must not call upstream'); }, now: () => NOW,
  });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.status, 'ok');
  assert.equal(body.orders_authorized, false);
  assert.equal(body.mode, 'PAPER_ONLY');
});

test('fresh primary market response wins without contacting secondary', async () => {
  const calls = [];
  const response = await handleRequest(new Request('https://edge.example/api/v1/markets', {
    headers: { authorization: 'Bearer ' + 'x'.repeat(40) },
  }), env(), {
    now: () => NOW,
    fetchImpl: async (url) => { calls.push(String(url)); return jsonResponse(freshMarket); },
  });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('x-solo-data-mode'), 'PRIMARY');
  assert.equal(calls.length, 1);
  assert.match(calls[0], /^https:\/\/solo-primary\.example/);
});

test('market request falls back to independent Deno source when primary is unavailable', async () => {
  const calls = [];
  const fallback = { ...freshMarket, status: 'DEGRADED', data_mode: 'POLLED_FALLBACK' };
  const response = await handleRequest(new Request('https://edge.example/api/v1/markets', {
    headers: { authorization: 'Bearer ' + 'x'.repeat(40) },
  }), env(), {
    now: () => NOW,
    fetchImpl: async (url) => {
      calls.push(String(url));
      if (String(url).startsWith('https://solo-primary.example')) throw new Error('primary asleep');
      return jsonResponse(fallback);
    },
  });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('x-solo-data-mode'), 'POLLED_FALLBACK');
  assert.equal(response.headers.get('x-solo-upstream'), 'deno-secondary');
  assert.equal(calls.length, 2);
  const body = await response.json();
  assert.equal(body.orders_authorized, false);
  assert.equal(body.data_mode, 'POLLED_FALLBACK');
});

test('risk and ledger endpoints never fall back to secondary market data', async () => {
  const calls = [];
  const response = await handleRequest(new Request('https://edge.example/api/v1/risk', {
    headers: { authorization: 'Bearer ' + 'x'.repeat(40) },
  }), env(), {
    now: () => NOW,
    fetchImpl: async (url) => { calls.push(String(url)); throw new Error('primary down'); },
  });
  assert.equal(response.status, 503);
  assert.equal(calls.length, 1);
  assert.match(calls[0], /^https:\/\/solo-primary\.example/);
  const body = await response.json();
  assert.equal(body.orders_authorized, false);
});

test('emergency stop is forwarded once to primary and never retried on secondary', async () => {
  const calls = [];
  const request = new Request('https://edge.example/api/v1/emergency-stop', {
    method: 'POST',
    headers: { authorization: 'Bearer ' + 'x'.repeat(40), 'content-type': 'application/json' },
    body: JSON.stringify({ reason: 'operator test' }),
  });
  const response = await handleRequest(request, env(), {
    now: () => NOW,
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), method: init.method });
      throw new Error('primary unavailable');
    },
  });
  assert.equal(response.status, 503);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, 'POST');
  assert.match(calls[0].url, /^https:\/\/solo-primary\.example/);
});

test('unsupported mutation methods and paths are rejected', async () => {
  const calls = [];
  const response = await handleRequest(new Request('https://edge.example/api/v1/orders', {
    method: 'POST', headers: { authorization: 'Bearer ' + 'x'.repeat(40) }, body: '{}',
  }), env(), { fetchImpl: async (url) => { calls.push(url); return jsonResponse({}); } });
  assert.equal(response.status, 405);
  assert.equal(calls.length, 0);
});

test('unauthorized market request does not trigger either upstream', async () => {
  const calls = [];
  const response = await handleRequest(new Request('https://edge.example/api/v1/markets'), env(), {
    fetchImpl: async (url) => { calls.push(url); return jsonResponse(freshMarket); },
  });
  assert.equal(response.status, 401);
  assert.equal(calls.length, 0);
});

test('stale or unsafe secondary market payload is rejected', async () => {
  const stale = structuredClone(freshMarket);
  stale.items[0].mark_observed_at = '2026-11-04T23:00:00.000Z';
  const response = await handleRequest(new Request('https://edge.example/api/v1/markets', {
    headers: { authorization: 'Bearer ' + 'x'.repeat(40) },
  }), env(), {
    now: () => NOW,
    fetchImpl: async (url) => String(url).startsWith('https://solo-primary.example')
      ? new Response('unavailable', { status: 503 }) : jsonResponse(stale),
  });
  assert.equal(response.status, 503);
  const body = await response.json();
  assert.equal(body.orders_authorized, false);
});

test('status fallback is explicitly market-only and never fabricates ledger or risk state', async () => {
  const calls = [];
  const fallback = { ...freshMarket, status: 'DEGRADED', data_mode: 'POLLED_FALLBACK' };
  const response = await handleRequest(new Request('https://edge.example/api/v1/status', {
    headers: { authorization: 'Bearer ' + 'x'.repeat(40) },
  }), env(), {
    now: () => NOW,
    fetchImpl: async (url) => {
      calls.push(String(url));
      if (String(url).startsWith('https://solo-primary.example')) return new Response('unavailable', { status: 503 });
      return jsonResponse(fallback);
    },
  });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.status, 'DEGRADED_MARKET_ONLY');
  assert.equal(body.mode, 'PAPER_ONLY');
  assert.equal(body.orders_authorized, false);
  assert.equal(body.feed.data_mode, 'POLLED_FALLBACK');
  assert.equal(Object.hasOwn(body, 'emergency_stop_active'), false);
  assert.equal(Object.hasOwn(body, 'open_position_count'), false);
  assert.equal(Object.hasOwn(body, 'positions'), false);
  assert.equal(calls.length, 2);
});

test('primary HTTP 500 triggers market-only fallback, while auth errors do not', async () => {
  const calls = [];
  const fallback = { ...freshMarket, status: 'DEGRADED', data_mode: 'POLLED_FALLBACK' };
  const response = await handleRequest(new Request('https://edge.example/api/v1/markets', {
    headers: { authorization: 'Bearer ' + 'x'.repeat(40) },
  }), env(), {
    now: () => NOW,
    fetchImpl: async (url) => {
      calls.push(String(url));
      return String(url).startsWith('https://solo-primary.example')
        ? new Response('server error', { status: 500 }) : jsonResponse(fallback);
    },
  });
  assert.equal(response.status, 200);
  assert.equal(calls.length, 2);
});

test('oversized emergency-stop request is rejected before any upstream call', async () => {
  const calls = [];
  const response = await handleRequest(new Request('https://edge.example/api/v1/emergency-stop', {
    method: 'POST',
    headers: { authorization: 'Bearer ' + 'x'.repeat(40), 'content-type': 'application/json' },
    body: JSON.stringify({ reason: 'x'.repeat(3000) }),
  }), env(), { fetchImpl: async (url) => { calls.push(url); return jsonResponse({}); } });
  assert.equal(response.status, 400);
  assert.equal(calls.length, 0);
});

test('authenticated market WebSocket is tunneled to Render primary only', async () => {
  const calls = [];
  const request = new Request('https://edge.example/ws/v1/markets', {
    headers: { authorization: 'Bearer ' + 'x'.repeat(40), upgrade: 'websocket', 'sec-websocket-key': 'dGhlIHNhbXBsZSBub25jZQ==', 'sec-websocket-version': '13' },
  });
  const response = await handleRequest(request, env(), {
    fetchImpl: async (upstreamRequest) => {
      calls.push(upstreamRequest);
      return new Response(null, { status: 200 });
    },
  });
  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(new URL(calls[0].url).pathname, '/ws/v1/markets');
  assert.equal(calls[0].headers.get('upgrade'), 'websocket');
  assert.equal(calls[0].headers.get('sec-websocket-key'), 'dGhlIHNhbXBsZSBub25jZQ==');
  assert.equal(calls[0].headers.get('sec-websocket-version'), '13');
  assert.equal(calls[0].headers.get('authorization'), 'Bearer ' + 'x'.repeat(40));
});

test('WebSocket never falls back to Deno when Render is unavailable', async () => {
  const calls = [];
  const request = new Request('https://edge.example/ws/v1/markets', {
    headers: { authorization: 'Bearer ' + 'x'.repeat(40), upgrade: 'websocket' },
  });
  const response = await handleRequest(request, env(), {
    fetchImpl: async (upstreamRequest) => { calls.push(upstreamRequest); throw new Error('primary down'); },
  });
  assert.equal(response.status, 503);
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /^https:\/\/solo-primary\.example/);
});


test('unsafe primary market authorization is blocked instead of hidden by fallback', async () => {
  const calls = [];
  const unsafe = structuredClone(freshMarket);
  unsafe.items[0].orders_authorized = true;
  const response = await handleRequest(new Request('https://edge.example/api/v1/markets', {
    headers: { authorization: 'Bearer ' + 'x'.repeat(40) },
  }), env(), {
    now: () => NOW,
    fetchImpl: async (url) => { calls.push(String(url)); return jsonResponse(unsafe); },
  });
  assert.equal(response.status, 502);
  assert.equal(calls.length, 1);
  const body = await response.json();
  assert.equal(body.error, 'unsafe_primary_market_response');
  assert.equal(body.orders_authorized, false);
});

test('streamed oversized emergency-stop body is capped even without Content-Length', async () => {
  const calls = [];
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new Uint8Array(3000));
      controller.close();
    },
  });
  const request = new Request('https://edge.example/api/v1/emergency-stop', {
    method: 'POST',
    headers: { authorization: 'Bearer ' + 'x'.repeat(40) },
    body: stream,
    duplex: 'half',
  });
  const response = await handleRequest(request, env(), {
    fetchImpl: async (url) => { calls.push(url); return jsonResponse({}); },
  });
  assert.equal(response.status, 400);
  assert.equal(calls.length, 0);
});
