BEGIN;

-- Append-only, backend-private memory for reproducible strategy experiments.
CREATE TABLE IF NOT EXISTS paper_ledger.strategy_research_experiments (
    experiment_id text PRIMARY KEY CHECK (length(experiment_id) BETWEEN 1 AND 160),
    idempotency_key text NOT NULL UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    candidate_id text NOT NULL CHECK (length(candidate_id) BETWEEN 1 AND 160),
    candidate_fingerprint text NOT NULL CHECK (length(candidate_fingerprint) BETWEEN 1 AND 128),
    source_provenance_id text NOT NULL CHECK (length(source_provenance_id) BETWEEN 1 AND 128),
    experiment_version integer NOT NULL CHECK (experiment_version >= 1),
    dataset_start timestamptz NOT NULL,
    dataset_end timestamptz NOT NULL,
    configuration_version text NOT NULL CHECK (length(configuration_version) BETWEEN 1 AND 120),
    assumptions jsonb NOT NULL CHECK (jsonb_typeof(assumptions) = 'object')
        CHECK (COALESCE(assumptions->>'orders_authorized', 'false') = 'false'),
    outcomes jsonb NOT NULL CHECK (jsonb_typeof(outcomes) = 'object')
        CHECK (COALESCE(outcomes->>'orders_authorized', 'false') = 'false'),
    status text NOT NULL CHECK (status IN (
        'EVALUATED', 'REJECTED', 'INSUFFICIENT_DATA', 'DRIFT_FLAGGED',
        'DECAY_FLAGGED', 'NOT_REPRODUCIBLE'
    )),
    failure_reasons jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(failure_reasons) = 'array'),
    orders_authorized boolean NOT NULL DEFAULT false CHECK (orders_authorized = false),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    CHECK (dataset_start < dataset_end),
    UNIQUE (candidate_fingerprint, experiment_version)
);

CREATE INDEX IF NOT EXISTS idx_strategy_research_candidate_history
    ON paper_ledger.strategy_research_experiments
       (candidate_fingerprint, experiment_version DESC);

CREATE INDEX IF NOT EXISTS idx_strategy_research_source_provenance
    ON paper_ledger.strategy_research_experiments
       (source_provenance_id, recorded_at DESC);

CREATE OR REPLACE FUNCTION paper_ledger.reject_strategy_research_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'strategy research memory is append-only';
END;
$$;

DROP TRIGGER IF EXISTS strategy_research_experiments_append_only
    ON paper_ledger.strategy_research_experiments;
CREATE TRIGGER strategy_research_experiments_append_only
    BEFORE UPDATE OR DELETE ON paper_ledger.strategy_research_experiments
    FOR EACH ROW EXECUTE FUNCTION paper_ledger.reject_strategy_research_mutation();

ALTER TABLE paper_ledger.strategy_research_experiments ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON paper_ledger.strategy_research_experiments FROM PUBLIC;

DO $$
DECLARE role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format(
                'REVOKE ALL ON paper_ledger.strategy_research_experiments FROM %I',
                role_name
            );
        END IF;
    END LOOP;
END
$$;

COMMIT;
