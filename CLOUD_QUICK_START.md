# 🎮 Heist Bot Cloud Deployment - Quick Reference

## TL;DR - Fastest Deployment Path

### Oracle Cloud (FREE)
```bash
# 1. Sign up: oracle.com/cloud/free
# 2. Create Ubuntu 22.04 instance
# 3. SSH into instance, then:

sudo apt update && sudo apt install -y python3.11 python3-pip python3-venv git
git clone https://github.com/Nivedh555/Steve-s-Discord-Bot.git
cd Steve-s-Discord-Bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 4. Create .env
nano .env
# Paste:
# DISCORD_TOKEN=your_token_here
# BOT_OWNER_ID=your_user_id_here
# Press Ctrl+O, Enter, Ctrl+X

# 5. Setup auto-restart
bash deploy-cloud.sh

# 6. Start bot
sudo systemctl start heist-bot
sudo systemctl status heist-bot
```

### Linode ($5/month)
Same as Oracle Cloud steps above, just different sign-up.

---

## Essential Commands

| Command | Purpose |
|---------|---------|
| `sudo systemctl start heist-bot` | Start bot |
| `sudo systemctl stop heist-bot` | Stop bot |
| `sudo systemctl restart heist-bot` | Restart bot |
| `sudo systemctl status heist-bot` | Check status |
| `sudo journalctl -u heist-bot -f` | View live logs |
| `sudo journalctl -u heist-bot -n 50` | View last 50 log lines |
| `git pull origin main` | Update code |

---

## Troubleshooting

### Bot won't start
```bash
sudo journalctl -u heist-bot -n 50
# Look for DISCORD_TOKEN error or permission denied
```

### Bot keeps restarting
```bash
# Check if .env exists and has correct format
cat .env
# Make sure no syntax errors in bot.py
python3 -m py_compile bot.py
```

### Commands don't work
```bash
# Restart bot
sudo systemctl restart heist-bot
# Wait 30 seconds
# Try slash command again
```

### View current bot IP
```bash
hostname -I
```

---

## Testing After Deployment

1. ✅ Go to your Discord server
2. ✅ Type `/heist` 
3. ✅ Should see the panel with "When 4 players join..."
4. ✅ Try adding yourself to queue
5. ✅ Check logs: `sudo journalctl -u heist-bot -n 5`

---

## Cost Estimate

| Provider | Cost | Ideal For |
|----------|------|-----------|
| Oracle Cloud | FREE (12 mo) | Testing, small servers, always-on trials |
| Linode | $5/month | Reliable, production-ready |
| AWS | ~$10/month | Large scale, but overkill for this bot |

---

## File Reference

- `DEPLOYMENT.md` - Full deployment guide
- `deploy-cloud.sh` - Auto-setup script
- `heist-bot.service` - Systemd config
- `bot.py` - Main bot code
- `requirements.txt` - Dependencies

---

## Getting Help

1. Check logs: `sudo journalctl -u heist-bot -f`
2. Re-read DEPLOYMENT.md for your cloud provider
3. Verify .env: `cat .env | grep DISCORD_TOKEN`
4. Test locally first: `python3 bot.py`
