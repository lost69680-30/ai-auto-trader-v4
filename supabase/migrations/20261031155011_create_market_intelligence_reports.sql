BEGIN;

-- Store compact, durable Market Watcher assessments separately from the worker
-- heartbeat. This avoids rewriting a large JSON report on every short heartbeat.
CREATE TABLE IF NOT EXISTS paper_ledger.market_intelligence_reports (
    scan_id text NOT NULL CHECK (length(scan_id) BETWEEN 1 AND 200),
    symbol text NOT NULL CHECK (symbol = upper(btrim(symbol)) AND length(symbol) BETWEEN 1 AND 30),
    assessed_at timestamptz NOT NULL,
    market_type text NOT NULL CHECK (market_type IN ('SPOT', 'FUTURES')),
    environment text NOT NULL CHECK (environment = 'TESTNET'),
    venue text NOT NULL CHECK (
        (market_type = 'FUTURES' AND venue = 'BINANCE_USDM_TESTNET')
        OR (market_type = 'SPOT' AND venue = 'BINANCE_SPOT_TESTNET')
    ),
    assessment jsonb NOT NULL CHECK (assessment->>'orders_authorized' = 'false'),
    selection jsonb NOT NULL CHECK (selection->>'orders_authorized' = 'false'),
    liquidity_evidence jsonb NOT NULL,
    in_progress_timeframes jsonb NOT NULL DEFAULT '[]'::jsonb,
    timeframe_errors jsonb NOT NULL DEFAULT '{}'::jsonb,
    orders_authorized boolean NOT NULL DEFAULT false CHECK (orders_authorized = false),
    PRIMARY KEY (scan_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_market_intelligence_reports_latest
    ON paper_ledger.market_intelligence_reports (assessed_at DESC, scan_id);

CREATE INDEX IF NOT EXISTS idx_market_intelligence_reports_symbol_latest
    ON paper_ledger.market_intelligence_reports (symbol, assessed_at DESC);

ALTER TABLE paper_ledger.market_intelligence_reports ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON paper_ledger.market_intelligence_reports FROM PUBLIC;

DO $$
DECLARE role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('REVOKE ALL ON paper_ledger.market_intelligence_reports FROM %I', role_name);
        END IF;
    END LOOP;
END
$$;

COMMIT;
