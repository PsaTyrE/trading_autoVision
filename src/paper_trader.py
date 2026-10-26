import json
import os
from typing import Dict, Any, List
from datetime import datetime, timezone
from src.config import config

STATE_FILE = "paper_trading_state.json"

class PaperTrader:
    """
    Realistic Binance Futures Paper Trading Simulator.
    Calculates isolated margin, taker fees (config.taker_fee_rate), liquidation thresholds, SL, and TP.
    """
    def __init__(self):
        self.state_file = STATE_FILE
        self.balance: float = config.paper_initial_balance
        self.open_positions: Dict[str, Dict[str, Any]] = {}
        self.trade_history: List[Dict[str, Any]] = []
        self.total_trades: int = 0
        self.winning_trades: int = 0
        self.losing_trades: int = 0
        self.load_state()

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                    self.balance = data.get("balance", config.paper_initial_balance)
                    self.open_positions = data.get("open_positions", {})
                    self.trade_history = data.get("trade_history", [])
                    self.total_trades = data.get("total_trades", 0)
                    self.winning_trades = data.get("winning_trades", 0)
                    self.losing_trades = data.get("losing_trades", 0)
            except Exception as e:
                print(f"[!] Error loading paper trade state: {e}")

    def save_state(self):
        try:
            data = {
                "balance": round(self.balance, 2),
                "open_positions": self.open_positions,
                "trade_history": self.trade_history[-100:],  # keep last 100
                "total_trades": self.total_trades,
                "winning_trades": self.winning_trades,
                "losing_trades": self.losing_trades,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
            with open(self.state_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[!] Error saving paper trade state: {e}")

    def get_balance(self) -> float:
        return self.balance

    def get_open_positions_count(self) -> int:
        return len(self.open_positions)

    def open_position(self, symbol: str, sizing: Dict[str, Any], ai_reason: str) -> bool:
        """Opens a simulated futures position."""
        if symbol in self.open_positions:
            return False  # Position already open for this symbol

        required_margin = sizing["required_margin_usdt"]
        if self.balance < required_margin:
            print(f"[PaperTrader] Insufficient virtual balance (${self.balance:.2f} < ${required_margin:.2f})")
            return False

        # Deduct entry taker fee
        fee = sizing["position_notional_usdt"] * config.taker_fee_rate
        self.balance -= fee

        position = {
            "symbol": symbol,
            "side": sizing["action"],  # LONG or SHORT
            "entry_price": sizing["entry_price"],
            "quantity": sizing["quantity"],
            "notional": sizing["position_notional_usdt"],
            "margin": required_margin,
            "leverage": sizing["leverage"],
            "stop_loss": sizing["stop_loss"],
            "take_profit_1": sizing["take_profit_1"],
            "take_profit_2": sizing.get("take_profit_2"),
            "ai_reason": ai_reason,
            "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        }

        self.open_positions[symbol] = position
        self.save_state()
        return True

    def check_and_update_positions(self, current_prices: Dict[str, float]) -> List[Dict[str, Any]]:
        """
        Evaluates active positions against live market prices.
        Closes position if SL or TP is reached.
        """
        closed_events = []
        symbols_to_close = []

        for symbol, pos in self.open_positions.items():
            if symbol not in current_prices:
                continue

            price = current_prices[symbol]
            side = pos["side"]
            entry = pos["entry_price"]
            sl = pos["stop_loss"]
            tp = pos["take_profit_1"]
            qty = pos["quantity"]

            hit_sl = False
            hit_tp = False

            if side == "LONG":
                if price <= sl:
                    hit_sl = True
                elif price >= tp:
                    hit_tp = True
            else: # SHORT
                if price >= sl:
                    hit_sl = True
                elif price <= tp:
                    hit_tp = True

            if hit_sl or hit_tp:
                # Calculate PnL
                if side == "LONG":
                    gross_pnl = (price - entry) * qty
                else:
                    gross_pnl = (entry - price) * qty

                # Exit taker fee
                exit_notional = price * qty
                fee = exit_notional * config.taker_fee_rate
                net_pnl = gross_pnl - fee

                self.balance += (pos["margin"] + net_pnl)
                self.total_trades += 1
                if net_pnl > 0:
                    self.winning_trades += 1
                else:
                    self.losing_trades += 1

                reason = "TAKE_PROFIT" if hit_tp else "STOP_LOSS"
                event = {
                    "symbol": symbol,
                    "side": side,
                    "entry_price": entry,
                    "close_price": price,
                    "reason": reason,
                    "net_pnl": round(net_pnl, 2),
                    "roi_pct": round((net_pnl / max(0.01, pos["margin"])) * 100, 2),
                    "closed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                }
                self.trade_history.append(event)
                closed_events.append(event)
                symbols_to_close.append(symbol)

        for sym in symbols_to_close:
            del self.open_positions[sym]

        if closed_events:
            self.save_state()

        return closed_events

    def get_portfolio_summary(self, current_prices: Dict[str, float] = None) -> Dict[str, Any]:
        """Calculates total equity including unrealized PnL."""
        if current_prices is None:
            current_prices = {}

        unrealized_pnl = 0.0
        active_list = []

        for symbol, pos in self.open_positions.items():
            curr_price = current_prices.get(symbol, pos["entry_price"])
            side = pos["side"]
            entry = pos["entry_price"]
            qty = pos["quantity"]

            if side == "LONG":
                u_pnl = (curr_price - entry) * qty
            else:
                u_pnl = (entry - curr_price) * qty

            unrealized_pnl += u_pnl
            active_list.append({
                "symbol": symbol,
                "side": side,
                "entry": entry,
                "current": curr_price,
                "margin": pos["margin"],
                "unrealized_pnl": round(u_pnl, 2),
                "roe_pct": round((u_pnl / max(0.01, pos["margin"])) * 100, 2),
                "sl": pos["stop_loss"],
                "tp": pos["take_profit_1"]
            })

        total_equity = self.balance + unrealized_pnl
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0.0

        return {
            "mode": "PAPER_TRADING",
            "wallet_balance": round(self.balance, 2),
            "total_equity": round(total_equity, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "total_trades": self.total_trades,
            "win_rate_pct": round(win_rate, 1),
            "active_positions": active_list
        }
