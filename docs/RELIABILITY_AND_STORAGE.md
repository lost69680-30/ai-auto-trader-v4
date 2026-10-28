# Solo V4 — Reliability and storage status

## Current safety posture

The project is paper-only for development and testing. The independent risk gate is fail-closed; `orders_authorized` remains false. No live order-placement route is part of the read-only API.

The SQLite ledger supports idempotent paper intent records, durable audit rows, explicit cancellation of pending paper intents when process-local trigger state cannot be restored, and emergency-stop state that survives reopening the *same database file*. Startup verifies SQLite integrity before running the paper-service composition. These properties do not make a temporary filesystem durable.

## Current Render limitation

The configured Render service runs on the Free plan and currently points `PAPER_DATABASE_PATH` at `/tmp/v4data/paper-ledger.sqlite3`. The API reports `state_storage.durability=EPHEMERAL`, `state_storage.survives_restart=false`, and a warning in its status and state responses. Ledger history and emergency-stop state may be lost on a host restart, redeploy, or spin-down.

Before relying on recovery for any stateful paper-runtime deployment, move the ledger to verified persistent storage (a supported persistent disk on an eligible service or an external durable database), verify backups/restart behavior, and confirm emergency-stop state survives that exact deployment path. Do not create or upgrade paid infrastructure without authorization.

## Operator checks

1. Read `GET /api/v1/status` and inspect `state_storage`.
2. Treat `EPHEMERAL` as an explicit warning and `UNVERIFIED` as not proven durable.
3. Do not use the persisted emergency stop as a cross-restart guarantee until storage is verified.
4. After storage is migrated, test emergency-stop activation, service restart, status readback, and audit retention.
