# Solo staging PostgreSQL backup — Android / Termux

**Purpose:** create an off-project logical backup of the staging-only `paper_ledger` schema before any staging migration. This procedure does not alter the database.

## Safety rules

- Use only the Supabase project `solo-ai-auto-trader-staging` (project ref `gryjtjobkmredawmymwy`).
- Never use the production project.
- Never paste a database password, connection URI containing a password, API key, or backup file into ChatGPT, GitHub, an issue, or a public repository.
- Do not run `DROP`, `TRUNCATE`, `DELETE`, `UPDATE`, `INSERT`, `ALTER`, `CREATE`, `supabase db push`, or any restore command against the staging or production database during backup.
- The backup must be encrypted before it is copied out of Termux's private home directory.
- This is a manual user-run procedure. No backup has yet been taken by this project workflow.

## 1. Install tools in Termux

Run:

```bash
pkg update
pkg install postgresql age
```

If Termux asks to update packages, accept only the normal free package installation. No paid subscription or service is needed.

## 2. Get staging connection details privately

In Supabase Dashboard, open **solo-ai-auto-trader-staging** → **Connect**. Choose the **Session pooler** connection details. Use the pooler host, port, database name and username exactly as shown. Do not copy the password into the URI.

The session pooler is preferred for mobile networks that may not support direct database IPv6 connectivity. If the dashboard provides a different port, use that exact port.

## 3. Create a private, read-only logical dump

Copy and run the following in Termux. Enter the connection details when prompted. The database password is entered through a hidden prompt and is not part of shell history.

```bash
set -euo pipefail
umask 077
mkdir -p "$HOME/solo-backups"
cd "$HOME/solo-backups"

read -r -p "Staging session-pooler host: " PGHOST
read -r -p "Staging session-pooler port: " PGPORT
read -r -p "Staging database username: " PGUSER
read -r -p "Staging database name (usually postgres): " PGDATABASE
export PGHOST PGPORT PGUSER PGDATABASE
export PGSSLMODE=require
read -r -s -p "Staging database password (hidden): " PGPASSWORD
echo
export PGPASSWORD

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP="$HOME/solo-backups/solo-staging-paper-ledger-$STAMP.dump"
ENCRYPTED="$DUMP.age"
VERIFY="$DUMP.verify"

pg_dump --format=custom --no-owner --no-acl --schema=paper_ledger --file="$DUMP"

unset PGPASSWORD
pg_restore --list "$DUMP" >/dev/null
age --passphrase --output "$ENCRYPTED" "$DUMP"

# Verify that the encrypted copy decrypts to a valid PostgreSQL archive.
age --decrypt --output "$VERIFY" "$ENCRYPTED"
pg_restore --list "$VERIFY" >/dev/null

sha256sum "$ENCRYPTED"
rm -f "$DUMP" "$VERIFY"
chmod 600 "$ENCRYPTED"
echo "Encrypted staging backup created: $ENCRYPTED"
```

When age prompts for a passphrase, choose a strong unique passphrase and store it somewhere safe outside the phone. Without it, the encrypted backup cannot be recovered. Do not reuse the database password.

## 4. Copy only the encrypted archive to Android shared storage

```bash
termux-setup-storage
mkdir -p "$HOME/storage/shared/Documents/SoloBackups"
cp "$ENCRYPTED" "$HOME/storage/shared/Documents/SoloBackups/"
sha256sum "$HOME/storage/shared/Documents/SoloBackups/$(basename "$ENCRYPTED")"
```

Compare the SHA-256 value with the value printed in Step 3. Keep the encrypted archive and passphrase in separate safe places. Do not upload the dump to the public GitHub repository.

## What this backup covers — and does not cover

- It exports schema definitions and rows in `paper_ledger`, including the candle archive and paper ledger records.
- It is not a full Supabase project backup. It does not include Storage bucket objects, Auth users, project settings, Edge Function secrets, or every managed Supabase schema.
- It does not test restoration. `pg_restore --list` verifies that the archive can be read, not that a restore has been successfully rehearsed.
- Before applying the opportunity-registry migration, restore this archive into a disposable local PostgreSQL instance and verify the tables/data there. Never rehearse restoration against staging or production.
- The archive may contain trading history and market data; keep it private and encrypted.

## Stop conditions

Stop and do not migrate if the dump fails, the encrypted copy cannot be verified, the checksum differs after copying, the passphrase is lost, or the restore rehearsal has not succeeded. Do not send credentials or backup contents to anyone for troubleshooting.
