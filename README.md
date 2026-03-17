# 🎮 Verno's Discord Heist Bot

A lightweight Discord bot for managing GTA Online heist queues with real-time updates and automatic thread creation.

## Features

✅ **Slash Commands** — Easy-to-use `/queue_status`, `/setup_heist_panel`, `/clear_heist_queue`  
✅ **Real-time Queue Updates** — See player names and progress bars instantly  
✅ **Automatic Thread Creation** — Private threads spawn at 3/3 players  
✅ **Persistent Storage** — Queue state saved in Discord pinned messages  
✅ **MongoDB-Free** — No database required, 100% Discord-native storage  
✅ **Multi-Timezone Support** — All timestamps in UTC for universal clarity  
✅ **Python 3.14 Compatible** — Future-proof with audioop stub injection  

## Supported Heists

- Casino
- Pacific
- Doomsday
- Cayo Perico

## Setup

### Prerequisites
- Python 3.13+
- Discord Bot Token
- Your Discord User ID (for owner-only commands)

### Installation

```bash
git clone https://github.com/Nivedh555/Verno-s-Discord-Bot.git
cd Verno-s-Discord-Bot
python -m venv .venv
.venv\Scripts\activate  # On Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration

Create a `.env` file:

```env
DISCORD_TOKEN=your_bot_token_here
BOT_OWNER_ID=your_discord_user_id
```

### Running the Bot

**Local:**
```bash
python bot.py
```

**Production (Oracle Cloud / Linode):**
```bash
nohup python bot.py &
# Or use systemd / supervisor for auto-restart
```

## Commands

### `/queue_status`
Shows current queue status for all heists (anyone can use)

### `/setup_heist_panel` (Owner Only)
Posts the interactive heist queue panel in a channel

**Usage:**
```
/setup_heist_panel channel:#general
```

### `/clear_heist_queue` (Owner Only)
Clear individual heist queue or all queues at once

**Options:**
- `Casino` / `Pacific` / `Doomsday` / `Cayo Perico` — Clear specific heist
- `All` — Clear all queues

## How It Works

1. **Setup Panel** — Owner runs `/setup_heist_panel` to create the queue dropdown
2. **Join Queue** — Players select a heist and enter their Rockstar name
3. **Real-time Updates** — Embed updates instantly as players join
4. **Auto Thread** — At 3/3 players, a private thread spawns with UTC timestamp
5. **Persistence** — Queue state recovers from Discord pinned messages on restart

## Deployment

### Free Option: Oracle Cloud (12 months)
- Always-free tier: oracle.com/cloud/free
- 3500 compute hours/month = unlimited for 1 bot
- Then ~$5-10/month if you continue

### Paid Option: Linode ($5/month)
- Most reliable, simple setup
- Full root access

## Architecture

- **Storage:** In-memory dict + Discord pinned messages (no database)
- **Framework:** discord.py 2.3.2
- **Python:** 3.13.7+ (with Python 3.14 compatibility shims)
- **Dependencies:** discord.py, python-dotenv only

## Troubleshooting

**Bot doesn't respond to commands?**
- Ensure bot has `applications.commands` OAuth2 scope
- Verify `DISCORD_TOKEN` and `BOT_OWNER_ID` in `.env`

**Queue not updating?**
- Check bot has permissions in the channel
- Verify embed panel message ID is tracked correctly

**404 Unknown Interaction errors?**
- Fixed: Bot now defers() immediately, then processes
- If still occurring, restart the bot

## License

MIT License — Use freely!

## Support

Issues? Suggestions?
- Message: @verno on Discord
- Issues tab on GitHub

---

**Built with ❤️ for GTA Online heists**
