import requests
from typing import Dict, Any
from src.config import config

class TelegramNotifier:
    def __init__(self):
        self.token = config.telegram_bot_token
        self.chat_id = config.telegram_chat_id
        self.enabled = bool(self.token and self.chat_id)

    def send_message(self, text: str):
        """Sends markdown formatted Telegram message."""
        if not self.enabled:
            return

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        # AUDIT FIX: Pi networks are slow; timeout=5 silently dropped alerts on
        # congestion. 10s + one retry so a critical alert (SL/TP/circuit breaker)
        # is not lost to a transient network blip.
        for attempt in (1, 2):
            try:
                requests.post(url, json=payload, timeout=10)
                return
            except Exception as e:
                if attempt == 2:
                    print(f"[!] Telegram alert error (after retry): {e}")

    def notify_trade_opened(self, symbol: str, sizing: Dict[str, Any], ai_reason: str, mode: str):
        action = sizing["action"]
        icon = "🟢 LONG" if action == "LONG" else "🔴 SHORT"
        msg = f"""
🤖 <b>AI TRADING BOT - NEW ORDER EXECUTED</b>
Mode: <code>{mode.upper()}</code>

<b>Symbol:</b> {symbol}
<b>Action:</b> {icon}
<b>Entry Price:</b> ${sizing['entry_price']}
<b>Stop Loss:</b> ${sizing['stop_loss']}
<b>Take Profit:</b> ${sizing['take_profit_1']}
<b>Leverage:</b> {sizing['leverage']}x (Margin: ${sizing['required_margin_usdt']} USDT)
<b>Risk:</b> ${sizing['risk_amount_usdt']} ({sizing['risk_pct']}%)

🧠 <b>AI Rationale:</b>
<i>{ai_reason}</i>
"""
        self.send_message(msg.strip())

    def notify_trade_closed(self, event: Dict[str, Any], mode: str):
        pnl = event["net_pnl"]
        icon = "🎉 PROFIT" if pnl >= 0 else "🛑 LOSS"
        msg = f"""
{icon} <b>POSITION CLOSED</b>
Mode: <code>{mode.upper()}</code>

<b>Symbol:</b> {event['symbol']} ({event['side']})
<b>Reason:</b> {event['reason']}
<b>Entry:</b> ${event['entry_price']} ➡️ <b>Exit:</b> ${event['close_price']}
<b>Net PnL:</b> <b>{'+' if pnl >= 0 else ''}${pnl} USDT</b> ({event['roi_pct']}%)
"""
        self.send_message(msg.strip())

    def notify_circuit_breaker(self, daily_loss: float):
        msg = f"""
⚠️ <b>CIRCUIT BREAKER TRIGGERED</b>
Daily loss reached: <b>-${abs(daily_loss):.2f} USDT</b>
Trading has been automatically <b>PAUSED</b> for 24 hours to protect capital.
"""
        self.send_message(msg.strip())
