#!/bin/bash
# One-time setup script. Run once on your VPS as the bot user.
# Usage: chmod +x setup.sh && ./setup.sh

set -e
BOTDIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Creating virtual environment..."
python3 -m venv "$BOTDIR/venv"
source "$BOTDIR/venv/bin/activate"

echo "==> Installing dependencies..."
pip install --upgrade pip -q
pip install -r "$BOTDIR/requirements.txt" -q

echo "==> Checking .env..."
if [ ! -f "$BOTDIR/.env" ]; then
    cp "$BOTDIR/.env.example" "$BOTDIR/.env"
    echo "    IMPORTANT: Edit $BOTDIR/.env and fill in your keys before starting."
    exit 1
fi

echo "==> Installing systemd service..."
# Patch the service file with the actual bot directory and current user
SERVICE_SRC="$BOTDIR/polymarket_bot.service"
SERVICE_DEST="/etc/systemd/system/polymarket_bot.service"
CURRENT_USER="$(whoami)"

sed "s|/home/ubuntu|$HOME|g; s|User=ubuntu|User=$CURRENT_USER|g" \
    "$SERVICE_SRC" | sudo tee "$SERVICE_DEST" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable polymarket_bot
sudo systemctl start polymarket_bot

echo ""
echo "==> Bot is running! Useful commands:"
echo "    sudo systemctl status polymarket_bot    # check status"
echo "    sudo systemctl stop polymarket_bot      # stop bot"
echo "    sudo systemctl restart polymarket_bot   # restart bot"
echo "    tail -f $BOTDIR/polymarket_bot.log      # live logs"
