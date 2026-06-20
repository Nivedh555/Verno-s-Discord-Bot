# 🎯 Queue Bot

A free, **multi-tenant** Discord bot for running self-service queues — invite it
to any server, configure it per-server, and when a queue fills it spins up a
private session thread automatically. Built to be hosted free and 24/7.

Originally a single-server GTA Online heist-queue bot, now generalized so **any
server** can define its own activities (heists, scrims, raids, ranked lobbies —
anything) with their own capacities, cooldowns and limits.

## Features

- **Multi-tenant** — one bot instance serves every server it's invited to; each
  configures itself independently. No hard-coded IDs.
- **Configurable activities** — `/activity add` defines a queue with its own
  capacity, cooldown, daily limit and icon.
- **Auto sessions** — when a queue fills, a private thread is created and the
  players are added.
- **Per-server settings** — panel channel, log channel, admin role, ping role,
  and a renamable optional in-game-name field.
- **Built-in rate limits** — per-user cooldown, per-user daily cap, and a
  per-activity post-session cooldown (configurable per activity).
- **Durable state** — everything lives in PostgreSQL, so queues and active
  sessions survive restarts (no fragile pinned-message parsing).
- **Scales** — uses `AutoShardedBot` for Discord's sharding requirement past
  ~1,000 servers.

## Architecture

```
bot/
  main.py        # AutoShardedBot, startup, cog loading
  config.py      # env + defaults
  db.py          # asyncpg pool, schema, queries (PostgreSQL)
  models.py      # dataclasses
  service.py     # queue/session business logic
  permissions.py # configurable admin-role check
  views.py       # persistent panel / modal / session views
  cogs/          # setup, activities, queue, meta commands
```

- **Language:** Python 3.10+ (tested on 3.14)
- **Library:** discord.py 2.4+
- **Database:** PostgreSQL (via `asyncpg`)

## Commands

**Admin** (`Manage Server`, or the configured admin role):
- `/setup` — configure the bot for the server
- `/config show | panel-channel | log-channel | admin-role | ping-role | ign-label | ign-required`
- `/activity add | remove | list`
- `/panel` — post the queue panel
- `/force-start`, `/clear`

**Everyone:**
- Join/leave via the panel dropdown and button
- `/help`, `/about`

## Running it

**Deploy (recommended)** — free, 24/7, public. See **DEPLOYMENT.md**: an Oracle
Cloud Always-Free VM running the bot + PostgreSQL on the same machine.

**Local (development):**
```bash
python -m venv .venv
.venv\Scripts\activate          # Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
# create .env (see .env.example) with DISCORD_TOKEN and DATABASE_URL
python run.py                    # or: python -m bot.main
```

`.env` keys are documented in `.env.example`. You need a PostgreSQL database
(local install or a hosted one) reachable via `DATABASE_URL`.

## How it works

1. An admin runs `/setup`, then `/activity add` (or keeps the defaults) and `/panel`.
2. Players pick an activity from the dropdown and (optionally) enter an in-game name.
3. The panel updates live; when a queue reaches its capacity, a private session
   thread is created with the players.
4. Rate limits (cooldowns, daily caps) are enforced from the database.
5. An admin marks the session complete (button), which closes the thread and
   starts that activity's cooldown.

## Legal

`LEGAL/PRIVACY_POLICY.md` and `LEGAL/TERMS_OF_SERVICE.md` — required for public
listing and Discord's 100+ server verification. Data is deleted automatically
when the bot is removed from a server.

## License

MIT.
