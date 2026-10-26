from typing import Dict, Any, List, Optional
from src.config import config

GRID_STATE_FILE = "futures_grid_state.json"

class FuturesGridBot:
    """
    Implements Binance Futures Grid Trading Strategy (Long Grid).
    Mimics Binance's AI Strategy by calculating price range, grid lines,
    buy-low/sell-high orders, and tracking matched grid profits.
    """
    def __init__(
        self,
        symbol: str,
        current_price: float,
        atr: float,
        leverage: Optional[int] = None,
        total_investment: Optional[float] = None,
        grid_count: Optional[int] = None,
        min_notional: Optional[float] = None,
        fee_rate: Optional[float] = None,
        price_precision: Optional[int] = None,
        qty_precision: Optional[int] = None,
        profit_multiplier: float = 1.0
    ):
        self.symbol = symbol
        self.current_price = current_price
        # AUDIT FIX: never fabricate volatility silently — use the config fallback pct
        # (env-overridable) and remember that it was synthesised, so callers can log it.
        self.atr_is_synthetic = not (atr and atr > 0)
        self.atr = atr if (atr and atr > 0) else current_price * float(getattr(config, "atr_fallback_pct", 0.02))
        self.leverage = leverage if leverage is not None else config.leverage
        self.total_investment = total_investment if total_investment is not None else config.default_investment
        self.grid_count = grid_count if grid_count is not None else config.default_grid_count
        raw_min = min_notional if min_notional is not None else config.min_notional_threshold
        self.min_notional = max(0.1, float(raw_min or config.min_notional_threshold))
        # AUDIT FIX: grid levels are LIMIT orders -> maker fee, and the live VIP rate is
        # passed in from the caller. Only fall back to config when nothing was supplied.
        self.fee_rate = fee_rate if fee_rate is not None else (float(getattr(config, "maker_fee_rate", 0.0002)) * 2)
        self.bep_markup = 1.0 + self.fee_rate + float(getattr(config, "fee_buffer_pct", 0.001)) # BE price lifts SL over entry to cover all fees
        self.profit_multiplier = max(0.8, float(profit_multiplier))

        # GRID RAMPING: cap grid_count down so notional per grid does not fall below
        # the venue min — never clamped UP. Floor is config.grid_ramp_min_grids.
        total_notional_cap = self.total_investment * self.leverage
        _ramp_floor = max(2, int(getattr(config, "grid_ramp_min_grids", 2) or 2))
        max_viable_grids = max(_ramp_floor, int(total_notional_cap / max(0.1, self.min_notional)))
        if self.grid_count > max_viable_grids:
            self.grid_count = max(_ramp_floor, max_viable_grids)

        # Dynamic precision based on price scale or explicit market precision
        # AUDIT FIX: precision from the market's real tick when supplied; the magnitude
        # ladder is only a last-resort fallback (env-overridable via price_precision).
        if price_precision is not None:
            self.precision = int(price_precision)
        else:
            self.precision = 7 if current_price < 0.001 else (6 if current_price < 0.01 else (5 if current_price < 0.1 else (4 if current_price < 1.0 else 2)))
        # AUDIT FIX: quantity decimals come from the market's amount step when supplied,
        # otherwise default to 4 (previous hardcoded behaviour).
        self.qty_precision = int(qty_precision) if qty_precision is not None else 4

        # Dynamic AI Range calculation matching Binance AI Grid (with Quant-Memory Profit Multiplier)
        # AUDIT FIX: ATR multiples / pct floors are now config knobs, not literals.
        lower_dist = max(float(getattr(config, "range_lower_atr_mult", 1.8)) * self.atr,
                         current_price * float(getattr(config, "range_lower_pct_floor", 0.022)))
        raw_lower = round(current_price - lower_dist, self.precision)
        self.lower_price = max(raw_lower, round(current_price * 0.1, self.precision)) if current_price > 0 else max(raw_lower, 0.0001)
        
        upper_dist = max(float(getattr(config, "range_upper_atr_mult", 2.5)) * self.atr * self.profit_multiplier,
                         current_price * float(getattr(config, "range_upper_pct_floor", 0.035)) * self.profit_multiplier)
        self.upper_price = round(current_price + upper_dist, self.precision)
        
        # Stop Loss placed safely below the entire grid range (never suffocating active buy grids)
        sl_cushion = max(float(getattr(config, "sl_cushion_atr_mult", 0.8)) * self.atr,
                         current_price * float(getattr(config, "sl_cushion_pct", 0.010)))
        self.stop_loss = round(self.lower_price - sl_cushion, self.precision)

        # FIX 3: R:R REPAIR — the structural stop above can sit wider than TP1's reward
        # (risk > reward was the verified bleed: risk ~2.2% vs first reward ~0.9%).
        # Clamp so risk never exceeds reward / min_RR, i.e. the stop is never wider
        # than the TP1 distance divided by the required risk-reward ratio.
        tp1_dist = current_price * config.tp1_target_pct
        max_sl_dist = tp1_dist / max(config.min_risk_reward_ratio, 0.1)
        tightened_sl = round(current_price - max_sl_dist, self.precision)
        # AUDIT FIX: the clamp must never lift the stop INTO the grid range (above the
        # lowest buy level), else a normal dip fills buys then stops out instantly. Only
        # tighten when it still leaves clearance below the grid, otherwise keep the
        # structural stop (and, if needed, widen TP1 instead of suffocating the grid).
        clearance = float(getattr(config, "stop_grid_clearance_atr", 0.5)) * self.atr
        grid_floor = round(self.lower_price - clearance, self.precision)
        if tightened_sl > self.stop_loss and tightened_sl <= grid_floor:
            self.stop_loss = tightened_sl
        # Keep it strictly below current_price (a stop at/above entry is not a stop).
        if current_price > 0 and self.stop_loss >= current_price:
            self.stop_loss = round(current_price * 0.999, self.precision)

        # FIX 4: FEE/OVERTRADING FLOOR — a matched grid must net >= min_grid_step_pct of
        # price (~0.8%); anything tighter is pure fee churn. Widen the step to the floor
        # (and extend the range so the ladder stays coherent) -> fewer, bigger grids.
        min_step_floor = current_price * float(getattr(config, "min_grid_step_pct", 0.008))

        # Generate grid levels
        self.grid_step = round((self.upper_price - self.lower_price) / max(1, self.grid_count), self.precision)
        if self.grid_step <= 0:
            self.grid_step = round(current_price * 0.008, self.precision) if current_price > 0 else 0.0001
        if min_step_floor > 0 and self.grid_step < min_step_floor:
            self.grid_step = round(min_step_floor, self.precision)
            # Keep upper_price == top of the ladder so the reported range matches the
            # levels actually traded (only ever widens the range, never shrinks it).
            self.upper_price = round(self.lower_price + (self.grid_step * self.grid_count), self.precision)

        self.grid_levels = [round(self.lower_price + (i * self.grid_step), self.precision) for i in range(self.grid_count + 1)]

        # Allocation per grid
        self.notional_per_grid = (self.total_investment * self.leverage) / max(1, self.grid_count)
        self.margin_per_grid = self.total_investment / max(1, self.grid_count)

        # Matched trades & profit tracking
        self.matched_trades: int = 0
        self.total_grid_profit: float = 0.0
        self.active_grids: List[Dict[str, Any]] = []
        self.entry_price: float = current_price
        self.highest_price: float = current_price
        self.breakeven_activated: bool = False

        self._initialize_grid_orders()

    def _initialize_grid_orders(self):
        """Builds the ladder of buy-low and sell-high orders."""
        self.active_grids = []
        for level in self.grid_levels:
            if level <= 0:
                continue
            if level < self.current_price:
                # Buy order below market price
                qty = round(self.notional_per_grid / level, self.qty_precision)
                self.active_grids.append({
                    "price": level,
                    "side": "BUY",
                    "status": "OPEN",
                    "quantity": qty,
                    "target_sell_price": round(level + self.grid_step, self.precision)
                })
            elif level > self.current_price:
                # Sell order above market price
                qty = round(self.notional_per_grid / level, self.qty_precision)
                self.active_grids.append({
                    "price": level,
                    "side": "SELL",
                    "status": "OPEN",
                    "quantity": qty,
                    "target_buy_price": round(level - self.grid_step, self.precision)
                })

    def update_price(self, new_price: float) -> List[Dict[str, Any]]:
        """
        Simulates grid order matching when market price moves across grid lines.
        Returns list of newly matched trade events.
        """
        matched_events = []
        self.current_price = new_price

        # Check if stop-loss triggered
        if new_price <= self.stop_loss:
            return [{"event": "STOP_LOSS_TRIGGERED", "price": new_price, "symbol": self.symbol}]

        # SMART BREAK-EVEN PLUS (Activated only after real profit expansion >= config):
        if not self.breakeven_activated and self.entry_price > 0 and new_price >= (self.entry_price * (1.0 + config.breakeven_trigger_pct)):
            be_price = round(self.entry_price * self.bep_markup, self.precision)
            if be_price > self.stop_loss:
                self.stop_loss = be_price
                self.breakeven_activated = True
                matched_events.append({
                    "event": "BREAKEVEN_STOP_ACTIVATED",
                    "symbol": self.symbol,
                    "new_stop_loss": be_price,
                    "message": f"Stop-Loss {self.symbol} dinaikkan ke Modal + Profit Bersih (BEP+: ${be_price}) - Posisi 100% Bebas Resiko!"
                })

        # SMART TRAILING PROFIT HARVESTER:
        if new_price > self.highest_price:
            self.highest_price = new_price
            if self.entry_price > 0 and self.highest_price >= (self.entry_price * (1.0 + config.trailing_start_pct)):
                trail_dist = max(0.8 * self.atr, self.highest_price * config.trailing_dist_pct)
                trail_sl = round(self.highest_price - trail_dist, self.precision)
                if trail_sl > self.stop_loss:
                    self.stop_loss = trail_sl
                    matched_events.append({
                        "event": "TRAILING_STOP_RAISED",
                        "symbol": self.symbol,
                        "new_stop_loss": trail_sl,
                        "highest_price": self.highest_price,
                        "message": f"Trailing Stop {self.symbol} dinaikkan ke ${trail_sl} (Puncak: ${self.highest_price}) - Mengunci profit maksimal!"
                    })

        for grid in self.active_grids:
            if grid["status"] != "OPEN":
                continue

            if grid["side"] == "BUY" and new_price <= grid["price"]:
                # Buy filled -> place take-profit sell order
                grid["status"] = "FILLED"
                sell_price = grid["target_sell_price"]
                profit = (sell_price - grid["price"]) * grid["quantity"] - (sell_price * grid["quantity"] * self.fee_rate) # dynamic taker fees

                matched_event = {
                    "event": "BUY_GRID_FILLED",
                    "symbol": self.symbol,
                    "price": grid["price"],
                    "qty": grid["quantity"],
                    "next_target": sell_price,
                    "potential_profit": round(profit, 3)
                }
                matched_events.append(matched_event)

                # Reset to sell order at higher grid
                grid["side"] = "SELL"
                grid["price"] = sell_price
                grid["status"] = "OPEN"
                grid["target_buy_price"] = round(sell_price - self.grid_step, self.precision)

            elif grid["side"] == "SELL" and new_price >= grid["price"]:
                # Sell filled -> profit locked in!
                grid["status"] = "FILLED"
                profit = (grid["price"] - (grid["price"] - self.grid_step)) * grid["quantity"] - (grid["price"] * grid["quantity"] * self.fee_rate)
                self.total_grid_profit += max(profit, 0.0)
                self.matched_trades += 1

                # AUTO BREAK-EVEN STOP-LOSS (Risk-Free Trade Locking)
                if not self.breakeven_activated and self.entry_price > 0:
                    be_price = round(self.entry_price * self.bep_markup, self.precision)
                    if be_price > self.stop_loss:
                        self.stop_loss = be_price
                        self.breakeven_activated = True
                        matched_events.append({
                            "event": "BREAKEVEN_STOP_ACTIVATED",
                            "symbol": self.symbol,
                            "new_stop_loss": be_price,
                            "message": f"Stop-Loss {self.symbol} dinaikkan ke Modal + Profit Bersih (BEP+: ${be_price}) - Posisi 100% Bebas Resiko!"
                        })

                matched_event = {
                    "event": "SELL_GRID_PROFIT_LOCKED",
                    "symbol": self.symbol,
                    "price": grid["price"],
                    "qty": grid["quantity"],
                    "realized_profit": round(profit, 3),
                    "total_grid_profit": round(self.total_grid_profit, 2)
                }
                matched_events.append(matched_event)

                # Reset to buy order at lower grid
                buy_price = round(grid["price"] - self.grid_step, self.precision)
                grid["side"] = "BUY"
                grid["price"] = buy_price
                grid["status"] = "OPEN"
                # AUDIT FIX (was a profit-killer): target sell must be one grid step ABOVE
                # the buy price. The old code set target_sell_price = buy price, so every
                # recycled level sold at cost -> a guaranteed loss equal to round-trip fees.
                grid["target_sell_price"] = round(buy_price + self.grid_step, self.precision)

        return matched_events

    def get_summary(self) -> Dict[str, Any]:
        """Returns comprehensive status of this Futures Grid."""
        grid_spread_pct = round((self.grid_step / max(self.current_price, 0.0001)) * 100, 2) if self.current_price > 0 else 0.0
        quote_curr = self.symbol.split('/')[1].split(':')[0] if '/' in self.symbol else "USDT"
        return {
            "symbol": self.symbol,
            "strategy": "BINANCE_FUTURES_LONG_GRID",
            "current_price": self.current_price,
            "range": f"${self.lower_price} - ${self.upper_price}",
            "grid_count": self.grid_count,
            "grid_step": self.grid_step,
            "grid_step_usdt": self.grid_step,
            "profit_per_grid_pct": f"{grid_spread_pct}%",
            "total_investment": self.total_investment,
            "leverage": f"{self.leverage}x",
            "stop_loss": self.stop_loss,
            "matched_trades": self.matched_trades,
            "total_grid_profit": round(self.total_grid_profit, 3),
            "total_grid_profit_usdt": round(self.total_grid_profit, 3),
            "quote_asset": quote_curr
        }
