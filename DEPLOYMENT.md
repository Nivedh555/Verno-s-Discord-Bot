# 🚀 Cloud Deployment Guide

This guide covers deploying the bot to Oracle Cloud (free) or Linode (paid).

## Option 1: Oracle Cloud (FREE - 12 months always-free tier)

### Step 1: Create Oracle Cloud Account
1. Go to [oracle.com/cloud/free](https://oracle.com/cloud/free)
2. Sign up with your email
3. Create an account (includes $300 free credits + always-free tier)

### Step 2: Launch a Compute Instance
1. Go to **Compute → Instances**
2. Click **Create Instance**
3. Choose:
   - **Image:** Ubuntu 22.04 (always-free eligible)
   - **Shape:** Ampere (ARM-based, always-free eligible) - **VM.Standard.A1.Flex** - 4 OCPU recommended
   - **VCN and Subnet:** Keep defaults (create new if needed)
   - Add SSH key (download and save it!)
4. Click **Create**
5. Wait for instance to be **Running** (status green)

### Step 3: Connect to Your Instance
```powershell
# On Windows, use WSL or Git Bash
ssh -i your-private-key.key ubuntu@YOUR_INSTANCE_IP
```

### Step 4: Install Python and Dependencies
```bash
sudo apt update
sudo apt install -y python3.11 python3-pip python3-venv git
```

### Step 5: Clone & Setup Bot
```bash
git clone https://github.com/Nivedh555/Steve-s-Discord-Bot.git
cd Steve-s-Discord-Bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Step 6: Set Environment Variables
```bash
# Create .env file with your bot token and owner ID
nano .env
```
Paste:
```
DISCORD_TOKEN=your_bot_token_here
BOT_OWNER_ID=your_discord_user_id
```
Press `Ctrl+O`, Enter, `Ctrl+X` to save.

### Step 7: Test Bot (optional)
```bash
python bot.py
# Press Ctrl+C to stop after 10 seconds if it connects
```

### Step 8: Setup Auto-Restart with Systemd
```bash
# Create systemd service file
sudo nano /etc/systemd/system/heist-bot.service
```

Paste this:
```ini
[Unit]
Description=Discord Heist Queue Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/Steve-s-Discord-Bot
Environment="PATH=/home/ubuntu/Steve-s-Discord-Bot/.venv/bin"
ExecStart=/home/ubuntu/Steve-s-Discord-Bot/.venv/bin/python bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Then enable and start:
```bash
sudo systemctl enable heist-bot
sudo systemctl start heist-bot
sudo systemctl status heist-bot
```

### Step 9: Monitor Logs
```bash
# View live logs
sudo journalctl -u heist-bot -f

# View last 50 lines
sudo journalctl -u heist-bot -n 50
```

---

## Option 2: Linode ($5/month)

### Step 1: Create Linode Account
1. Go to [linode.com](https://linode.com)
2. Sign up (promo code **HEIST50** = $50 credit)

### Step 2: Create a Linode
1. Click **Create → Linode**
2. Choose:
   - **Image:** Ubuntu 22.04 LTS
   - **Region:** Pick closest to you
   - **Linode Plan:** Nanode 1GB ($5/month, always eligible)
   - **Root Password:** Create strong password
3. Click **Create Linode**

### Step 3: SSH Into Your Linode
```bash
ssh root@YOUR_LINODE_IP
```
Enter the root password you created.

### Step 4-9: Follow the same steps as Oracle Cloud (Steps 4-9 above)
The commands are identical for Linode.

---

## Verification Checklist

✅ Bot responds to `/heist` command on your Discord server  
✅ `sudo systemctl status heist-bot` shows **active (running)**  
✅ `sudo journalctl -u heist-bot -n 5` shows recent startup logs  
✅ Panel appears when you run `/heist` command  

---

## Troubleshooting

### Bot won't start
```bash
sudo journalctl -u heist-bot -n 50
# Check for DISCORD_TOKEN or BOT_OWNER_ID errors
```

### 404 Unknown Interaction
- Restart the bot: `sudo systemctl restart heist-bot`
- Wait 10 seconds, try command again

### Permission denied when cloning repo
```bash
ssh-keygen -t ed25519 -C "your_email@example.com"
# Add public key to GitHub SSH keys
# Then retry git clone with SSH URL
```

### Instance keeps stopping
- Check Oracle Cloud billing (if trial ended)
- Check Linode billing (if payment failed)
- Increase compute hours in your plan

---

## Updating the Bot

When you push updates to GitHub:

```bash
cd Steve-s-Discord-Bot
git pull origin main
sudo systemctl restart heist-bot
```

---

## Cost Breakdown

| Service | Cost | Notes |
|---------|------|-------|
| Oracle Cloud | FREE | 12 months always-free tier, then ~$5-10/month |
| Linode | $5/month | Cheapest paid option, promo codes available |
| Discord Bot | FREE | No limits for heist management |

---

## Support

Issues during deployment?
- Check logs: `sudo journalctl -u heist-bot -f`
- Verify `.env` has correct token: `cat .env`
- Restart bot: `sudo systemctl restart heist-bot`
