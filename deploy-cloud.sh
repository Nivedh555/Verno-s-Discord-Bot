#!/bin/bash
# Cloud Deployment Quick-Start Script
# Usage: ./deploy-cloud.sh

set -e

echo "🚀 Setting up Discord Heist Bot on Cloud..."

# Update system
echo "📦 Updating system packages..."
sudo apt update
sudo apt install -y python3.11 python3-pip python3-venv git

# Create bot directory
echo "📁 Creating bot directory..."
cd /home/ubuntu || cd ~
if [ ! -d "Steve-s-Discord-Bot" ]; then
    echo "📥 Cloning repository..."
    git clone https://github.com/Nivedh555/Steve-s-Discord-Bot.git
fi
cd Steve-s-Discord-Bot

# Create venv
echo "🐍 Setting up Python virtual environment..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate

# Install dependencies
echo "📚 Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Check for .env file
if [ ! -f ".env" ]; then
    echo ""
    echo "⚠️  .env file not found!"
    echo "Please create .env with:"
    echo "  DISCORD_TOKEN=your_token_here"
    echo "  BOT_OWNER_ID=your_user_id_here"
    echo ""
    echo "Then run: sudo systemctl start heist-bot"
    exit 1
fi

# Setup systemd service
echo "⚙️  Setting up systemd service..."
sudo cp heist-bot.service /etc/systemd/system/heist-bot.service
sudo sed -i "s|/home/ubuntu|$PWD|g" /etc/systemd/system/heist-bot.service
sudo systemctl daemon-reload
sudo systemctl enable heist-bot

echo ""
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "1. Create .env file: nano .env"
echo "   Add: DISCORD_TOKEN=your_token_here"
echo "        BOT_OWNER_ID=your_user_id_here"
echo ""
echo "2. Start bot: sudo systemctl start heist-bot"
echo "3. Check status: sudo systemctl status heist-bot"
echo "4. View logs: sudo journalctl -u heist-bot -f"
