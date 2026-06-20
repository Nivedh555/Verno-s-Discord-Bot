#!/bin/bash
# Sets up an automatic daily backup of the bot's PostgreSQL database using
# pg_dump + a cron job. Keeps the last 7 days of compressed dumps in ./backups.
#
# Run once from the repo directory on the VM:
#   chmod +x scripts/setup_backups.sh
#   ./scripts/setup_backups.sh

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${APP_DIR}/backups"
DB_NAME="queuebot"
DB_USER="queuebot"

mkdir -p "${BACKUP_DIR}"

# Write the backup runner script. pg_dump accepts the full connection URI, so we
# just reuse DATABASE_URL straight from .env (no password parsing).
cat > "${APP_DIR}/scripts/run_backup.sh" <<EOF
#!/bin/bash
set -euo pipefail
STAMP=\$(date +%Y%m%d-%H%M%S)
DATABASE_URL=\$(grep -oP '(?<=^DATABASE_URL=).*' "${APP_DIR}/.env")
pg_dump "\${DATABASE_URL}" | gzip > "${BACKUP_DIR}/${DB_NAME}-\${STAMP}.sql.gz"
# Keep only the 7 most recent backups.
ls -1t "${BACKUP_DIR}"/${DB_NAME}-*.sql.gz | tail -n +8 | xargs -r rm --
EOF
chmod +x "${APP_DIR}/scripts/run_backup.sh"

# Install a daily cron job at 03:17 (off the hour on purpose).
CRON_LINE="17 3 * * * ${APP_DIR}/scripts/run_backup.sh >> ${APP_DIR}/backups/backup.log 2>&1"
( crontab -l 2>/dev/null | grep -v 'run_backup.sh' ; echo "${CRON_LINE}" ) | crontab -

echo "Daily backups installed. Dumps land in: ${BACKUP_DIR} (last 7 kept)."
echo "Test it now with: ${APP_DIR}/scripts/run_backup.sh"
