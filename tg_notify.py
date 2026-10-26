#!/usr/bin/env python3
"""Telegram notifier for Pi-side bots. Usage: tg_notify.py \"message\"
Reads token+chat_id from .tg_creds (mode 600). Silent on any failure —
a dead notifier must never crash the trading loop."""
import sys
import urllib.request
import urllib.parse

def send(text: str) -> bool:
    try:
        creds = open("/home/pi/latihan/.tg_creds").read().split()
        token, chat_id = creds[0], creds[1]
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception:
        return False

if __name__ == "__main__":
    if len(sys.argv) > 1:
        send(sys.argv[1])
