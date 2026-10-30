BEGIN;

-- Shared, durable, read-only market feed between the independent worker and API.
CREATE TABLE IF NOT EXISTS paper_ledger.market_feed_snapshots (
    symbol text PRIMARY KEY,
    worker_id text NOT NULL,
    checked_at timestamptz NOT NULL,
    observed_at timestamptz,
    feed_status text NOT NULL CHECK (feed_status IN
        ('HEALTHY','STALE','GAP_DETECTED','DISCONNECTED','NO_CLOSED_CANDLES','INVALID_DATA')),
    payload jsonb NOT NULL,
    orders_authorized boolean NOT NULL DEFAULT false CHECK (orders_authorized = false),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_market_feed_snapshots_checked
    ON paper_ledger.market_feed_snapshots (checked_at DESC);

CREATE TABLE IF NOT EXISTS paper_ledger.market_worker_heartbeats (
    worker_id text PRIMARY KEY,
    owner_id text NOT NULL,
    started_at timestamptz NOT NULL,
    heartbeat_at timestamptz NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('STARTING','RUNNING','DEGRADED','STOPPING','STOPPED','ERROR')),
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    orders_authorized boolean NOT NULL DEFAULT false CHECK (orders_authorized = false)
);

CREATE INDEX IF NOT EXISTS idx_market_worker_heartbeat_age
    ON paper_ledger.market_worker_heartbeats (heartbeat_at DESC);

-- The market-feed role is private to backend service credentials, never the Supabase Data API.
ALTER TABLE paper_ledger.market_feed_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE paper_ledger.market_worker_heartbeats ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON paper_ledger.market_feed_snapshots FROM PUBLIC;
REVOKE ALL ON paper_ledger.market_worker_heartbeats FROM PUBLIC;
DO $$
DECLARE role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('REVOKE ALL ON paper_ledger.market_feed_snapshots FROM %I', role_name);
            EXECUTE format('REVOKE ALL ON paper_ledger.market_worker_heartbeats FROM %I', role_name);
        END IF;
    END LOOP;
END
$$;

COMMIT;
