# Solo staging backup — GitHub Actions + AgePony (Android)

This is the phone-friendly alternative to Termux. It uses a manually triggered GitHub Actions run to read **only** the Supabase staging `paper_ledger` schema, encrypt the dump to an AgePony key held on your phone, prove an encrypted round-trip and restore into a disposable PostgreSQL service, then upload only the encrypted archive for one day.

## Important current blocker — do not run yet

The latest read-only catalog check (2026-11-03) found 15 staging tables, all with RLS, but only 14 have the required `solo_backup_reader_select` policy. The missing table is `market_intelligence_reports`. Additional read-only checks confirmed its owner is `postgres`; `anon`, `authenticated`, and `service_role` have no SELECT grant on it; the private `paper_ledger` schema has no USAGE grant to those roles; and `solo_backup_reader` is not superuser, BYPASSRLS, or INHERIT. It has SELECT on this table but no matching policy, so the workflow refuses to dump.

If the project owner approves granting the dedicated backup role visibility into every row of this report table, the narrowly scoped SQL would be:

```sql
CREATE POLICY solo_backup_reader_select
ON paper_ledger.market_intelligence_reports
FOR SELECT
TO solo_backup_reader
USING (true);
```

This policy would not grant access to `anon`, `authenticated`, or `service_role`, and it adds no write privileges. However, it would let the dedicated login role read all report rows. It has **not** been applied. Do not weaken RLS or use a privileged database account to bypass this check.

The existing GitHub workflow was previously capable of uploading an **unencrypted** dump. It has been replaced with an encrypted, fail-closed workflow. Do not run older workflow versions or download any old plaintext artifact; if one exists, delete it immediately from Actions artifacts and treat its contents as exposed.

## Zero-cost gate

GitHub documents standard hosted runners as free for public repositories, but artifact storage has quotas and overage rules. The workflow therefore requires a manual confirmation that free artifact storage is available, limits the encrypted file to 200 MiB, and retains it for one day. Check the repository's Actions storage usage and the account's billing/usage page before each run. If available free storage cannot be verified, do not run it. No paid runner, upgrade, trial, or payment method is authorized.

## 1. Install AgePony on Android

Install the open-source AgePony app from its official F-Droid listing: https://f-droid.org/en/packages/com.agepony.app/

Inside AgePony, create a new age identity/key pair. Keep the **private identity** only in AgePony's protected vault or a separate offline safe backup. Copy only the public recipient string beginning with `age1`. Never export or paste the private identity into GitHub, this repository, ChatGPT, a workflow log, or a screenshot.

## 2. Add two GitHub Actions secrets

Open the repository → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

Add:

- `SOLO_BACKUP_DATABASE_URL`: the staging Supabase session-pooler connection URI for the dedicated `solo_backup_reader` database role. Use the staging project only and ensure TLS is required (`sslmode=require`). Never use production credentials, the postgres owner, service-role credentials, or a role with BYPASSRLS.
- `SOLO_BACKUP_AGE_RECIPIENT`: the public `age1...` recipient copied from AgePony. This is not the private key.

Never put either secret in a file, issue, chat, workflow input, or commit. Do not send either value to ChatGPT.

## 3. Resolve the RLS blocker before running

The current staging catalog has 15 tables, 15 with RLS, but only 14 with the explicit backup-reader SELECT policy. The missing table is `market_intelligence_reports`. Review the existing policy design and create only a narrowly scoped policy for `solo_backup_reader` if authorized. Then confirm every `paper_ledger` table has RLS enabled and the expected `solo_backup_reader_select` SELECT policy. Do not add a broad policy for `anon`, `authenticated`, `service_role`, or `PUBLIC`.

No migration is applied by the backup workflow.

## 4. Run from GitHub on your phone

The updated workflow is on the repository's default `main` branch, so it is available under **Actions → Solo staging PostgreSQL backup → Run workflow**.

Before starting:
1. Verify the GitHub Actions storage page shows enough included free capacity; remember package storage may share the allowance.
2. Choose the branch `main`.
3. Tick both confirmations: staging-only and free artifact storage.
4. Run the workflow once.

The workflow stops if the database role is not exactly `solo_backup_reader`, any table lacks RLS or the explicit SELECT policy, the dump fails, the age round-trip fails, the disposable PostgreSQL restore fails, the encrypted archive exceeds 200 MiB, or the recipient secret is invalid. It never connects to production.

## 5. Download and decrypt on the phone

After a successful run, open the run's **Artifacts** section and download `solo-staging-paper-ledger-encrypted-<run-id>`. Save the ZIP in a private folder, extract it, and verify the SHA-256 file using a trusted local checksum utility if available.

Open the `.age` file in AgePony and decrypt it using the private identity kept in the app. Save the resulting `.dump` only in private phone storage. Keep the decrypted archive and private identity protected. Delete the Actions artifact after the encrypted archive is safely stored on the phone; its one-day retention is only a backstop.

## What the workflow verifies

- The connection authenticates as `solo_backup_reader`, not a privileged owner.
- All current `paper_ledger` tables have RLS and the dedicated SELECT policy before export.
- The dump is created with row security enabled.
- The encrypted artifact is encrypted to the public key whose private identity stays on the phone.
- A separate ephemeral runner key verifies age encryption/decryption, and a disposable PostgreSQL 17 service proves the archive can restore. That temporary key is deleted and never uploaded.
- Only the encrypted archive and its checksum are uploaded; retention is one day.

The disposable restore drill is not a production restore and does not alter staging or production. It is still necessary to preserve the downloaded encrypted backup and its private key safely. The workflow does not migrate the database or prove a rollback of the opportunity-registry migration.

## Stop conditions

Do not run if the RLS policy gap remains, the backup role has BYPASSRLS or write privileges, free artifact capacity is uncertain, either secret is missing, or the app cannot safely retain the private identity. Never send passwords, private keys, backup files, or decrypted data to ChatGPT.
