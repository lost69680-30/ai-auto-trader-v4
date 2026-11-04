import test from 'node:test';
import assert from 'node:assert/strict';
import { buildMarketPayload, createHandler, parseSymbols } from '../main.js';

const NOW = Date.parse('2026-11-05T00:00:00.000Z');
const upstreamRows = [{
  symbol: 'BTCUSDT', markPrice: '70000.25', indexPrice: '69990.00',
  lastFundingRate: '0.0001', nextFundingTime: NOW + 8 * 60 * 60 * 1000,
  time: NOW,
}];

test('symbol parser normalizes and de-duplicates a bounded allowlist', () => {
  assert.deepEqual(parseSymbols('btcusdt, ETHUSDT,btcusdt'), ['BTCUSDT', 'ETHUSDT']);
  assert.throws(() => parseSymbols('BTCUSDT,;DROP'), /invalid/);
  assert.throws(() => parseSymbols(Array(40).fill('BTCUSDT').join(',')), /maximum/);
});

test('market payload carries explicit polled fallback provenance and paper-only flag', () => {
  const payload = buildMarketPayload(upstreamRows, ['BTCUSDT'], NOW);
  assert.equal(payload.status, 'DEGRADED');
  assert.equal(payload.data_mode, 'POLLED_FALLBACK');
  assert.equal(payload.orders_authorized, false);
  assert.equal(payload.items.length, 1);
  assert.equal(payload.items[0].price_type, 'FUTURES_MARK_PRICE');
  assert.equal(payload.items[0].market_type, 'FUTURES');
  assert.equal(payload.items[0].orders_authorized, false);
  assert.equal(payload.items[0].candles.length, 0);
});

test('stale source observations are omitted rather than relabeled fresh', () => {
  const stale = [{ ...upstreamRows[0], time: NOW - 10 * 60 * 1000 }];
  const payload = buildMarketPayload(stale, ['BTCUSDT'], NOW);
  assert.equal(payload.items.length, 0);
  assert.equal(payload.status, 'UNAVAILABLE');
});

test('malformed, non-finite and non-allowlisted market rows are rejected', () => {
  const rows = [
    { ...upstreamRows[0], markPrice: 'NaN' },
    { ...upstreamRows[0], symbol: 'ETHUSDT' },
    { ...upstreamRows[0], indexPrice: '0' },
  ];
  const payload = buildMarketPayload(rows, ['BTCUSDT'], NOW);
  assert.equal(payload.items.length, 0);
  assert.equal(payload.orders_authorized, false);
});

test('handler exposes health and GET-only markets; mutation is forbidden', async () => {
  const env = (name) => ({
    SOLO_MARKET_SYMBOLS: 'BTCUSDT',
    BINANCE_USDM_TESTNET_REST_ORIGIN: 'https://demo-fapi.binance.com',
    SOLO_FALLBACK_TOKEN: 'solo-test-shared-secret-0123456789abcdef',
  })[name];
  const handler = createHandler({ env, now: () => NOW, fetchImpl: async () => new Response(JSON.stringify(upstreamRows)) });
  const health = await handler(new Request('https://deno.example/healthz'));
  assert.equal(health.status, 200);
  assert.equal((await health.json()).orders_authorized, false);
  const readyDenied = await handler(new Request('https://deno.example/readyz'));
  assert.equal(readyDenied.status, 401);
  const ready = await handler(new Request('https://deno.example/readyz', { headers: { authorization: 'Bearer solo-test-shared-secret-0123456789abcdef' } }));
  assert.equal(ready.status, 200);
  const denied = await handler(new Request('https://deno.example/api/v1/markets', { method: 'POST' }));
  assert.equal(denied.status, 405);
  const market = await handler(new Request('https://deno.example/api/v1/markets', { headers: { authorization: 'Bearer solo-test-shared-secret-0123456789abcdef' } }));
  assert.equal(market.status, 200);
  assert.equal((await market.json()).data_mode, 'POLLED_FALLBACK');
});

test('upstream failure returns safe unavailable response', async () => {
  const env = (name) => ({ SOLO_MARKET_SYMBOLS: 'BTCUSDT', BINANCE_USDM_TESTNET_REST_ORIGIN: 'https://demo-fapi.binance.com' })[name];
  const handler = createHandler({ env, now: () => NOW, fetchImpl: async () => { throw new Error('network down'); } });
  const response = await handler(new Request('https://deno.example/api/v1/markets', { headers: { authorization: 'Bearer solo-test-shared-secret-0123456789abcdef' } }));
  assert.equal(response.status, 503);
  const body = await response.json();
  assert.equal(body.status, 'UNAVAILABLE');
  assert.equal(body.orders_authorized, false);
  assert.deepEqual(body.items, []);
});


test('Deno market fallback requires the private service-to-service token', async () => {
  let upstreamCalls = 0;
  const env = (name) => ({
    SOLO_MARKET_SYMBOLS: 'BTCUSDT',
    BINANCE_USDM_TESTNET_REST_ORIGIN: 'https://demo-fapi.binance.com',
    SOLO_FALLBACK_TOKEN: 'solo-test-shared-secret-0123456789abcdef',
  })[name];
  const handler = createHandler({
    env,
    now: () => NOW,
    fetchImpl: async () => { upstreamCalls += 1; return new Response(JSON.stringify(upstreamRows)); },
  });
  const unauthorized = await handler(new Request('https://deno.example/api/v1/markets'));
  assert.equal(unauthorized.status, 401);
  assert.equal(upstreamCalls, 0);
  const authorized = await handler(new Request('https://deno.example/api/v1/markets', {
    headers: { authorization: 'Bearer solo-test-shared-secret-0123456789abcdef' },
  }));
  assert.equal(authorized.status, 200);
  assert.equal(upstreamCalls, 1);
});
