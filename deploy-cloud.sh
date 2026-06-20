#!/bin/bash
# One-shot deploy for the multi-tenant queue bot on an Oracle Cloud Always-Free
# Ubuntu VM. Installs PostgreSQL locally (co-located with the bot for ~0ms
# queries), sets up a Python venv, creates the database, and installs a systemd
# service that runs the bot 24/7 with auto-restart.
#
# Usage (run from the cloned repo directory, as a sudo-capable user e.g. ubuntu):
#   chmod +x deploy-cloud.sh
#   ./deploy-cloud.sh
#
# Idempotent: safe to re-run. It will NOT overwrite an existing .env.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="$(whoami)"
SERVICE_NAME="queue-bot"
DB_NAME="queuebot"
DB_USER="queuebot"

echo "==> Deploying queue bot from: $APP_DIR (user: $RUN_USER)"

# --- 1. System packages ------------------------------------------------------
echo "==> Installing system packages (python, venv, postgresql)..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git postgresql postgresql-contrib openssl

# --- 2. PostgreSQL: ensure running, create role + database -------------------
echo "==> Configuring PostgreSQL..."
sudo systemctl enable --now postgresql

# Create the role with a random password (only if it doesn't already exist).
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1; then
    DB_PASS="$(openssl rand -hex 24)"
    sudo -u postgres psql -c "CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';"
    echo "==> Created DB role '${DB_USER}'."
    NEW_DB_CREDS=1
else
    echo "==> DB role '${DB_USER}' already exists (leaving password unchanged)."
    NEW_DB_CREDS=0
fi

# Create the database owned by that role (only if it doesn't exist).
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
    sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}"
    echo "==> Created database '${DB_NAME}'."
fi

DATABASE_URL="postgresql://${DB_USER}:${DB_PASS:-CHANGE_ME}@localhost:5432/${DB_NAME}"

# --- 3. Python virtual environment + dependencies ----------------------------
echo "==> Setting up Python virtual environment..."
if [ ! -d "$APP_DIR/.venv" ]; then
    python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# --- 4. .env -----------------------------------------------------------------
if [ ! -f "$APP_DIR/.env" ]; then
    echo "==> Creating .env (you must add your DISCORD_TOKEN)."
    cat > "$APP_DIR/.env" <<EOF
DISCORD_TOKEN=PUT_YOUR_BOT_TOKEN_HERE
DATABASE_URL=${DATABASE_URL}
# Optional:
# DEV_GUILD_ID=
# MEMBERS_INTENT=false
EOF
    echo "    -> Edit $APP_DIR/.env and set DISCORD_TOKEN."
else
    echo "==> .env already exists; not overwriting."
    if [ "${NEW_DB_CREDS}" = "1" ]; then
        echo "    NOTE: a new DB password was generated. Update DATABASE_URL in .env to:"
        echo "    ${DATABASE_URL}"
    fi
fi

# --- 5. systemd service ------------------------------------------------------
echo "==> Installing systemd service '${SERVICE_NAME}'..."
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<EOF
[Unit]
Description=Multi-tenant Discord Queue Bot
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python -m bot.main
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

echo ""
echo "============================================================"
echo " Setup complete."
echo ""
echo " 1. Add your bot token:   nano ${APP_DIR}/.env"
echo " 2. Start the bot:        sudo systemctl start ${SERVICE_NAME}"
echo " 3. Watch the logs:       sudo journalctl -u ${SERVICE_NAME} -f"
echo " 4. (Optional) backups:   ./scripts/setup_backups.sh"
echo ""
echo " The database schema is created automatically on first start."
echo "============================================================"
