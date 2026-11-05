BEGIN;

CREATE TABLE IF NOT EXISTS paper_ledger.scheduled_job_definitions (
    job_name text PRIMARY KEY,
    interval_seconds integer NOT NULL CHECK (interval_seconds >= 1),
    next_run_at timestamptz NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS paper_ledger.scheduled_jobs (
    idempotency_key text PRIMARY KEY,
    job_name text NOT NULL REFERENCES paper_ledger.scheduled_job_definitions(job_name),
    scheduled_for timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_owner text,
    lease_expires_at timestamptz,
    started_at timestamptz,
    completed_at timestamptz,
    last_error text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (status = 'RUNNING' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR status <> 'RUNNING'
    )
);

CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_claim
    ON paper_ledger.scheduled_jobs (status, scheduled_for, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_name
    ON paper_ledger.scheduled_jobs (job_name, scheduled_for DESC);

ALTER TABLE paper_ledger.scheduled_job_definitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE paper_ledger.scheduled_jobs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON paper_ledger.scheduled_job_definitions FROM PUBLIC;
REVOKE ALL ON paper_ledger.scheduled_jobs FROM PUBLIC;
DO $$
DECLARE role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('REVOKE ALL ON paper_ledger.scheduled_job_definitions FROM %I', role_name);
            EXECUTE format('REVOKE ALL ON paper_ledger.scheduled_jobs FROM %I', role_name);
        END IF;
    END LOOP;
END
$$;

COMMIT;
