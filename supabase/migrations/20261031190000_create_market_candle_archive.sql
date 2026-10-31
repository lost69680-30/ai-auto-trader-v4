BEGIN;

-- Canonical, immutable one-minute archive. Venue is explicit so Testnet data
-- can never be silently mixed with production-market history.
CREATE TABLE IF NOT EXISTS paper_ledger.market_candles (
    venue text NOT NULL CHECK (venue IN ('BINANCE_USDM_TESTNET','BINANCE_SPOT_TESTNET')),
    market_type text NOT NULL CHECK (market_type IN ('FUTURES','SPOT')),
    symbol text NOT NULL CHECK (symbol = upper(btrim(symbol)) AND length(symbol) > 0),
    interval text NOT NULL DEFAULT '1m' CHECK (interval = '1m'),
    open_time timestamptz NOT NULL,
    close_time timestamptz NOT NULL,
    open double precision NOT NULL CHECK (open > 0 AND open < 'Infinity'::double precision),
    high double precision NOT NULL CHECK (high > 0 AND high < 'Infinity'::double precision),
    low double precision NOT NULL CHECK (low > 0 AND low < 'Infinity'::double precision),
    close double precision NOT NULL CHECK (close > 0 AND close < 'Infinity'::double precision),
    volume double precision NOT NULL CHECK (volume >= 0 AND volume < 'Infinity'::double precision),
    orders_authorized boolean NOT NULL DEFAULT false CHECK (orders_authorized = false),
    archived_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (venue, market_type, symbol, interval, open_time),
    CHECK (
        (market_type = 'FUTURES' AND venue = 'BINANCE_USDM_TESTNET')
        OR (market_type = 'SPOT' AND venue = 'BINANCE_SPOT_TESTNET')
    ),
    CHECK (date_trunc('minute', open_time) = open_time),
    CHECK (close_time > open_time AND close_time < open_time + interval '1 minute'),
    CHECK (high >= GREATEST(open, close, low)),
    CHECK (low <= LEAST(open, close, high))
);

CREATE INDEX IF NOT EXISTS idx_market_candles_symbol_time
    ON paper_ledger.market_candles (venue, market_type, symbol, open_time DESC);

ALTER TABLE paper_ledger.market_candles ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON paper_ledger.market_candles FROM PUBLIC;
DO $$
DECLARE role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('REVOKE ALL ON paper_ledger.market_candles FROM %I', role_name);
        END IF;
    END LOOP;
END
$$;

COMMIT;
