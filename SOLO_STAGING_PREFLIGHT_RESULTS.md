# Solo Staging Preflight — Read-Only Findings

**Checked:** 2026-10-29  
**Scope:** Read-only inspection to prepare isolated staging. No resources were created, no billing settings were changed, and no production settings, secrets, schema, or data were changed.

## Current Supabase state

- The existing organisation `lost69680-30's Org` reports the **Free** plan.
- The existing `solo-ai-auto-trader` project is in `eu-central-1` and reports `ACTIVE_HEALTHY` on PostgreSQL 17.
- This is the only Supabase project returned for the account. The existing project has **no development branches**.
- Do not use the current production project as a staging database. Keep its schema, data, and connection secrets unchanged.
- The Supabase security advisor returned 10 informational findings named `rls_enabled_no_policy`: RLS is enabled with no policies on nine `paper_ledger` tables and on `public.audit_events`. Supabase documents that this pattern blocks read/write access through the Supabase API unless a policy is present. This can be intentional default-deny behavior; **do not add permissive policies merely to silence the notice**. Source: https://supabase.com/docs/guides/observability/advisors?lint=0008_rls_enabled_no_policy
- Read-only inspection of table grants found the `paper_ledger` tables granted to database role `postgres`; `public.audit_events` is granted to `postgres` and `service_role`. No table-level grants for `anon` or `authenticated` were found on those tables. Do not change grants without first confirming the backend connection role in the secret manager (without printing the DSN or password) and running an authenticated ledger smoke test.

## Cost-safe staging database options

1. **Prefer evaluating a second Free Supabase project before any paid branch.** Supabase's current documentation says Free supports up to two active Free projects with 500 MB database storage each; Free projects can pause after a week of low activity. Check the dashboard's quoted cost and available project slot before creation. No project has been created. Source: https://supabase.com/kb/faqs/platform/database-only-cost
2. **Do not create a Supabase preview branch without a cost check.** Branch usage is billed separately and is not covered by Spend Cap; the documented starting rate for a Micro branch is $0.01344/hour (about $9.68 for 30 days at 720 hours, before other applicable usage). Source: https://supabase.com/docs/guides/platform/manage-your-usage/branching
3. A Northflank Developer Sandbox advertises up to $20/month of included resources, but its pricing page says “No card needed” while its billing documentation says a payment method is required before creating resources. Resolve this account-level conflict first. Two continuously running 0.5-vCPU/1-GB services at the published $12/month each would total about $24/month before considering the sandbox credit, so don't assume an always-on API plus a separate worker fits inside $20. Sources: https://northflank.com/pricing and https://northflank.com/docs/v1/application/billing/pricing-on-northflank
4. Railway Free is capped at 0.5 GB RAM per service; its post-trial monthly credit is $1. Fly.io's published 1-GB shared-cpu-4x compute is $8.78/month before storage/network. These are not zero-cost migration approvals. Sources: https://docs.railway.com/pricing/plans and https://fly.io/pricing-update/

## Staging release gate

Before any staging resource is created:
- Confirm the hosting plan, actual included resource limits, account payment-method requirements, alerts/budget controls, and quoted maximum charge.
- Confirm the staging database is separate from production and has no production secrets/data.
- Use a separate staging `PAPER_API_TOKEN` of at least 32 characters; store it only in the host's secret store.
- Start on Futures Testnet public market data with paper-only worker settings; every market event must retain `orders_authorized=false`.
- Validate all paper risk inputs and symbol/interval/candle/poll/freshness configuration pairs. Missing or contradictory inputs must fail closed.
- Run authenticated HTTP and WebSocket tests, verify stale snapshots are rejected, and confirm restart/persistence behavior.
- Keep PR #41 and PR #42 unmerged until hosted staging passes. Render auto-deploys from `main`, so a merge is a production deployment gate.

**Current status:** staging is not provisioned; no paid resources, branches, billing changes, or production changes were made.
