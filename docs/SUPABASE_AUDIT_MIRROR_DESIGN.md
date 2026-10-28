# Supabase Audit Mirror Design

Status: approved approach; implementation remains isolated on `feature/supabase-audit-mirror` until reviewed and merged.

## Goal
Mirror locally recorded paper-ledger audit events to the existing private Supabase `public.audit_events` table without replacing SQLite or changing Render configuration.

## Non-goals
- Do not replace SQLite as the paper ledger.
- Do not change order authorization, strategy, risk controls, or emergency-stop behavior.
- Do not add exchange execution capability.
- Do not deploy or modify Render environment variables in the first code change.
- Do not migrate historical SQLite records automatically.

## Data flow
1. SQLite remains the source of truth for paper orders, fills, positions, and emergency-stop state.
2. A background mirror reads existing audit events only.
3. It sends audit rows to Supabase over HTTPS using the REST endpoint and a server-only secret key.
4. The database enforces an idempotency key so retries do not duplicate events.
5. The mirror never sends order requests and never authorizes orders.

## Schema
Extend `public.audit_events` with a stable `source_event_key` and a unique constraint/index. Construct the key from a configured source identifier and the SQLite audit event ID. Keep existing `event_type`, `payload`, and `created_at` columns. Never expose service credentials to browser/mobile clients.

## Failure handling
- If Supabase configuration is absent, the mirror remains disabled and existing API behavior is unchanged.
- Network errors, non-2xx responses, invalid responses, and timeouts are logged without credentials or secret payloads.
- Failures do not alter SQLite ledger data or risk-control state.
- Retry with bounded delays and idempotent writes; never mark an event delivered until Supabase confirms success.
- Supabase unavailability does not enable orders; this code has no order authorization path.

## Security
- Use only a backend secret stored in Render environment variables after code review.
- Never put the key in source control, APK assets, public logs, or API responses.
- Keep RLS enabled and no grants/policies for `anon` or `authenticated`.
- Verify the target table/schema before enabling integration.

## Tests and acceptance criteria
- Unit tests for stable idempotency keys, disabled configuration, successful writes, duplicate-safe retries, timeout/network errors, and response failures.
- Existing test suite must pass.
- Confirm no order/exchange API is introduced and `orders_authorized` remains false.
- Deploy only after review and secure secret configuration; verify a harmless test audit event in Supabase without logging secrets.
- SQLite remains authoritative until a separately designed and tested durable-ledger migration is approved.
