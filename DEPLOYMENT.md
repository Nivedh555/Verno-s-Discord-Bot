# Deploying the Queue Bot (free, 24/7, public)

This guide deploys the bot to a **free Oracle Cloud Always-Free VM** with
**PostgreSQL running on the same machine**. Because the bot and database are
co-located, queries are ~0 ms and the bot is fast for users in **every country**
(Discord handles each user's distance, not your database).

You do this **once**. After that the bot runs 24/7 and anyone can invite it.

---

## Overview

```
┌─────────────────────────────────────────────────────────┐
│  Oracle Always-Free ARM VM  (Ubuntu, an India region)    │
│                                                          │
│   systemd ─▶ queue-bot (python -m bot.main)              │
│                     │ localhost (~0 ms)                  │
│                     ▼                                    │
│              PostgreSQL                                  │
└─────────────────────────────────────────────────────────┘
         ▲                                   ▲
         │ Discord gateway (outbound)        │ users worldwide reach
         └───────────────────────────────────┘ Discord's edge, not your DB
```

---

## Step 1 — Push the code to GitHub

The VM pulls the code from your repo. From your PC, commit and push the `bot/`
package (your `.env` is gitignored and stays private):

```bash
git add bot/ run.py requirements.txt deploy-cloud.sh heist-bot.service scripts/ LEGAL/ *.md
git commit -m "Multi-tenant queue bot"
git push
```

## Step 2 — Create the Oracle Always-Free VM

1. Sign up at <https://www.oracle.com/cloud/free/>. Choose a **home region in
   India** (Mumbai or Hyderabad) — this is permanent, so pick it carefully.
2. **Compute → Instances → Create Instance.**
   - Image: **Ubuntu 22.04 or 24.04**.
   - Shape: **Ampere (ARM) `VM.Standard.A1.Flex`**, 1–2 OCPU, 6–12 GB RAM
     (all within Always-Free).
   - Add your **SSH public key** (so you can log in).
   - Create. If you see *"Out of capacity"*, retry later or try the other India
     region — free ARM is in high demand.
3. Note the instance's **public IP**.

## Step 3 — Connect and deploy

```bash
ssh ubuntu@YOUR_PUBLIC_IP

# clone your repo
git clone https://github.com/<you>/<your-repo>.git queue-bot
cd queue-bot

# run the installer (installs Postgres + Python + systemd service)
chmod +x deploy-cloud.sh
./deploy-cloud.sh
```

The script installs PostgreSQL, creates the database, builds the venv, installs
dependencies, and registers the `queue-bot` systemd service.

## Step 4 — Add your token and start

```bash
nano .env          # set DISCORD_TOKEN=...  (DATABASE_URL is already filled in)
sudo systemctl start queue-bot
sudo journalctl -u queue-bot -f     # watch it boot
```

You're looking for: `Database pool ready and schema applied` and
`Online as ... across N guild(s)`.

## Step 5 — Invite the bot to any server

Use this invite URL (replace `CLIENT_ID` with your application's client ID from
the Discord Developer Portal). The permissions integer grants exactly what the
bot needs, including **Create Private Threads** and **Manage Threads** for
sessions:

```
https://discord.com/api/oauth2/authorize?client_id=CLIENT_ID&permissions=397284472384&scope=bot%20applications.commands
```

In each server an admin runs `/setup`, then `/activity add` and `/panel`.

## Step 6 (recommended) — Automatic backups

Self-hosted Postgres means you own backups. Install a daily `pg_dump`:

```bash
chmod +x scripts/setup_backups.sh
./scripts/setup_backups.sh
```

Keeps the last 7 daily compressed dumps in `./backups/`.

---

## Day-to-day operations

| Task | Command |
| --- | --- |
| Status | `sudo systemctl status queue-bot` |
| Restart | `sudo systemctl restart queue-bot` |
| Stop | `sudo systemctl stop queue-bot` |
| Live logs | `sudo journalctl -u queue-bot -f` |
| Update code | `git pull && sudo systemctl restart queue-bot` |
| Update deps | `.venv/bin/pip install -r requirements.txt && sudo systemctl restart queue-bot` |

## Notes & gotchas

- **No inbound ports needed.** The bot only makes outbound connections (to
  Discord) and talks to Postgres on `localhost`, so you don't open firewall
  ports for it.
- **Free ARM capacity** can be scarce — retry instance creation if needed.
- **Keep the VM in your free limits:** one A1 instance ≤ 2 OCPU / 12 GB total.
- **Privileged intents:** not required. Set `MEMBERS_INTENT=true` in `.env`
  (and enable it in the Developer Portal) only if you want faster member
  resolution in large servers.
- **100+ servers:** Discord requires bot verification (ID check + intent review)
  past 100 servers. The `LEGAL/PRIVACY_POLICY.md` and `TERMS_OF_SERVICE.md` here
  cover the policy requirement.

## Troubleshooting

**Bot won't start** — `sudo journalctl -u queue-bot -n 50`. Most often a missing
`DISCORD_TOKEN` or a wrong `DATABASE_URL` in `.env`.

**Database connection errors** — confirm Postgres is up
(`sudo systemctl status postgresql`) and that `DATABASE_URL` in `.env` matches
the role/db the installer created (`postgresql://queuebot:...@localhost:5432/queuebot`).

**Slash commands not appearing** — global sync can take up to an hour. For an
instant test in one server, set `DEV_GUILD_ID=<server id>` in `.env` and restart.
