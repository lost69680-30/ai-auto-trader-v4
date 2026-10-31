BEGIN;

-- Durable, private registry for independent research opportunities.
-- This schema has no exchange execution authority.
CREATE TABLE IF NOT EXISTS paper_ledger.opportunities (
    opportunity_id text PRIMARY KEY CHECK (length(opportunity_id) BETWEEN 1 AND 160),
    idempotency_key text NOT NULL UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    symbol text NOT NULL CHECK (
        symbol = upper(btrim(symbol)) AND length(symbol) BETWEEN 1 AND 30
    ),
    market_type text NOT NULL CHECK (market_type IN ('SPOT', 'FUTURES')),
    venue text NOT NULL CHECK (
        (market_type = 'FUTURES' AND venue = 'BINANCE_USDM_TESTNET')
        OR (market_type = 'SPOT' AND venue = 'BINANCE_SPOT_TESTNET')
    ),
    strategy_id text NOT NULL CHECK (
        strategy_id IN ('scalping', 'day', 'swing', 'position', 'long_term')
    ),
    strategy_version text NOT NULL CHECK (length(strategy_version) BETWEEN 1 AND 80),
    setup_fingerprint text NOT NULL CHECK (length(setup_fingerprint) BETWEEN 1 AND 240),
    direction text NOT NULL CHECK (direction IN ('LONG', 'SHORT', 'NEUTRAL')),
    timeframe text NOT NULL CHECK (length(timeframe) BETWEEN 1 AND 12),
    state text NOT NULL CHECK (state IN (
        'DISCOVERED', 'ASSESSING', 'WAITING', 'REASSESSING', 'TRIGGERED',
        'RISK_CHECK', 'INTENT_RECORDED', 'PAPER_OPEN', 'REJECTED',
        'INVALIDATED', 'EXPIRED', 'CANCELLED', 'BLOCKED', 'NO_TRADE'
    )),
    version integer NOT NULL DEFAULT 0 CHECK (version >= 0),
    expires_at timestamptz,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object')
        CHECK (COALESCE(metadata->>'orders_authorized', 'false') = 'false'),
    orders_authorized boolean NOT NULL DEFAULT false CHECK (orders_authorized = false),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (market_type, symbol, strategy_id, setup_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_opportunities_active_state
    ON paper_ledger.opportunities (state, expires_at, updated_at DESC)
    WHERE state IN ('DISCOVERED', 'ASSESSING', 'WAITING', 'REASSESSING', 'TRIGGERED', 'RISK_CHECK', 'INTENT_RECORDED');

CREATE INDEX IF NOT EXISTS idx_opportunities_symbol_strategy
    ON paper_ledger.opportunities (symbol, strategy_id, timeframe, created_at DESC);

CREATE TABLE IF NOT EXISTS paper_ledger.opportunity_assessments (
    assessment_id text PRIMARY KEY CHECK (length(assessment_id) BETWEEN 1 AND 160),
    opportunity_id text NOT NULL REFERENCES paper_ledger.opportunities(opportunity_id) ON DELETE CASCADE,
    idempotency_key text NOT NULL UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    strategy_id text NOT NULL CHECK (
        strategy_id IN ('scalping', 'day', 'swing', 'position', 'long_term')
    ),
    strategy_version text NOT NULL CHECK (length(strategy_version) BETWEEN 1 AND 80),
    assessment_version integer NOT NULL CHECK (assessment_version >= 1),
    decision text NOT NULL CHECK (decision IN (
        'WAIT', 'REJECT', 'REQUEST_MORE_EVIDENCE', 'ANALYSIS_ONLY', 'NO_TRADE', 'ELIGIBLE'
    )),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object')
        CHECK (COALESCE(payload->>'orders_authorized', 'false') = 'false'),
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(evidence_refs) = 'array'),
    orders_authorized boolean NOT NULL DEFAULT false CHECK (orders_authorized = false),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_opportunity_assessments_history
    ON paper_ledger.opportunity_assessments (opportunity_id, created_at DESC);

CREATE TABLE IF NOT EXISTS paper_ledger.opportunity_evidence (
    evidence_id text PRIMARY KEY CHECK (length(evidence_id) BETWEEN 1 AND 160),
    opportunity_id text NOT NULL REFERENCES paper_ledger.opportunities(opportunity_id) ON DELETE CASCADE,
    idempotency_key text NOT NULL UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    evidence_type text NOT NULL CHECK (length(evidence_type) BETWEEN 1 AND 60),
    reference_id text NOT NULL CHECK (length(reference_id) BETWEEN 1 AND 240),
    observed_at timestamptz NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(payload) = 'object')
        CHECK (COALESCE(payload->>'orders_authorized', 'false') = 'false'),
    orders_authorized boolean NOT NULL DEFAULT false CHECK (orders_authorized = false),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_opportunity_evidence_history
    ON paper_ledger.opportunity_evidence (opportunity_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS paper_ledger.opportunity_watch_conditions (
    watch_id text PRIMARY KEY CHECK (length(watch_id) BETWEEN 1 AND 160),
    opportunity_id text NOT NULL UNIQUE REFERENCES paper_ledger.opportunities(opportunity_id) ON DELETE CASCADE,
    idempotency_key text NOT NULL UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    trigger_rule text NOT NULL CHECK (trigger_rule IN (
        'AT_OR_ABOVE', 'AT_OR_BELOW', 'CANDLE_CLOSE_ABOVE', 'CANDLE_CLOSE_BELOW'
    )),
    trigger_price double precision NOT NULL CHECK (trigger_price > 0 AND trigger_price < 'Infinity'::float8),
    invalidation_price double precision CHECK (
        invalidation_price IS NULL OR (invalidation_price > 0 AND invalidation_price < 'Infinity'::float8)
    ),
    timeframe text NOT NULL CHECK (length(timeframe) BETWEEN 1 AND 12),
    expires_at timestamptz NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(payload) = 'object')
        CHECK (COALESCE(payload->>'orders_authorized', 'false') = 'false'),
    orders_authorized boolean NOT NULL DEFAULT false CHECK (orders_authorized = false),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_opportunity_watch_expiry
    ON paper_ledger.opportunity_watch_conditions (expires_at);

CREATE TABLE IF NOT EXISTS paper_ledger.opportunity_events (
    event_id text PRIMARY KEY CHECK (length(event_id) BETWEEN 1 AND 200),
    opportunity_id text NOT NULL REFERENCES paper_ledger.opportunities(opportunity_id) ON DELETE CASCADE,
    from_state text,
    to_state text NOT NULL,
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 1000),
    actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 120),
    evidence_version text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(payload) = 'object')
        CHECK (COALESCE(payload->>'orders_authorized', 'false') = 'false'),
    occurred_at timestamptz NOT NULL,
    orders_authorized boolean NOT NULL DEFAULT false CHECK (orders_authorized = false)
);

CREATE INDEX IF NOT EXISTS idx_opportunity_events_timeline
    ON paper_ledger.opportunity_events (opportunity_id, occurred_at, event_id);

CREATE TABLE IF NOT EXISTS paper_ledger.opportunity_claims (
    opportunity_id text PRIMARY KEY REFERENCES paper_ledger.opportunities(opportunity_id) ON DELETE CASCADE,
    lease_owner text NOT NULL CHECK (length(lease_owner) BETWEEN 1 AND 120),
    claimed_at timestamptz NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    orders_authorized boolean NOT NULL DEFAULT false CHECK (orders_authorized = false),
    CHECK (lease_expires_at > claimed_at)
);

CREATE INDEX IF NOT EXISTS idx_opportunity_claim_expiry
    ON paper_ledger.opportunity_claims (lease_expires_at);

-- These records remain backend-private and inaccessible through the Supabase Data API.
ALTER TABLE paper_ledger.opportunities ENABLE ROW LEVEL SECURITY;
ALTER TABLE paper_ledger.opportunity_assessments ENABLE ROW LEVEL SECURITY;
ALTER TABLE paper_ledger.opportunity_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE paper_ledger.opportunity_watch_conditions ENABLE ROW LEVEL SECURITY;
ALTER TABLE paper_ledger.opportunity_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE paper_ledger.opportunity_claims ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON paper_ledger.opportunities FROM PUBLIC;
REVOKE ALL ON paper_ledger.opportunity_assessments FROM PUBLIC;
REVOKE ALL ON paper_ledger.opportunity_evidence FROM PUBLIC;
REVOKE ALL ON paper_ledger.opportunity_watch_conditions FROM PUBLIC;
REVOKE ALL ON paper_ledger.opportunity_events FROM PUBLIC;
REVOKE ALL ON paper_ledger.opportunity_claims FROM PUBLIC;

DO $$
DECLARE role_name text;
DECLARE table_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            FOREACH table_name IN ARRAY ARRAY[
                'opportunities', 'opportunity_assessments', 'opportunity_evidence',
                'opportunity_watch_conditions', 'opportunity_events', 'opportunity_claims'
            ] LOOP
                EXECUTE format('REVOKE ALL ON paper_ledger.%I FROM %I', table_name, role_name);
            END LOOP;
        END IF;
    END LOOP;
END
$$;

COMMIT;
