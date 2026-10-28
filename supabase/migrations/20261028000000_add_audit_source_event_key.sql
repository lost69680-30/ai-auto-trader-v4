-- Adds an idempotency key for backend-only audit mirroring.
-- Apply only after reviewing the feature branch; do not grant client roles access.
ALTER TABLE public.audit_events
    ADD COLUMN IF NOT EXISTS source_event_key text;

CREATE UNIQUE INDEX IF NOT EXISTS audit_events_source_event_key_uidx
    ON public.audit_events (source_event_key);
