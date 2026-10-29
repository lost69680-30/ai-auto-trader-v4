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

## Hosting baseline and alternatives

| Option | Published cost / constraint | Assessment |
|---|---|---|
| Existing Render Free | $0; sleeps after 15 minutes with no inbound HTTP request or inbound WebSocket message. Free services have ephemeral filesystems, cannot run background workers or cron jobs as separate paid service types, and can be restarted by Render. | Keep only for current read-only preview; not the full always-on target. Source: https://render.com/docs/free |
| Render paid web service | Published pricing lists a 0.5-CPU / 1-GB web service at $19/month. Paid compute removes Free-tier sleep limitations. Cron jobs are billed by runtime with a $1/month minimum per job. | **Lowest migration risk**: keep existing Render ↔ GitHub integration and Supabase; this is the price baseline to compare with alternatives. A likely minimum baseline is about $20/month for one 1-GB API/continuous-worker service and one short cron job, before extra bandwidth or additional compute. Confirm the exact plan/pricing in the account before any upgrade. Sources: https://render.com/pricing and https://render.com/docs/cronjobs |
| Northflank Developer Sandbox | Advertises up to $20/month included resources, 2 services, 2 jobs and 1 database. Pricing page says “No card needed” while billing docs say payment method required before creating resources. At published rates, two continuously running 0.5-vCPU / 1-GB services at $12/month each would be about $24/month before considering the sandbox credit. | Candidate only after resolving the card requirement and billing controls; don't assume API + separate always-on worker will fit within $20. Sources: https://northflank.com/pricing and https://northflank.com/docs/v1/application/billing/pricing-on-northflank |
| Railway Free | Post-trial credit is $1/month, with 0.5 GB RAM maximum per service. | Not a reliable basis for the full analysis-worker workload. Source: https://docs.railway.com/pricing/plans |
| Fly.io shared compute | Published 1-GB shared-cpu-4x compute is $8.78/month before storage and network. | Low entry compute price, but migration effort, storage, network and runtime sizing must be added. Source: https://fly.io/pricing-update/ |

The Render paid estimate above assumes the API and continuous research worker stay in the same long-running service as documented in the code/runbook, with a separate short scheduled job. It is not a measured capacity guarantee. Run the workload in staging and check memory/CPU before choosing a smaller or larger plan.

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
