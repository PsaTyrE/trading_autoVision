from typing import Tuple, Dict, Any
from datetime import datetime, date, timezone
import math
from src.config import config
from src.ai_analyzer import TradeSignal

class RiskManager:
    def __init__(self):
        self.daily_realized_pnl: float = 0.0
        self.last_reset_date: date = datetime.now(timezone.utc).date()
        self.circuit_breaker_triggered: bool = False

    def check_reset_daily(self):
        """Resets daily PnL counter when a new UTC day starts."""
        today = datetime.now(timezone.utc).date()
        if today != self.last_reset_date:
            self.daily_realized_pnl = 0.0
            self.circuit_breaker_triggered = False
            self.last_reset_date = today

    def record_closed_trade_pnl(self, pnl: float):
        """Records realized profit or loss."""
        self.check_reset_daily()
        self.daily_realized_pnl += pnl

    def evaluate_signal(
        self,
        signal: TradeSignal,
        portfolio_balance: float,
        open_positions_count: int
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Validates trade signal against strict risk criteria.
        Returns: (is_approved: bool, reason: str, sizing_details: dict)
        """
        self.check_reset_daily()

        # 1. Action Check
        if signal.action not in ["LONG", "SHORT"]:
            return False, f"Action is {signal.action} (No execution needed)", {}

        # 2. Daily Drawdown Circuit Breaker
        max_allowed_daily_loss = portfolio_balance * config.max_daily_loss_pct
        if self.daily_realized_pnl <= -max_allowed_daily_loss or self.circuit_breaker_triggered:
            self.circuit_breaker_triggered = True
            return False, f"[CIRCUIT BREAKER] Daily loss limit hit (${self.daily_realized_pnl:.2f}). Trading paused for today.", {}

        # 3. Maximum Open Positions Limit
        if open_positions_count >= config.max_open_positions:
            return False, f"Maximum open positions limit reached ({open_positions_count}/{config.max_open_positions})", {}

        # 4. AI Confidence Check
        if signal.confidence < config.min_ai_confidence:
            return False, f"AI confidence ({signal.confidence:.2f}) below threshold ({config.min_ai_confidence:.2f})", {}

        # 5. Guard None and Non-finite values
        entry = signal.suggested_entry
        sl = signal.stop_loss
        tp = signal.take_profit_1

        if None in (entry, sl, tp):
            return False, f"Missing critical price levels: entry={entry}, sl={sl}, tp={tp}", {}

        if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (entry, sl, tp)):
            return False, "Price levels must be valid finite numbers", {}

        # 6. Directional SL / TP Logic Validation & Real Mathematical RR Check
        if signal.action == "LONG":
            if sl >= entry:
                return False, f"Invalid LONG setup: Stop-Loss ({sl}) must be below Entry ({entry})", {}
            if tp <= entry:
                return False, f"Invalid LONG setup: Take-Profit ({tp}) must be above Entry ({entry})", {}
            loss_per_unit = entry - sl
            gain_per_unit = tp - entry
        else: # SHORT
            if sl <= entry:
                return False, f"Invalid SHORT setup: Stop-Loss ({sl}) must be above Entry ({entry})", {}
            if tp >= entry:
                return False, f"Invalid SHORT setup: Take-Profit ({tp}) must be below Entry ({entry})", {}
            loss_per_unit = sl - entry
            gain_per_unit = entry - tp

        if loss_per_unit <= 0:
            return False, "Calculated loss per unit is non-positive", {}

        # Real mathematical RR calculation (never trust AI claimed RR alone)
        real_rr = gain_per_unit / loss_per_unit
        if real_rr < config.min_risk_reward_ratio:
            return False, f"Real mathematical R:R ({real_rr:.2f}) below minimum ({config.min_risk_reward_ratio:.2f})", {}

        # 7. Position Sizing (Fixed Fractional Risk)
        # Risk capital = e.g., 2% of total portfolio
        risk_capital = portfolio_balance * config.max_risk_per_trade
        # Quantity of contracts/coins
        target_quantity = risk_capital / loss_per_unit
        position_notional = target_quantity * entry
        required_margin = position_notional / max(1, config.leverage)

        # Safety Cap: Margin required for single trade cannot exceed max configured percentage of total wallet
        max_margin_per_trade = portfolio_balance * config.max_margin_per_trade_pct
        if required_margin > max_margin_per_trade:
            # Scale down quantity to max allowed margin
            required_margin = max_margin_per_trade
            position_notional = required_margin * max(1, config.leverage)
            target_quantity = position_notional / max(0.0001, entry)

        sym_name = getattr(signal, "symbol", "") or (config.trading_symbols[0] if config.trading_symbols else "AUTO")
        quote_asset = sym_name.split("/")[1].split(":")[0] if "/" in sym_name else config.default_quote_asset

        sizing_details = {
            "symbol": sym_name,
            "currency": quote_asset,
            "action": signal.action,
            "entry_price": entry,
            "stop_loss": sl,
            "take_profit_1": tp,
            "take_profit_2": signal.take_profit_2,
            "quantity": round(target_quantity, 4),
            "position_notional": round(position_notional, 2),
            "position_notional_usdt": round(position_notional, 2),
            "required_margin": round(required_margin, 2),
            "required_margin_usdt": round(required_margin, 2),
            "leverage": config.leverage,
            "risk_amount": round(target_quantity * loss_per_unit, 2),
            "risk_amount_usdt": round(target_quantity * loss_per_unit, 2),
            "risk_pct": round((target_quantity * loss_per_unit / max(0.01, portfolio_balance)) * 100, 2),
            "real_risk_reward_ratio": round(real_rr, 2)
        }

        return True, "Risk criteria satisfied", sizing_details
