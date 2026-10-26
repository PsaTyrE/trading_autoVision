#!/bin/bash
# One-shot installer for the tradingbot systemd service. Run ONCE on the Pi with sudo:
#   sudo bash /home/pi/latihan/deploy/install_service.sh
set -e
SRC="$(dirname "$0")/tradingbot.service"
DEST="/etc/systemd/system/tradingbot.service"

if [ ! -f "$SRC" ]; then
  echo "[!] $SRC not found"; exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "[!] Run with sudo:  sudo bash $0"; exit 1
fi

install -m 644 "$SRC" "$DEST"
systemctl daemon-reload
systemctl enable tradingbot
echo "[OK] tradingbot.service installed + enabled (starts on boot)."
echo "     Start now:   sudo systemctl start tradingbot"
echo "     Status:      systemctl status tradingbot"
echo "     Logs:        journalctl -u tradingbot -f"
echo "     NOTE: bot stays OFF on boot (AUTO_START_BOT=false) until you press Start in the dashboard."
