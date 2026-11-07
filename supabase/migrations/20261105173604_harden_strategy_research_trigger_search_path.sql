BEGIN;

CREATE OR REPLACE FUNCTION paper_ledger.reject_strategy_research_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'strategy research memory is append-only';
END;
$$;

COMMENT ON TABLE paper_ledger.strategy_research_experiments IS
    'Private backend-only append-only research memory; no client-role policies by design.';

COMMIT;
