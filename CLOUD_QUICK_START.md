# Quick Reference — Deploy & Operate

Fast commands. For the full walkthrough see **DEPLOYMENT.md**.

## Deploy (Oracle Always-Free VM, India region)

```bash
# On the VM (Ubuntu), after SSH:
git clone https://github.com/<you>/<your-repo>.git queue-bot
cd queue-bot
chmod +x deploy-cloud.sh
./deploy-cloud.sh                 # installs Postgres + Python + systemd service

nano .env                         # set DISCORD_TOKEN= ...
sudo systemctl start queue-bot
sudo journalctl -u queue-bot -f   # watch it come online
```

The database (PostgreSQL) runs on the **same VM** as the bot, so queries are
~0 ms and it's fast for users in every country. The schema is created
automatically on first start.

## Essential commands

| Command | Purpose |
| --- | --- |
| `sudo systemctl start queue-bot` | Start |
| `sudo systemctl stop queue-bot` | Stop |
| `sudo systemctl restart queue-bot` | Restart |
| `sudo systemctl status queue-bot` | Status |
| `sudo journalctl -u queue-bot -f` | Live logs |
| `git pull && sudo systemctl restart queue-bot` | Update code |
| `./scripts/setup_backups.sh` | Install daily DB backups |

## In Discord (per server, by an admin)

1. `/setup` — choose the panel channel (+ optional log channel / admin role).
2. `/activity add` — define your queues (or use the defaults).
3. `/panel` — post the queue panel.

Players use the dropdown to join; a private session thread is created when a
queue fills.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Won't start | `sudo journalctl -u queue-bot -n 50` — usually missing `DISCORD_TOKEN` |
| DB errors | `sudo systemctl status postgresql`; verify `DATABASE_URL` in `.env` |
| Commands missing | Global sync can take ~1h; set `DEV_GUILD_ID` in `.env` for instant sync in one server |
| Sessions don't create | Bot needs **Create Private Threads** + **Manage Threads** in the channel |

## File reference

- `DEPLOYMENT.md` — full guide
- `deploy-cloud.sh` — installer (Postgres + venv + systemd)
- `heist-bot.service` — reference systemd unit
- `scripts/setup_backups.sh` — daily `pg_dump` backups
- `scripts/reset_runtime.py` — clear queues/sessions/usage for a clean test
- `bot/` — the bot (run with `python -m bot.main` or `python run.py`)
- `requirements.txt` — dependencies
