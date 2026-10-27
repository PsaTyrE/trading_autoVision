import os
import math
import logging
import threading
import time
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from src.config import config
from src.market_data import MarketDataFeeder
from src.market_scanner import MarketScanner, is_valid_symbol
from src.futures_grid_engine import FuturesGridBot
from src.live_trader import BinanceFuturesTrader
from src.ai_analyzer import AIAnalyzer
from src.hybrid_engine import HybridConsensusEngine
from src.forex_factory_news import ForexFactoryNewsSentinel
from src.self_learning_memory import SelfLearningMemory

logger = logging.getLogger("web_manager")

def normalize_symbol_dynamic(symbol: str, default_quote: str = "USDT") -> str:
    """Normalizes any symbol input dynamically (e.g. 'BTC' -> 'BTC/USDT', 'BTCUSDC' -> 'BTC/USDC', 'SOL/USDC' -> 'SOL/USDC')."""
    if not symbol:
        return ""
    s = symbol.strip().upper().replace(" ", "").split(":")[0]
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 2 and parts[1]:
            quote = "USDT" if parts[1] == "USD" else parts[1]
            return f"{parts[0]}/{quote}"
        return s
    for q in ["USDC", "USDT", "FDUSD", "BUSD", "USD"]:
        if s.endswith(q) and len(s) > len(q):
            base = s[:-len(q)]
            quote = "USDT" if q == "USD" else q
            return f"{base}/{quote}"
    return f"{s}/{default_quote}"

def _decimals_from_step(step: float) -> int:
    """Number of decimal places implied by a tickSize/stepSize (e.g. 0.001 -> 3)."""
    try:
        step = float(step or 0.0)
    except Exception as e:
        logger.debug(f"_decimals_from_step parse failed: {e}")
        return 0
    if step <= 0:
        return 0
    s = f"{step:.16f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0

def market_price_precision(market: Dict[str, Any]) -> Optional[int]:
    """Price decimals from Binance exchangeInfo (tickSize), synced — never hardcoded."""
    try:
        p = (market.get("precision") or {}).get("price")
        if isinstance(p, int) and p >= 0:
            return p
        if isinstance(p, float) and p > 0:
            return _decimals_from_step(p)
    except Exception as e:
        logger.debug(f"market_price_precision precision parse failed: {e}")
    try:
        for f in ((market.get("info") or {}).get("filters") or []):
            if f.get("filterType") == "PRICE_FILTER":
                return _decimals_from_step(float(f.get("tickSize", 0) or 0))
    except Exception as e:
        logger.debug(f"market_price_precision filter parse failed: {e}")
    return None

def market_qty_precision(market: Dict[str, Any]) -> Optional[int]:
    """Quantity decimals from Binance exchangeInfo (stepSize), synced — never hardcoded."""
    try:
        p = (market.get("precision") or {}).get("amount")
        if isinstance(p, int) and p >= 0:
            return p
        if isinstance(p, float) and p > 0:
            return _decimals_from_step(p)
    except Exception as e:
        logger.debug(f"market_qty_precision precision parse failed: {e}")
    try:
        for f in ((market.get("info") or {}).get("filters") or []):
            if f.get("filterType") in ("LOT_SIZE", "MARKET_LOT_SIZE"):
                d = _decimals_from_step(float(f.get("stepSize", 0) or 0))
                if d:
                    return d
    except Exception as e:
        logger.debug(f"market_qty_precision filter parse failed: {e}")
    return None

class BasketCoinState:
    """Holds independent state, grid engine, orders, and PnL for a single coin in the portfolio basket."""
    def __init__(self, symbol: str, investment: float, grid_count: int, leverage: int, custom_stop_loss: Optional[float] = None):
        self.symbol = symbol
        self.investment = investment
        self.grid_count = grid_count
        self.leverage = leverage
        self.custom_stop_loss = custom_stop_loss

        self.current_price: float = 0.0
        self.entry_price: float = 0.0
        self.unrealized_pnl: float = 0.0
        self.position_contracts: float = 0.0
        self.realized_profit: float = 0.0
        self.matched_trades: int = 0
        self.is_breakeven_active: bool = False
        self.initial_snapshot: Dict[str, Any] = {}
        # FORWARD-TEST ANCHORS (P7): frozen entry/stop pair used ONLY to convert realized
        # exits into R-multiples for the live-vs-backtest comparison. Set once at entry;
        # grid averaging must NOT move them (otherwise live R is not comparable to backtest).
        self.r_anchor_entry: float = 0.0
        self.r_anchor_stop: float = 0.0

        self.grid_bot: Optional[FuturesGridBot] = None
        self.grid_summary: Dict[str, Any] = {}
        self.active_grids: List[Dict[str, Any]] = []
        self.deployed_orders: Dict[str, Any] = {}
        self.is_active: bool = True
        self.consensus_status: str = "INITIALIZING"
        self.quant_score: float = 0.0
        self.ai_confidence: float = 0.0
        self.consensus_explanation: str = ""

        # Smart Multi-Tier Take-Profit & Trailing Runner
        self.tp1_price: float = 0.0
        self.tp1_filled: bool = False
        self.highest_price: float = 0.0
        self.trailing_tp_active: bool = False
        self.trailing_stop_price: float = 0.0
        self._hard_stop_synced: float = 0.0
        self._net_income_baseline: Optional[float] = None

        # MULTI-TIMEFRAME CONFLUENCE (read-only, additive): per-coin timeframe view for the
        # dashboard panel. 1H macro bias + primary-timeframe trend/RSI. NEVER consumed by the
        # order/entry/exit path. None until a market snapshot has been captured for this coin.
        self.mtf_1h: Optional[Dict[str, Any]] = None
        self.mtf_primary: Optional[Dict[str, Any]] = None

        # RISK PER TRANSAKSI (read-only, additive): dashboard-only snapshot of the account-level
        # risk the last entry was sized for. Captured from config + the grid engine that was
        # already built; NEVER consumed by the order/entry/exit path. None until an entry ran.
        self.risk_pct: Optional[float] = None
        self.risk_usd: Optional[float] = None
        self.risk_min_notional: Optional[float] = None
        self.risk_fee_pct: Optional[float] = None
        self.risk_sizing_source: Optional[str] = None   # "QUANT" | "MIN_NOTIONAL_GUARD" | "NO_ENTRY"

        # P1 PYRAMID LADDER: plan computed at sizing time; the fill-watcher (_pyramid_watch)
        # only ever ADDS when config.pyramiding_auto is true (kill-switch, default OFF).
        self.pyramid_plan: list = []
        # Index into pyramid_plan of the NEXT staged add still to be placed (1 = stage2 @0.5xATR,
        # 2 = stage3 @1.0xATR). 0/len -> no more adds. Additive: untouched when auto is OFF.
        self.pyramid_next_idx: int = 1
        # Abort ladder once price closes past the LAST trigger without the penalty path, so a
        # ran-away move never chases. Set -> the watcher stops evaluating this coin.
        self.pyramid_done: bool = False
        # TP ROTATION: set once this coin's TP1 fired and the runner was locked at
        # adaptive BE+. Prevents double-rotation on every tick after TP1.
        self.tp_rotated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "investment": round(self.investment, 2),
            "grid_count": self.grid_count,
            "leverage": self.leverage,
            "current_price": round(self.current_price, 5),
            "entry_price": round(self.entry_price, 5),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "position_contracts": self.position_contracts,
            "realized_profit": round(self.realized_profit, 4),
            "matched_trades": self.matched_trades,
            "is_breakeven_active": self.is_breakeven_active,
            "tp1_price": round(self.tp1_price, 5),
            "tp1_filled": self.tp1_filled,
            "highest_price": round(self.highest_price, 5),
            "trailing_stop_price": round(self.trailing_stop_price, 5),
            "grid_summary": self.grid_summary,
            "stop_loss": (float(getattr(self.grid_bot, "stop_loss", 0.0) or 0.0) if self.grid_bot else (self.grid_summary.get("stop_loss", 0.0) if self.grid_summary else 0.0)),
            "active_grids": self.active_grids,
            "deployed_orders_count": len(self.deployed_orders),
            "consensus_status": self.consensus_status,
            "quant_score": self.quant_score,
            "ai_confidence": self.ai_confidence,
            "consensus_explanation": self.consensus_explanation,
            "is_active": self.is_active,
            "mtf_1h": self.mtf_1h,
            "mtf_primary": self.mtf_primary,
            "risk_pct": self.risk_pct,
            "risk_usd": self.risk_usd,
            "risk_min_notional": self.risk_min_notional,
            "risk_fee_pct": self.risk_fee_pct,
            "risk_sizing_source": self.risk_sizing_source,
            "pyramid_plan": self.pyramid_plan
        }

class BotWebManager:
    """
    Singleton manager running the Futures Grid Bot in background threads,
    providing thread-safe states and event streaming for the Web Dashboard.
    Supports Multi-Coin Portfolio Basket (Top 3 Bullish Coins simultaneously).
    """
    def __init__(self):
        self.lock = threading.RLock()
        self.is_running: bool = False
        self.stop_requested: bool = False
        self.bot_thread: Optional[threading.Thread] = None
        self.active_thread_id: Optional[int] = None
        self.session_id: int = 0

        # Bot configuration
        self.symbol: str = ""
        self.mode: str = "live" if config.trading_mode == "live" else "demo"
        self.investment: float = config.default_investment
        self.grid_count: int = config.default_grid_count
        # P5: polling cadence driven by config (was hardcoded 5s). Faster = tighter SL
        # reaction but more REST load (rate limits on a Pi); slower = safer, cheaper.
        self.interval: int = int(getattr(config, "loop_interval_seconds", 5) or 5)
        self.leverage: int = config.leverage
        self.custom_stop_loss: Optional[float] = None
        self.quote_asset: str = config.default_quote_asset
        self.timeframe: str = config.timeframe

        # Basket Portfolio Mode
        self.basket_size: int = 3
        self.basket_coins: Dict[str, BasketCoinState] = {}

        # Aggregated Live stats
        self.current_price: float = 0.0
        self.realized_profit: float = 0.0
        self.matched_trades: int = 0
        self.unrealized_pnl: float = 0.0
        self.position_contracts: float = 0.0
        self.wallet_balance: float = 0.0
        self.available_balance: float = 0.0

        # Grid structures (primary / selected view)
        self.grid_summary: Dict[str, Any] = {}
        self.active_grids: List[Dict[str, Any]] = []
        self.deployed_orders: Dict[str, Any] = {}
        self.logs: List[Dict[str, Any]] = []

        # Connected WebSocket clients
        self.ws_clients = set()
        self.event_loop = None

        # Market & Execution helpers
        self.feeder = MarketDataFeeder()
        self.scanner = MarketScanner()
        self.ai = AIAnalyzer()
        self.memory = SelfLearningMemory()
        self.hybrid = HybridConsensusEngine(self.ai, memory=self.memory)
        self.news_sentinel = ForexFactoryNewsSentinel()
        self.cached_news_guard: Dict[str, Any] = {"is_danger": False, "status": "NORMAL", "pause_entries": False, "caution_reason": "Aman"}
        self.last_news_guard_check: float = 0.0
        self.trader: Optional[BinanceFuturesTrader] = None

        # Automated Daily Discipline & Circuit Breakers (Pilar 1: Disiplin Batas Transaksi Harian)
        self.daily_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.daily_trades_count: int = 0
        self.entry_count_utc: int = 0
        self.daily_realized_pnl: float = 0.0
        self.max_daily_trades: int = config.max_daily_trades
        self.max_daily_entries: int = config.max_daily_entries
        self.max_daily_loss_pct: float = config.max_daily_loss_pct # 0.05 (5%)
        self.daily_circuit_breaker_triggered: bool = False
        # TP ROTATION bookkeeping: per-symbol cooldown timestamps + daily counter.
        self._tp_rotation_last_ts: Dict[str, float] = {}
        self._tp_rotation_last_ts_global: float = 0.0
        self._tp_rotation_fail_streak: Dict[str, int] = {}
        self._tp_rotation_last_skip_ts: Dict[str, float] = {}
        self._tp_rotation_count_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._tp_rotation_count: int = 0

        # AUDIT FIX (measurability): running performance metrics so live edge is measurable
        # against the backtest — win-rate, avg win/loss, payoff ratio, profit factor, avg R.
        self.perf_closed: int = 0
        self.perf_wins: int = 0
        self.perf_losses: int = 0
        self.perf_gross_win: float = 0.0
        self.perf_gross_loss: float = 0.0
        self.perf_r_sum: float = 0.0
        self.perf_r_count: int = 0

    def _record_closed_trade_metrics(self, pnl: float, r_multiple: Optional[float] = None):
        """Aggregate per-trade performance (win-rate, payoff, profit factor, avg R)."""
        try:
            with self.lock:
                self.perf_closed += 1
                if pnl >= 0:
                    self.perf_wins += 1
                    self.perf_gross_win += float(pnl)
                else:
                    self.perf_losses += 1
                    self.perf_gross_loss += abs(float(pnl))
                if r_multiple is not None:
                    self.perf_r_sum += float(r_multiple)
                    self.perf_r_count += 1
        except Exception as e:
            logger.debug(f"_record_closed_trade_metrics failed: {e}")

    def get_performance_metrics(self) -> Dict[str, Any]:
        """Live expectancy report — the honest scoreboard of the strategy."""
        with self.lock:
            closed = self.perf_closed
            wins = self.perf_wins
            losses = self.perf_losses
            gw = self.perf_gross_win
            gl = self.perf_gross_loss
            r_sum = self.perf_r_sum
            r_n = self.perf_r_count
        win_rate = round((wins / closed) * 100, 1) if closed > 0 else 0.0
        avg_win = round(gw / wins, 4) if wins > 0 else 0.0
        avg_loss = round(gl / losses, 4) if losses > 0 else 0.0
        payoff = round(avg_win / avg_loss, 3) if avg_loss > 0 else None
        # An all-wins / zero-loss scoreboard has an UNDEFINED profit factor. Emit None
        # (JSON-safe), never inf — inf crashes Starlette's strict encoder -> 500 on /api/status.
        profit_factor = round(gw / gl, 3) if gl > 0 else None
        expectancy = round((gw - gl) / closed, 4) if closed > 0 else 0.0
        avg_r = round(r_sum / r_n, 3) if r_n > 0 else None
        return {
            "closed_trades": closed,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "payoff_ratio": payoff,
            "profit_factor": profit_factor,
            "expectancy_per_trade": expectancy,
            "avg_r_multiple": avg_r,
            "r_samples": r_n,
        }

    def _check_and_reset_daily_stats(self):
        """Pilar 1: Disiplin otomatis mereset batas transaksi dan batas kerugian setiap pergantian hari UTC (00:00 UTC)."""
        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if current_date != self.daily_date:
            with self.lock:
                self.daily_date = current_date
                self.daily_trades_count = 0
                self.entry_count_utc = 0
                self.daily_realized_pnl = 0.0
                self.daily_circuit_breaker_triggered = False
            self.add_log(f"🌅 [DISIPLIN HARIAN] Hari baru ({current_date} UTC). Batas transaksi dan batas risiko harian direset otomatis.", "info")
        if current_date != getattr(self, "_tp_rotation_count_date", current_date):
            with self.lock:
                self._tp_rotation_count_date = current_date
                self._tp_rotation_count = 0

    def check_daily_discipline_guardrail(self) -> Dict[str, Any]:
        """
        Pilar 1: Disiplin otomatis mengatur batas transaksi harian & batas kerugian harian.
        Menghilangkan overtrading dan revenge trading saat market bergejolak.
        """
        self._check_and_reset_daily_stats()
        if self.daily_circuit_breaker_triggered:
            return {"allowed": False, "reason": "Batas kerugian harian telah tercapai (Circuit Breaker aktif)."}

        # 1. Batas Kerugian Harian (Max Daily Drawdown 5%)
        base_inv = self.investment if self.investment > 0 else 50.0
        max_allowed_loss = max(base_inv * self.max_daily_loss_pct, 2.0)
        if self.daily_realized_pnl <= -max_allowed_loss:
            with self.lock:
                self.daily_circuit_breaker_triggered = True
            return {
                "allowed": False,
                "reason": f"Batas kerugian harian (-${abs(self.daily_realized_pnl):.2f}) menyentuh toleransi maksimal 5% (-${max_allowed_loss:.2f})."
            }

        # 2. Batas Frekuensi Transaksi Harian (Max Daily Trades)
        if self.daily_trades_count >= self.max_daily_trades:
            return {
                "allowed": False,
                "reason": f"Batas transaksi harian ({self.daily_trades_count}/{self.max_daily_trades}) tercapai untuk mencegah overtrading."
            }

        return {"allowed": True, "reason": "Disiplin harian aman."}

    def record_daily_trade(self, pnl: float, r_multiple: Optional[float] = None):
        """Mencatat setiap transaksi dan mengevaluasi disiplin harian seketika."""
        self._check_and_reset_daily_stats()
        # AUDIT FIX: feed the live expectancy scoreboard on every realized outcome.
        self._record_closed_trade_metrics(pnl, r_multiple)
        with self.lock:
            self.daily_trades_count += 1
            self.daily_realized_pnl += pnl
            base_inv = self.investment if self.investment > 0 else 50.0
            max_allowed_loss = max(base_inv * self.max_daily_loss_pct, 2.0)
            if self.daily_realized_pnl <= -max_allowed_loss:
                self.daily_circuit_breaker_triggered = True

        status_icon = "📈" if pnl >= 0 else "📉"
        self.add_log(f"{status_icon} [DISIPLIN STATS] Transaksi #{self.daily_trades_count} hari ini: {pnl:+.4f} | Total PnL Harian: {self.daily_realized_pnl:+.4f}", "info")
        if self.daily_circuit_breaker_triggered:
            self.add_log(f"🛑 [CIRCUIT BREAKER HARIAN] Total loss harian (-${abs(self.daily_realized_pnl):.2f}) menyentuh batas 5%! Mengunci trading harian otomatis demi melindungi modal tersisa.", "cl")

    # ------------------------------------------------------------------
    # P1: Accurate (fee-inclusive) PnL accounting & exchange hard-stop sync
    # ------------------------------------------------------------------
    def _net_realized(self, symbol: str, est_profit: float = 0.0) -> float:
        """
        Returns NET realized profit for a symbol from Binance's own income ledger
        (REALIZED_PNL + COMMISSION + FUNDING_FEE). This replaces the old theoretical
        'est_profit' figure that ignored fees and used target prices instead of fills.

        Falls back to the (fee-adjusted) estimate only if the exchange call fails.
        """
        trader = self.trader
        if trader is None:
            return round(est_profit, 6)
        try:
            inc = trader.get_net_income(symbol)
            net = float(inc.get("net", 0.0))
            if abs(net) > 1e-9:
                return round(net, 6)
        except Exception as e:
            logger.debug(f"get_net_income failed: {e}")
        # Fallback: subtract estimated round-trip taker fees from the gross estimate
        return round(est_profit, 6)

    def _sync_net_income_baseline(self, symbol: str, coin: "BasketCoinState") -> None:
        """Advance the symbol's cumulative-income cursor to now, marking everything realized so
        far as already accounted. Call it when a position opens (nothing accounted yet) and right
        after a TP1 partial cash-in (that slice is accounted via net_gain), so every later
        _realized_since_baseline() reports ONLY the income that arrived after the cursor.

        Without this the FIRST close had baseline=None and fell back to est_profit, which the
        exit paths pass as -abs(investment * realized_cl_fallback_pct). A position already
        flattened by its exchange stop therefore got booked as a FIXED loss: real case — LIT
        closed +$0.4022 on Binance but was recorded -0.10275, mistraining the self-learning
        memory and the AI's risk lessons."""
        if self.trader is None:
            return
        try:
            coin._net_income_baseline = float(self.trader.get_net_income(symbol).get("net", 0.0))
        except Exception as e:
            logger.debug(f"_sync_net_income_baseline failed: {e}")

    def _realized_since_baseline(self, symbol: str, coin: "BasketCoinState", est_profit: float = 0.0) -> float:
        """
        True, fee-inclusive realized PnL for the latest closed trade of a symbol,
        measured as the DELTA of Binance's cumulative net income since the last close.

        This is self-correcting: it captures actual fills, commissions and funding
        without double-counting earlier trades, and it needs no per-order bookkeeping.
        Falls back to a fee-adjusted estimate if the exchange call is unavailable.
        """
        trader = self.trader
        if trader is None:
            return round(est_profit, 6)
        try:
            inc = trader.get_net_income(symbol)
            cumulative = float(inc.get("net", 0.0))
            if coin._net_income_baseline is None:
                # No zero point (seed missed). Never fabricate a PnL from est_profit: for an
                # already-closed position that value is a phantom loss (-investment*pct) that
                # mistrains the self-learning memory. Adopt the cumulative and report 0.0.
                coin._net_income_baseline = cumulative
                return 0.0
            delta = cumulative - coin._net_income_baseline
            coin._net_income_baseline = cumulative
            return round(delta, 6)
        except Exception as e:
            logger.debug(f"_realized_since_baseline failed: {e}")
            return round(est_profit, 6)

    def _net_of_fee(self, gross: float, notional: float, fee_rate: Optional[float] = None) -> float:
        """Subtract estimated round-trip fees from a gross profit figure.
        AUDIT FIX: prefer the LIVE (VIP-aware) round-trip fee carried by the grid bot
        instead of the static config taker rate."""
        _rt = float(fee_rate) if fee_rate else (float(getattr(config, "taker_fee_rate", 0.0004)) * 2.0)
        return round(gross - abs(notional) * _rt, 6)

    def _post_news_volatility_settled(self, snapshot: Optional[dict], news_guard: dict) -> tuple:
        """Returns (settled: bool, reason: str) for a post-release news window.

        The market moves most violently right AFTER a high-impact release; a fixed timer
        re-opens entries while the tape is still whipping. This measures volatility instead:
        an entry is allowed only when ATR(now)/ATR(20-avg) has fallen back to <= the gate
        threshold (already-settled tape), OR the calm ceiling has fully elapsed (escape hatch
        so a persistent storm cannot stall the bot forever). Pre-release, never settled.
        """
        try:
            if not bool(getattr(config, "volatility_gate_enabled", True)):
                return True, "volatility gate disabled"
            if not news_guard.get("is_post_release"):
                return False, "berita belum rilis — mode siaga penuh"
            thr = float(getattr(config, "volatility_gate_atr_ratio", 1.5))
            mins_since = float(news_guard.get("minutes_since_release", 0.0) or 0.0)
            calm_min = float(getattr(config, "volatility_calm_minutes", 45))
            # FAIL-CLOSED: an unknown/degenerate ATR ratio is NOT evidence the tape settled.
            # Never let a missing/NaN measurement read as "calm" (the old default 1.0 did exactly
            # that, allowing entries right after news whenever the indicator was unavailable).
            # Hold — the calm-window escape hatch below still frees the bot eventually.
            try:
                atr_ratio = float((snapshot or {}).get("indicators", {}).get("atr_ratio", float("nan")))
            except Exception:
                atr_ratio = float("nan")
            if not (math.isfinite(atr_ratio) and atr_ratio > 0):
                if mins_since >= calm_min:
                    return True, f"ATR tak terukur; jendela tenang {calm_min:.0f}mnt terlampaui → lanjut hati-hati"
                return False, f"⚠️ ATR tak terukur (data indikator hilang/rusak) — menahan entry ({mins_since:.0f}mnt sejak rilis)"
            if atr_ratio <= thr:
                return True, f"volatilitas reda (ATR×{atr_ratio:.2f} ≤ {thr:.2f})"
            if mins_since >= calm_min:
                return True, f"jendela tenang {calm_min:.0f}mnt terlampaui (ATR×{atr_ratio:.2f} masih tinggi, lanjut dengan hati-hati)"
            return False, f"⚠️ volatilitas masih tinggi (ATR×{atr_ratio:.2f} > {thr:.2f}, {mins_since:.0f}mnt sejak rilis)"
        except Exception:
            # Never let the volatility gate itself become the reason entries are blocked forever.
            return True, "volatility gate error → fallback allow"

    def _sync_hard_stop(self, sym: str, coin: "BasketCoinState", trader: Optional[BinanceFuturesTrader], reason: str = ""):
        """
        Mirrors the in-memory stop_loss onto a REAL STOP_MARKET order on Binance so the
        position is protected even if this process dies. Cheap-guarded: only fires when
        the desired stop actually changed since the last sync.
        """
        if not config.use_exchange_hard_stop or trader is None or coin.grid_bot is None:
            return
        sl = float(getattr(coin.grid_bot, "stop_loss", 0.0) or 0.0)
        if sl <= 0:
            return
        if abs(sl - float(getattr(coin, "_hard_stop_synced", 0.0) or 0.0)) < 1e-9:
            return
        ok = trader.set_protective_stop(sym, sl, position_side="LONG")
        if ok:
            coin._hard_stop_synced = sl
            if reason:
                sq = sym.split('/')[1].split(':')[0] if '/' in sym else 'USDT'
                self.add_log(f"🛡️ [{sym} HARD STOP @ BURSA] Stop-Loss nyata terpasang di Binance @ ${sl} ({reason}). Posisi tetap aman walau bot mati/restart.", "success")

    def _record_memory_outcome(self, sym: str, coin: "BasketCoinState", pnl: float, exit_type: str, ref_price: float):
        """
        P7: single funnel for every memory write on position exit, so each record carries
        the frozen entry/stop anchors needed for an R-multiple (comparable to backtest).
        """
        if not hasattr(self, 'memory'):
            return
        # AUDIT FIX: copy the snapshot before mutating — the old code wrote current/exit
        # price straight into the shared dict, corrupting previously recorded outcomes.
        mem_snap = dict(getattr(coin, 'initial_snapshot', None) or {})
        mem_snap["current_price"] = ref_price
        mem_snap["exit_price"] = ref_price
        mem_snap["position_notional_usdt"] = float(getattr(coin, 'investment', 0.0) or 0.0) * float(getattr(coin, 'leverage', 1) or 1)
        # Fallback: resumed/adopted positions never passed the fresh-entry anchor block.
        entry_anchor = float(getattr(coin, 'r_anchor_entry', 0.0) or 0.0) or float(getattr(coin, 'entry_price', 0.0) or 0.0)
        stop_anchor = float(getattr(coin, 'r_anchor_stop', 0.0) or 0.0)
        if stop_anchor <= 0 or stop_anchor >= entry_anchor:
            # AUDIT FIX: resumed/adopted positions may never have set r_anchor_stop —
            # fall back to the live grid stop so the trade still yields a valid R-multiple.
            stop_anchor = float(getattr(getattr(coin, 'grid_bot', None), 'stop_loss', 0.0) or 0.0)
        if stop_anchor <= 0 or stop_anchor >= entry_anchor:
            stop_anchor = 0.0  # unknown risk distance -> no R-multiple rather than a wrong one
        self.memory.record_trade_outcome(
            sym, mem_snap, pnl, exit_type,
            entry_price=entry_anchor,
            stop_price=stop_anchor,
        )
        # Audit ledger: immutable record of the realised exit (the order itself is already
        # logged at the ccxt choke point; this adds the accounting/P&L verdict).
        try:
            from src.trade_ledger import ledger
            ledger.append("trade_exit", symbol=sym, exit_type=exit_type, pnl=round(float(pnl), 6),
                          entry_price=entry_anchor, stop_price=stop_anchor,
                          exit_price=float(ref_price or 0.0))
        except Exception:
            pass

    def set_event_loop(self, loop):
        self.event_loop = loop

    def add_log(self, text: str, log_type: str = "info"):
        now_str = datetime.now().strftime("%H:%M:%S")
        entry = {"time": now_str, "text": text, "type": log_type}
        with self.lock:
            self.logs.append(entry)
            if len(self.logs) > 150:
                self.logs.pop(0)

        # Broadcast via WebSockets
        self.broadcast_event({"event": "LOG", "data": entry})

    def broadcast_event(self, message: Dict[str, Any]):
        if not self.ws_clients or not self.event_loop:
            return
        
        async def _send():
            dead = set()
            for ws in list(self.ws_clients):
                try:
                    await ws.send_json(message)
                except Exception:
                    dead.add(ws)
            self.ws_clients.difference_update(dead)

        try:
            asyncio.run_coroutine_threadsafe(_send(), self.event_loop)
        except Exception:
            pass

    def get_current_news_guard(self) -> Dict[str, Any]:
        """Returns cached news guard state, refreshing at most once every 30 seconds to prevent blocking."""
        now = time.time()
        if now - self.last_news_guard_check > 30.0:
            try:
                self.cached_news_guard = self.news_sentinel.check_news_guard()
                self.last_news_guard_check = now
            except Exception:
                pass
        return self.cached_news_guard

    @staticmethod
    def _json_safe(obj: Any) -> Any:
        """Recursively replace non-finite floats (inf/nan) with None so the state
        payload always survives a strict JSON encoder -> no 500 on /api/status."""
        if isinstance(obj, float):
            return obj if math.isfinite(obj) else None
        if isinstance(obj, dict):
            return {k: BotWebManager._json_safe(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [BotWebManager._json_safe(v) for v in obj]
        return obj

    @staticmethod
    def _mtf_payload(snapshot: Optional[dict], timeframe: str) -> Dict[str, Any]:
        """Read-only multi-timeframe view for the dashboard. NEVER fabricates a value: a
        missing/failed feed yields None fields rather than a guessed RSI. Only reads keys
        already present in the market snapshot (mtf_1h from market_data.py + indicators)."""
        snap = snapshot if isinstance(snapshot, dict) else {}
        m1h = snap.get("mtf_1h")
        mtf_1h = None
        if isinstance(m1h, dict):
            _r = m1h.get("rsi")
            mtf_1h = {
                "trend": m1h.get("trend") or "NEUTRAL",
                "rsi": round(float(_r), 2) if isinstance(_r, (int, float)) else None,
            }
        ind = snap.get("indicators") if isinstance(snap.get("indicators"), dict) else {}
        _rp = ind.get("rsi_14")
        mtf_primary = {
            "tf": timeframe,
            "trend": snap.get("trend_bias") or "NEUTRAL",
            "rsi": round(float(_rp), 2) if isinstance(_rp, (int, float)) else None,
        }
        return {"mtf_1h": mtf_1h, "mtf_primary": mtf_primary}

    def get_state(self) -> Dict[str, Any]:
        news_info = self.get_current_news_guard()
        with self.lock:
            basket_list = [c.to_dict() for c in self.basket_coins.values()]
            symbols_list = [c.symbol for c in self.basket_coins.values()]
            
            # Primary coin view for ladder visualizer
            primary_coin = None
            if self.symbol and self.symbol in self.basket_coins:
                primary_coin = self.basket_coins[self.symbol]
            elif self.basket_coins:
                primary_coin = next(iter(self.basket_coins.values()))

            ladder_summary = primary_coin.grid_summary if primary_coin else self.grid_summary
            ladder_grids = primary_coin.active_grids if primary_coin else self.active_grids
            ladder_price = primary_coin.current_price if primary_coin else self.current_price
            ladder_sl = (float(getattr(primary_coin.grid_bot, "stop_loss", 0.0) or 0.0) if primary_coin and primary_coin.grid_bot else (primary_coin.grid_summary.get("stop_loss", 0.0) if primary_coin and primary_coin.grid_summary else self.grid_summary.get("stop_loss", 0.0)))

            total_deployed = sum(len(c.deployed_orders) for c in self.basket_coins.values()) if self.basket_coins else len(self.deployed_orders)
            total_contracts = sum(c.position_contracts for c in self.basket_coins.values()) if self.basket_coins else self.position_contracts
            total_upnl = sum(c.unrealized_pnl for c in self.basket_coins.values()) if self.basket_coins else self.unrealized_pnl

            return self._json_safe({
                "is_running": self.is_running,
                "kill_switch_active": self._kill_switch_active(),
                "symbol": self.symbol or (", ".join(symbols_list) if symbols_list else ""),
                "symbols": symbols_list,
                "basket_size": self.basket_size,
                "basket": basket_list,
                "mode": self.mode,
                "quote_asset": self.quote_asset,
                "investment": self.investment,
                "grid_count": self.grid_count,
                "leverage": self.leverage,
                "max_leverage": int(getattr(config, "max_leverage", 10)),
                "total_notional": round(float(self.investment or 0.0) * max(1, int(self.leverage or 1)), 2),
                "max_total_notional_usdt": float(getattr(config, "max_total_notional_usdt", 0.0) or 0.0),
                "current_price": ladder_price,
                "realized_profit": round(self.realized_profit, 4),
                "matched_trades": self.matched_trades,
                "unrealized_pnl": round(total_upnl, 4),
                "position_contracts": total_contracts,
                "wallet_balance": round(self.wallet_balance, 2),
                "available_balance": round(self.available_balance, 2),
                "max_exposure_mult": float(getattr(config, "max_exposure_mult", 0.0) or 0.0),
                "effective_notional_cap": round(BotWebManager._effective_notional_cap(self.wallet_balance), 2),
                "grid_summary": ladder_summary,
                "stop_loss": ladder_sl,
                "tp1_price": round(primary_coin.tp1_price, 6) if primary_coin else 0.0,
                "tp1_filled": primary_coin.tp1_filled if primary_coin else False,
                "active_grids": ladder_grids,
                "deployed_orders_count": total_deployed,
                "daily_trades": self.daily_trades_count,
                "max_daily_trades": self.max_daily_trades,
                "entries_today": self.entry_count_utc,
                "max_daily_entries": self.max_daily_entries,
                "daily_pnl": round(self.daily_realized_pnl, 4),
                "daily_status": "CIRCUIT_LOCKED" if self.daily_circuit_breaker_triggered else "DISCIPLINED_ACTIVE",
                "news_guard": news_info,
                "memory_stats": self.memory.get_summary_stats(),
                "performance": self.get_performance_metrics(),
                "logs": self.logs[-40:]
            })

    def switch_mode(self, new_mode: str) -> Dict[str, Any]:
        """Switches trading environment between demo, live, and simulation, updating wallet balance directly from Binance or virtual balance."""
        valid_modes = ["demo", "live", "simulation", "backtest"]
        if new_mode not in valid_modes:
            return {"success": False, "error": f"Invalid mode: {new_mode}"}

        with self.lock:
            if self.is_running:
                return {"success": False, "error": "Tidak dapat beralih mode saat bot sedang berjalan. Hentikan bot terlebih dahulu."}
            self.mode = new_mode
            self.realized_profit = 0.0
            self.matched_trades = 0
            self.unrealized_pnl = 0.0
            self.position_contracts = 0.0
            self.basket_coins.clear()
            self.deployed_orders.clear()

        # Connect to Binance for selected mode to query true real-time balance
        target_asset = getattr(self, "quote_asset", "USDT")
        if new_mode in ["demo", "live"]:
            is_testnet = (new_mode == "demo")
            try:
                self.trader = BinanceFuturesTrader(is_testnet=is_testnet)
                bal = self.trader.get_balance(asset=target_asset, force=True)
                with self.lock:
                    self.wallet_balance = bal.get("total", 0.0)
                    self.available_balance = bal.get("free", 0.0)
                    if self.available_balance > 0:
                        self.investment = round(self.available_balance * float(getattr(config, "margin_usage_pct", 0.85)), 2)
                    else:
                        self.investment = 0.0
                try:
                    rp = self.trader.get_realized_pnl()
                    with self.lock:
                        self.realized_profit = rp
                except Exception:
                    pass
                self.add_log(f"Trading environment beralih ke {new_mode.upper()}! Saldo Binance: ${self.wallet_balance:.2f} {target_asset} (Free: ${self.available_balance:.2f})", "success")
                if new_mode == "live" and self.available_balance <= 0.0:
                    other = self.trader.check_other_wallets(asset=target_asset)
                    if other.get("earn", 0) > 0:
                        self.add_log(f"💡 Terdeteksi saldo {other['earn']:.2f} {target_asset} di dompet Binance Earn (Simple Earn Flexible)!", "info")
                        self.add_log(f"👉 Cara pindahkan: Buka Binance App -> Wallets -> Earn -> Pilih {target_asset} -> Klik Redeem (Tebus) ke Spot -> Lalu Transfer dari Spot ke USD-M Futures.", "warning")
                    elif other.get("spot", 0) > 0:
                        self.add_log(f"💡 Terdeteksi saldo {other['spot']:.2f} {target_asset} di dompet Binance Spot!", "info")
                        self.add_log(f"👉 Cara pindahkan: Buka Binance App -> Wallets -> Futures -> Klik Transfer dari Spot ke USD-M Futures.", "warning")
                    else:
                        self.add_log(f"⚠️ [PERHATIAN LIVE]: Saldo USD-M Futures Anda di Binance saat ini $0.00 {target_asset}. Silakan buka aplikasi Binance -> Wallet -> Futures -> Transfer {target_asset} dari Spot ke USD-M Futures agar bot dapat menempatkan order.", "warning")
            except Exception as e:
                self.add_log(f"Gagal menghubungkan ke Binance {new_mode.upper()}: {e}", "error")
        elif new_mode == "simulation":
            with self.lock:
                self.wallet_balance = config.paper_initial_balance
                self.available_balance = config.paper_initial_balance
                self.investment = config.default_investment
            self.add_log(f"Trading environment beralih ke SIMULASI! Saldo virtual: ${self.wallet_balance:,.2f} {target_asset}.", "info")

        state = self.get_state()
        self.broadcast_event({"event": "STATE_UPDATE", "data": state})
        return {"success": True, "mode": new_mode, "state": state}

    def start_bot(self, symbol: Optional[str] = None, mode: str = "demo", investment: float = 0.0,
                  grid_count: int = 8, auto_pick: bool = True, stop_loss: Optional[float] = None,
                  basket_size: int = 3, leverage: Optional[int] = None, quote_asset: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            # Filesystem kill switch: refuse to START while the panic file exists. Placed before
            # any state mutation so a halted bot is never silently revived from the dashboard.
            if self._kill_switch_active():
                return {"success": False, "message": f"KILL SWITCH aktif: file '{getattr(config, 'kill_switch_file', 'KILL_SWITCH')}' ada. Hapus file itu dulu untuk mengaktifkan bot."}
            self.stop_requested = True
            self.session_id += 1
            current_session = self.session_id

            self.stop_requested = False
            target_mode = mode or "demo"
            if self.mode != target_mode:
                self.realized_profit = 0.0
                self.matched_trades = 0
                self.unrealized_pnl = 0.0
                self.position_contracts = 0.0
            self.mode = target_mode
            self.investment = investment
            self.grid_count = grid_count
            self.custom_stop_loss = stop_loss
            if leverage is not None:
                # MANDATE: clamp the requested leverage to the hard ceiling. The API used to
                # accept any value (e.g. 125), so gross notional = margin*leverage was unbounded.
                self.leverage = max(1, min(int(leverage), int(getattr(config, "max_leverage", 10))))
            if quote_asset and quote_asset.strip():
                self.quote_asset = quote_asset.strip().upper()

            # If user explicitly specified a single symbol manually, use single coin mode
            if symbol and symbol.strip() and not auto_pick:
                self.basket_size = 1
                clean = normalize_symbol_dynamic(symbol, default_quote=self.quote_asset)
                # FIX 1(c): the manual path is an entry path too — ban junk symbols here
                # before the symbol is ever adopted as the bot's target.
                if not is_valid_symbol(clean):
                    return {"success": False, "message": f"Simbol '{clean}' ditolak: bukan simbol koin yang valid (JUNK BAN). Gunakan pasangan resmi Binance Futures seperti BTC/USDT."}
                self.symbol = clean
                if "/" in clean:
                    self.quote_asset = clean.split("/")[1].split(":")[0]
            else:
                self.basket_size = max(1, min(basket_size, int(getattr(config, "max_open_positions", 2))))
                self.symbol = symbol or ""
                if self.symbol and "/" in self.symbol:
                    self.quote_asset = self.symbol.split("/")[1].split(":")[0]

            self.is_running = True
            self.basket_coins = {}

        self.bot_thread = threading.Thread(target=self._run_loop, args=(current_session, auto_pick), daemon=True)
        self.bot_thread.start()

        mode_label = self.mode.upper()
        basket_label = f"Basket {self.basket_size} Koin" if (auto_pick or self.basket_size > 1) else (self.symbol or "Auto-Pick #1")
        return {"success": True, "message": f"Bot berhasil dimulai ({basket_label}) [Mode: {mode_label}]."}

    def stop_bot(self) -> Dict[str, Any]:
        with self.lock:
            symbols_to_cancel = list(self.basket_coins.keys())
            if self.symbol and self.symbol not in symbols_to_cancel:
                symbols_to_cancel.append(self.symbol)
            # AUDIT FIX: snapshot each coin's intended stop BEFORE clearing state, so a stop
            # can be restored if the cleanup below would otherwise leave an OPEN position
            # naked.
            protect_map = {}
            for _s, _c in self.basket_coins.items():
                _sl = float(getattr(getattr(_c, "grid_bot", None), "stop_loss", 0.0) or 0.0)
                if _sl > 0:
                    protect_map[_s] = _sl
            current_mode_at_stop = self.mode
            current_session = self.session_id + 1
            self.session_id = current_session
            self.stop_requested = True
            self.is_running = False
            self.deployed_orders.clear()
            self.active_grids.clear()
            for c in self.basket_coins.values():
                c.deployed_orders.clear()
                c.active_grids.clear()
            self.symbol = ""
            self.basket_coins.clear()
            self.grid_summary.clear()
            self.current_price = 0.0

        self.add_log("🛑 User requested to STOP the Bot. Cleaning up orders across all basket coins...", "warning")

        # Perform Binance cancellation asynchronously in background thread so HTTP response is instant (demo/live only)
        if current_mode_at_stop in ["demo", "live"]:
            def _async_cancel(stopped_mode: str, stop_session: int):
                try:
                    is_testnet = (stopped_mode == "demo")
                    trader = BinanceFuturesTrader(is_testnet=is_testnet)
                    total_cancelled = 0
                    for sym in symbols_to_cancel:
                        try:
                            total_cancelled += trader.cancel_all_orders(sym)
                        except Exception:
                            pass
                    # SAFETY: cancel_all_orders also removes conditional stops. If a position
                    # is still OPEN after cleanup (stop the bot != close the position), re-arm
                    # its protective STOP_MARKET so we never leave it naked on the exchange.
                    try:
                        for pos in trader.get_open_positions():
                            psym = pos.get("symbol")
                            if psym and abs(float(pos.get("contracts", 0) or 0)) > 0:
                                sl = float(protect_map.get(psym, 0.0) or 0.0)
                                if sl > 0:
                                    trader.set_protective_stop(psym, sl, position_side=pos.get("side", "LONG"))
                                    print(f"[stop_bot] Re-armed protective stop for still-open {psym} @ {sl}")
                    except Exception as _pe:
                        print(f"[stop_bot] re-protect notice: {_pe}")
                    # CRITICAL GUARD: Only update wallet_balance if bot is STILL in this stopped_mode!
                    # If user switched to "simulation" in the meantime, NEVER overwrite virtual balance with Binance balance!
                    with self.lock:
                        if self.mode == stopped_mode and self.session_id == stop_session:
                            target_asset = getattr(self, "quote_asset", "USDT")
                            bal = trader.get_balance(asset=target_asset, force=True)
                            self.wallet_balance = bal.get("total", self.wallet_balance)
                            self.available_balance = bal.get("free", self.available_balance)
                    self.broadcast_event({"event": "STATE_UPDATE", "data": self.get_state()})
                    self.add_log(f"Semua active bot orders ({total_cancelled}) berhasil dibatalkan di Binance.", "info")
                except Exception as e:
                    self.add_log(f"Order cleanup notice: {e}", "info")

            threading.Thread(target=_async_cancel, args=(current_mode_at_stop, current_session), daemon=True).start()

        if os.path.exists("bot_live_state.json"):
            try:
                os.remove("bot_live_state.json")
            except Exception:
                pass

        self.add_log("🛑 Bot stopped safely & Target Coin reset.", "warning")
        self.broadcast_event({"event": "STATE_UPDATE", "data": self.get_state()})
        return {"success": True, "message": "Bot stopped, target coin reset, and orders cancelled safely."}

    def reset_orders_and_target(self) -> Dict[str, Any]:
        """Cleans up all deployed orders, active grids, and resets target coin/basket."""
        with self.lock:
            self.symbol = ""
            self.basket_coins.clear()
            self.deployed_orders.clear()
            self.active_grids.clear()
            self.grid_summary.clear()
            self.current_price = 0.0
            if self.is_running:
                self.session_id += 1
                self.stop_requested = True
                self.is_running = False

        if self.trader and self.mode in ["demo", "live"]:
            try:
                target_asset = getattr(self, "quote_asset", "USDT")
                bal = self.trader.get_balance(asset=target_asset, force=True)
                with self.lock:
                    if self.mode in ["demo", "live"]:
                        self.wallet_balance = bal.get("total", self.wallet_balance)
                        self.available_balance = bal.get("free", self.available_balance)
            except Exception:
                pass

        self.add_log("🧹 Target Koin & jaring order dibersihkan (Standby untuk koin baru).", "info")
        self.broadcast_event({"event": "STATE_UPDATE", "data": self.get_state()})
        return {"success": True, "message": "Target koin & level grid berhasil di-reset."}

    @staticmethod
    def _venue_min_notional(market, price: float) -> float:
        """VENUE-TRUE min notional: max(cost.min, amount.min*price) from exchangeInfo.
        config.min_notional_threshold is only a fallback when the market is null/empty
        or the venue publishes no usable limit — never an override of a real venue value."""
        try:
            _fallback = float(getattr(config, "min_notional_threshold", 5.5) or 0.0)
            if not market:
                return _fallback
            _lim = market.get('limits', {}) or {}
            _c = float((_lim.get('cost') or {}).get('min') or 0.0)
            _a = float((_lim.get('amount') or {}).get('min') or 0.0)
            _px = float(price or 0.0)
            _v = max(_c, _a * _px if _px > 0 else 0.0)
            return _v if _v > 0 else _fallback
        except Exception:
            return float(getattr(config, "min_notional_threshold", 5.5) or 0.0)

    @staticmethod
    def _ramp_grids_for_min_notional(investment: float, leverage: int, grid_count: int, min_notional: float) -> int:
        """GRID RAMPING: lower grid_count so notional/grid >= the venue minimum.
        Never clamps UP notional (that would breach the risk cap). Floor is
        config.grid_ramp_min_grids (minimal viable 2-3 grids). Returns the floor when
        even minimal grids cannot meet the minimum — the caller then skips the entry."""
        try:
            _floor = max(2, int(getattr(config, "grid_ramp_min_grids", 2) or 2))
            _g = max(_floor, int(grid_count or _floor))
            _mn = float(min_notional or 0.0)
            _total = float(investment or 0.0) * max(1, int(leverage or 1))
            if _total <= 0 or _mn <= 0:
                return _g
            _viable = int(_total // _mn)
            if _viable < _floor:
                return _floor
            return min(_g, max(_floor, _viable))
        except Exception:
            return int(grid_count or 0)

    def _is_coin_affordable(self, symbol: str, price: float, investment: float, trader: Optional[BinanceFuturesTrader]) -> bool:
        """
        Validates whether a coin's minimum lot / notional size can be comfortably traded
        within the coin's allocated margin budget at current leverage.
        Prevents code: -2019 'Margin is insufficient' on coins with large minimum notional (e.g. AAVE, BTC)
        when operating on smaller account balances.
        """
        if not trader or not hasattr(trader, 'exchange') or not trader.exchange.markets:
            return True
        try:
            resolved = trader.resolve_symbol(symbol)
            if not resolved or resolved not in trader.exchange.markets:
                return False
            market = trader.exchange.market(resolved)
            order_cost = BotWebManager._venue_min_notional(market, price)
            margin_per_order = order_cost / max(1, self.leverage)
            
            # If 1 single order takes more than 75% of the coin's allocation,
            # or more than 45% of the account's total available balance, it cannot sustain grid trading
            quote_currency = symbol.split('/')[1].split(':')[0] if '/' in symbol else getattr(self, "quote_asset", "USDT")
            bal = trader.get_balance(quote_currency)
            free_bal = float(bal.get('free', 0.0) or 0.0)
            
            _inv_frac = float(getattr(config, "affordable_investment_frac", 0.75) or 0.75)
            _free_frac = float(getattr(config, "affordable_free_frac", 0.45) or 0.45)
            if free_bal > 0 and margin_per_order > (free_bal * _free_frac):
                return False
            if margin_per_order > (investment * _inv_frac):
                return False
            return True
        except Exception:
            return True

    def _edge_aligned_scan(self, quote_currency: str, limit: int, force_refresh: bool = False):
        """Auto-pick scanner that mirrors the live Edge Gate.

        Selects coins that will actually survive the entry gate (trend != BULLISH, live
        volume, real ATR) instead of BULLISH runners the gate rejects — so the basket the
        bot builds is enterable. Falls back to the raw alpha scanner only if the aligned
        scanner is unavailable (older build / unexpected error).
        """
        try:
            return self.scanner.scan_edge_aligned_coins(
                quote_asset=quote_currency, limit=limit, force_refresh=force_refresh
            )
        except Exception:
            return self.scanner.scan_asymmetric_alpha_coins(quote_asset=quote_currency, limit=limit)

    @staticmethod
    def _kill_switch_active(path: Optional[str] = None) -> bool:
        """Panic halt: the presence of the kill-switch file stops trading regardless of the
        dashboard/API/systemd state. Path comes from config (env-overridable), never a
        literal. Checked at the loop choke points and at start_bot, so it is fail-closed on
        the entry side while leaving open positions and their exchange stops untouched."""
        p = path or str(getattr(config, "kill_switch_file", "KILL_SWITCH") or "KILL_SWITCH")
        try:
            return os.path.exists(p)
        except Exception:
            return True  # fail-CLOSED: an unreadable filesystem must halt, never trade blind

    @staticmethod
    def _tp1_already_fired(entry_price: float, adopted_stop: float) -> bool:
        """A stop strictly ABOVE entry only exists after BEP+/trailing activated, which
        happens exclusively on a TP1 cash-in. Lets a restart infer TP1 without in-memory state."""
        return entry_price > 0 and adopted_stop > entry_price * 1.0001

    @staticmethod
    def _tp_rotation_remain(sym_side, live_price: float, entry_price: float, qty: float) -> float:
        """Runner remainder PnL, side-aware: long = (live-entry)*qty, short = (entry-live)*qty."""
        try:
            _q = abs(float(qty or 0.0))
            if "SHORT" in str(sym_side or "LONG").upper():
                return (float(entry_price or 0.0) - float(live_price or 0.0)) * _q
            return (float(live_price or 0.0) - float(entry_price or 0.0)) * _q
        except Exception:
            return 0.0

    def _tp_rotation_retry_delay(self, sym: str) -> float:
        """Short backoff between rotation retries: base * 2^streak, capped at per-symbol cd."""
        try:
            base = float(getattr(config, "tp_rotation_retry_cooldown_sec", 60) or 0)
        except Exception:
            base = 60.0
        if base <= 0:
            return 0.0
        streak = int((getattr(self, "_tp_rotation_fail_streak", None) or {}).get(sym, 0) or 0)
        cap = float(getattr(config, "tp_cooldown_sec", 300) or 0) or base
        return min(base * (2 ** min(max(streak, 0), 4)), max(cap, base))

    def _tp_rotation_note_skip(self, sym: str) -> None:
        """Record a failed/skipped attempt: bump backoff streak + stamp skip time."""
        try:
            with self.lock:
                if not isinstance(getattr(self, "_tp_rotation_fail_streak", None), dict):
                    self._tp_rotation_fail_streak = {}
                if not isinstance(getattr(self, "_tp_rotation_last_skip_ts", None), dict):
                    self._tp_rotation_last_skip_ts = {}
                self._tp_rotation_fail_streak[sym] = int(self._tp_rotation_fail_streak.get(sym, 0) or 0) + 1
                self._tp_rotation_last_skip_ts[sym] = time.time()
        except Exception:
            pass
    @staticmethod
    def _tp_rotation_be_stop(entry_price: float, atr_live: float, venue_tick: float,
                             old_stop: float, precision: int,
                             live_fee_rate: Optional[float] = None) -> float:
        """Adaptive BE+ lock for the TP-rotation runner: buffer = max(venue tick,
        k*ATR_live, round-trip fee). k = config.tp_rotation_be_atr_mult (env
        TP_ROTATION_BE_ATR_MULT). fee = LIVE taker round-trip when provided, else
        config. Never a dead literal; guarantees SL > entry AND SL > old stop.
        Returns 0.0 when no usable entry (caller skips cleanly)."""
        try:
            e = float(entry_price or 0.0)
            if e <= 0:
                return 0.0
            a = float(atr_live or 0.0) or (e * float(getattr(config, "atr_fallback_pct", 0.02)))
            k = float(getattr(config, "tp_rotation_be_atr_mult", 0.15) or 0.0)
            tick = float(venue_tick or 0.0)
            _rt = float(live_fee_rate) if live_fee_rate else float(config.round_trip_fee_rate())
            fee_floor = e * (_rt + float(getattr(config, "fee_buffer_pct", 0.001)))
            buf = max(fee_floor, k * a, tick)
            sl = round(e + buf, int(precision))
            os_ = float(old_stop or 0.0)
            if os_ > 0 and sl < os_:
                sl = round(os_ + max(tick, k * a, fee_floor), int(precision))
            return sl
        except Exception:
            return 0.0

    def _maybe_tp_rotate(self, sym: str, coin: "BasketCoinState", trader,
                         live_price: float, is_live_execution: bool, session_id: int,
                         blacklist_symbols, inv_per_coin: float, auto_pick: bool = True) -> bool:
        """TP-ROTATION driver (opt-in, OFF default). Fires once per coin after a TP1
        fill (coin.tp1_filled or _tp1_already_fired ground truth): locks the runner
        at the adaptive BE+ stop (_tp_rotation_be_stop: max(venue tick via
        exchangeInfo, k*ATR live, live round-trip fee)), then frees a slot for a
        fresh pick WITHOUT waiting for the runner to close. Guards (churn cap,
        cooldown, news, kill-switch, daily caps) via _tp_rotation_allowed; fee-aware
        skip when the runner remainder cannot cover its own fee. Audits TP1_HIT /
        SL_MOVED_BE / SLOT_FREED (or TP_ROTATION_SKIPPED) via _audit_tp_rotation.
        Never raises. Returns True iff a new coin was actually added."""
        try:
            if not bool(getattr(config, "tp_rotation_enabled", False)):
                return False
            if getattr(coin, "tp_rotated", False):
                return False
            entry = float(getattr(coin, "entry_price", 0.0) or 0.0)
            if entry <= 0 or abs(float(getattr(coin, "position_contracts", 0.0) or 0.0)) <= 0:
                return False
            gb = getattr(coin, "grid_bot", None)
            sl_now = float(getattr(gb, "stop_loss", 0.0) or 0.0)
            tp_fired = bool(getattr(coin, "tp1_filled", False)) or self._tp1_already_fired(entry, sl_now)
            if not tp_fired:
                return False
            if not getattr(coin, "tp1_filled", False):
                coin.tp1_filled = True
            if not getattr(coin, "_tp1_hit_audited", False):
                coin._tp1_hit_audited = True
                self._audit_tp_rotation("TP1_HIT", sym, entry_price=entry, tp1_price=float(getattr(coin, "tp1_price", 0.0) or 0.0), live_price=float(live_price or 0.0))
            # ---- retry backoff: a recent skip/fail parks this symbol briefly so a
            # TP1-runner does not rescan + spam-audit on every tick ----
            try:
                _last_skip = float((getattr(self, "_tp_rotation_last_skip_ts", None) or {}).get(sym, 0.0) or 0.0)
                _retry_d = float(self._tp_rotation_retry_delay(sym) or 0.0)
            except Exception:
                _last_skip, _retry_d = 0.0, 0.0
            if _retry_d > 0 and (time.time() - _last_skip) < _retry_d:
                return False  # silent: the skip was already audited when stamped
            # ---- kill-switch BEFORE touching SL: fail-closed, never move stops halted ----
            if BotWebManager._kill_switch_active():
                self._tp_rotation_note_skip(sym)
                self._audit_tp_rotation("TP_ROTATION_SKIPPED", sym, reason="kill-switch aktif")
                return False
            # ---- lock runner at adaptive BE+ (venue tick + live ATR + live fee) ----
            atr_live = float(getattr(gb, "atr", 0.0) or 0.0) or (float(live_price or entry) * float(getattr(config, "atr_fallback_pct", 0.02)))
            tick, prec, live_rt = 0.0, int(getattr(gb, "precision", 4) or 4), None
            if is_live_execution and trader is not None:
                try:
                    resolved = trader.resolve_symbol(sym)
                    tick = float(trader._stop_tick_size(resolved) or 0.0)
                    try:
                        if not trader.exchange.markets:
                            trader.exchange.load_markets()
                        tick_m = trader.exchange.market(resolved) or {}
                        prec = market_price_precision(tick_m) or prec
                    except Exception:
                        pass
                    _fr = trader.get_trading_fee_rate(sym)
                    if _fr:
                        live_rt = float(_fr) * 2.0
                except Exception:
                    pass
            new_sl = BotWebManager._tp_rotation_be_stop(entry, atr_live, tick, sl_now, prec, live_rt)
            if gb is not None and new_sl > 0 and new_sl > sl_now:
                gb.stop_loss = new_sl
                coin.is_breakeven_active = True
                if is_live_execution and trader is not None:
                    self._sync_hard_stop(sym, coin, trader, reason="TP rotation BE+")
                self._audit_tp_rotation("SL_MOVED_BE", sym, old_stop=sl_now, new_stop=new_sl, atr_live=atr_live, venue_tick=tick)
            # ---- free a slot for a fresh pick (runner keeps riding) ----
            # Check + attempt-consume under ONE lock: the daily cap counts ATTEMPTS
            # (not just successes), so a retry loop cannot bypass it, and two racing
            # ticks cannot both slip past the cap (check-then-act atomicity).
            with self.lock:
                allowed, reason = self._tp_rotation_allowed(sym)
                self._consume_tp_rotation_attempt(sym)
            if not allowed:
                self._tp_rotation_note_skip(sym)
                self._audit_tp_rotation("TP_ROTATION_SKIPPED", sym, reason=reason)
                return False
            _rt = float(live_rt) if live_rt else float(config.round_trip_fee_rate())
            _rem_qty = abs(float(getattr(coin, "position_contracts", 0.0) or 0.0))
            _notional = float(live_price or 0.0) * _rem_qty
            _side = (getattr(coin, "position_side", None)
                     or getattr(gb, "position_side", None)
                     or ("SHORT" if float(getattr(coin, "position_contracts", 0.0) or 0.0) < 0 else "LONG"))
            _remain = BotWebManager._tp_rotation_remain(_side, float(live_price or 0.0), entry, _rem_qty)
            if _notional > 0 and _remain < _notional * (_rt + float(getattr(config, "fee_buffer_pct", 0.001))):
                self._tp_rotation_note_skip(sym)
                self._audit_tp_rotation("TP_ROTATION_SKIPPED", sym, reason=f"fee-aware: sisa profit {_remain:.4f} < fee {_notional * _rt:.4f}")
                return False
            if not auto_pick:
                coin.tp_rotated = True
                self._audit_tp_rotation("SLOT_FREED", sym, mode="manual-no-pick", runner_stop=float(getattr(gb, "stop_loss", 0.0) or 0.0))
                return False
            quote_ccy = sym.split("/")[1].split(":")[0] if "/" in sym else getattr(self, "quote_asset", config.default_quote_asset)
            bl = blacklist_symbols if blacklist_symbols is not None else set()
            try:
                cands = self._edge_aligned_scan(quote_ccy, 35) or []
            except Exception:
                cands = []
            picked = None
            for b in cands:
                try:
                    cs = b.get("symbol")
                    if not cs or cs in bl or cs in self.basket_coins:
                        continue
                    if hasattr(self, "memory") and self.memory.is_symbol_in_cooldown(cs):
                        continue
                    if not self._is_coin_affordable(cs, float(b.get("price", 0) or 0), float(inv_per_coin or 0.0), trader if is_live_execution else None):
                        continue
                    nc = self._init_basket_coin(cs, float(inv_per_coin or 0.0), session_id, is_live_execution, trader if is_live_execution else None)
                    if nc and (not is_live_execution or len(nc.deployed_orders) > 0 or abs(nc.position_contracts) > 0):
                        picked = (cs, nc)
                        break
                except Exception:
                    continue
            if picked is None:
                self._tp_rotation_note_skip(sym)
                self._audit_tp_rotation("TP_ROTATION_SKIPPED", sym, reason="tidak ada kandidat edge-aligned yang lolos filter")
                return False
            with self.lock:
                self.basket_coins[picked[0]] = picked[1]
            coin.tp_rotated = True
            with self.lock:
                self._tp_rotation_last_ts[sym] = time.time()
                self._tp_rotation_last_ts_global = time.time()
                if isinstance(getattr(self, "_tp_rotation_fail_streak", None), dict):
                    self._tp_rotation_fail_streak[sym] = 0
            self._audit_tp_rotation("SLOT_FREED", sym, new_coin=picked[0], runner_stop=float(getattr(gb, "stop_loss", 0.0) or 0.0))
            try:
                self.broadcast_event({"event": "STATE_UPDATE", "data": self.get_state()})
            except Exception:
                pass
            return True
        except Exception as _e:
            logger.debug(f"_maybe_tp_rotate failed: {_e}")
            return False

    def _consume_tp_rotation_attempt(self, symbol: str) -> None:
        """Count every rotation ATTEMPT (not just successes) toward the daily cap,
        under the manager lock — closes the retry-forever bypass of check-then-act."""
        try:
            with self.lock:
                self._check_and_reset_daily_stats()
                self._tp_rotation_count = int(getattr(self, "_tp_rotation_count", 0) or 0) + 1
        except Exception:
            pass

    def _tp_rotation_allowed(self, symbol: str) -> tuple:
        """Guards for a TP-rotation slot-free. Order: enabled -> kill-switch ->
        daily discipline (circuit/loss/trade count) -> news guard -> churn cap ->
        per-symbol + global cooldown -> daily rotation cap (attempts included).
        Check + consume run under one lock so the cap cannot be raced past.
        Returns (allowed: bool, reason: str)."""
        try:
            if not bool(getattr(config, "tp_rotation_enabled", False)):
                return False, "TP rotation OFF (TP_ROTATION_ENABLED=false)"
            if BotWebManager._kill_switch_active():
                return False, "kill-switch aktif"
            self._check_and_reset_daily_stats()
            if getattr(self, "daily_circuit_breaker_triggered", False):
                return False, "circuit breaker harian aktif"
            if int(getattr(self, "daily_trades_count", 0) or 0) >= int(getattr(self, "max_daily_trades", 0) or 0):
                return False, f"batas transaksi harian ({self.daily_trades_count}/{self.max_daily_trades})"
            try:
                _ng = self.news_sentinel.check_news_guard()
            except Exception:
                _ng = {}
            if (_ng or {}).get("is_danger") or (_ng or {}).get("pause_entries"):
                return False, str((_ng or {}).get("caution_reason") or "news guard bahaya")
            if self._entry_blocked_by_churn_cap(False):
                return False, f"churn cap ({self.entry_count_utc}/{self.max_daily_entries})"
            _cd = float(getattr(config, "tp_cooldown_sec", 300) or 0)
            _last = float((getattr(self, "_tp_rotation_last_ts", None) or {}).get(symbol, 0.0) or 0.0)
            if _cd > 0 and (time.time() - _last) < _cd:
                return False, f"cooldown {int(_cd - (time.time() - _last))}s tersisa"
            _gcd = float(getattr(config, "tp_rotation_global_cooldown_sec", 60) or 0)
            _glast = float(getattr(self, "_tp_rotation_last_ts_global", 0.0) or 0.0)
            if _gcd > 0 and (time.time() - _glast) < _gcd:
                return False, f"cooldown global {int(_gcd - (time.time() - _glast))}s tersisa"
            _cap = int(getattr(config, "max_tp_rotations_per_day", 8) or 0)
            if _cap > 0 and int(getattr(self, "_tp_rotation_count", 0) or 0) >= _cap:
                return False, f"cap rotasi harian ({self._tp_rotation_count}/{_cap})"
            return True, "ok"
        except Exception as _e:
            logger.debug(f"_tp_rotation_allowed failed: {_e}")
            return False, "rotation guard error"

    def _audit_tp_rotation(self, event: str, symbol: str, **fields) -> None:
        """Audit funnel for TP-rotation transitions (TP1_HIT, SL_MOVED_BE, SLOT_FREED,
        TP_ROTATION_SKIPPED): append-only ledger + dashboard log. Never raises."""
        try:
            from src.trade_ledger import ledger as _ledger
            _ledger.append(event, symbol=symbol, **fields)
        except Exception as _e:
            logger.debug(f"_audit_tp_rotation ledger failed: {_e}")
        try:
            self.add_log(f"🔁 [{symbol} ROTASI] {event}: " + ", ".join(f"{k}={v}" for k, v in fields.items()), "info")
        except Exception:
            pass

    @staticmethod
    def _effective_notional_cap(wallet_balance: float) -> float:
        """MANDATE: the stricter of the absolute USD cap and the balance-scaled cap
        (max_exposure_mult * equity). 0 disables a leg; both 0 -> no cap."""
        _abs = float(getattr(config, "max_total_notional_usdt", 0.0) or 0.0)
        _mult = float(getattr(config, "max_exposure_mult", 0.0) or 0.0)
        _scaled = _mult * float(wallet_balance or 0.0) if _mult > 0 else 0.0
        _caps = [c for c in (_abs, _scaled) if c > 0]
        return min(_caps) if _caps else 0.0

    @staticmethod
    def _basket_notional_ok(existing_notional: float, add_notional: float,
                            cap: Optional[float] = None, wallet_balance: float = 0.0) -> bool:
        """MANDATE exposure ceiling: True iff adding `add_notional` keeps the basket's total
        gross notional (Σ margin*leverage) within the cap. cap<=0 disables the check."""
        c = float(cap if cap is not None else BotWebManager._effective_notional_cap(wallet_balance))
        if c <= 0:
            return True
        return (float(existing_notional or 0.0) + float(add_notional or 0.0)) <= c

    def _entry_blocked_by_churn_cap(self, pre_has_position: bool) -> bool:
        """True iff a FRESH entry must be blocked by the daily entry (churn) cap.
        An adopted position (pre_has_position=True) is exempt: an already-open position on
        the exchange must always be managed, never blocked. max_daily_entries=0 disables."""
        if pre_has_position:
            return False
        return self.max_daily_entries > 0 and self.entry_count_utc >= self.max_daily_entries

    @staticmethod
    def _clamp_risk_pct(base_pct: float, ai_pct=None) -> float:
        """RISK PER TRADE cap. The AI may only LOWER the risk fraction, never raise it
        above the cap (base_pct). An AI value above the cap is clamped DOWN to the cap; a
        missing/invalid AI value leaves the config default untouched. Floor keeps an
        over-cautious AI from sizing down to nothing. Config knobs, never literals."""
        base = float(base_pct or 0.0)
        floor = float(getattr(config, "risk_per_trade_floor_pct", 0.0) or 0.0)
        try:
            if ai_pct is None:
                return max(floor, base)
            ai = float(ai_pct)
        except (TypeError, ValueError):
            return max(floor, base)
        if ai <= 0:
            return max(floor, base)
        return max(floor, min(ai, base))

    @staticmethod
    def _risk_based_investment(wallet_balance: float, risk_pct: float,
                               entry_price: float, stop_loss: float, leverage: int) -> float:
        """Fixed-fractional sizing (the RiskManager concept, wired into the live grid path).
        risk_usd = wallet_balance * risk_pct; qty = risk_usd / |entry - stop_loss|; position
        notional = qty * entry; margin = notional / leverage. A WIDER stop therefore yields a
        SMALLER position. Returns 0.0 (never a fabricated size) when inputs are unusable."""
        try:
            risk_usd = float(wallet_balance or 0.0) * float(risk_pct or 0.0)
            risk_dist = abs(float(entry_price or 0.0) - float(stop_loss or 0.0))
            if risk_usd <= 0 or risk_dist <= 0 or float(entry_price or 0.0) <= 0:
                return 0.0
            qty = risk_usd / risk_dist
            return round((qty * float(entry_price)) / max(1, int(leverage)), 2)
        except Exception:
            return 0.0

    @staticmethod
    def _pyramid_plan(total_investment: float, entry_price: float, atr: float) -> list:
        """P1 PYRAMIDING LADDER: 50% now, +30% at entry+0.5xATR, +20% at entry+1.0xATR.
        Fractions and ATR multiples are config-driven (never literals). Returns [] (never a
        fabricated ladder) when any input is unusable, so callers can skip cleanly."""
        try:
            inv = float(total_investment or 0.0)
            e = float(entry_price or 0.0)
            a = float(atr or 0.0)
            if inv <= 0 or e <= 0 or a <= 0:
                return []
            f1 = float(getattr(config, "pyramid_entry_pct", 0.50))
            f2 = float(getattr(config, "pyramid_stage1_pct", 0.30))
            f3 = float(getattr(config, "pyramid_stage2_pct", 0.20))
            m1 = float(getattr(config, "pyramid_stage1_atr_mult", 0.5))
            m2 = float(getattr(config, "pyramid_stage2_atr_mult", 1.0))
            return [
                {"stage": 1, "pct": f1, "investment": round(inv * f1, 2), "trigger": round(e, 8)},
                {"stage": 2, "pct": f2, "investment": round(inv * f2, 2), "trigger": round(e + m1 * a, 8)},
                {"stage": 3, "pct": f3, "investment": round(inv * f3, 2), "trigger": round(e + m2 * a, 8)},
            ]
        except Exception:
            return []

    @staticmethod
    def _pyramid_add_qty(investment_slice: float, price: float, leverage: int,
                         market: Dict[str, Any], venue_min_notional: float) -> float:
        """Quantity for a staged add from exchangeInfo stepSize + VENUE-TRUE min notional.
        Never a hardcoded step/notional. notional = slice * leverage; bumped up to meet the
        venue min notional (config.min_notional_buffer), then floored to the venue step."""
        try:
            px = float(price or 0.0)
            inv = float(investment_slice or 0.0)
            lev = max(1, int(leverage or 1))
            if px <= 0 or inv <= 0:
                return 0.0
            qty = (inv * lev) / px
            mn = float(venue_min_notional or 0.0)
            if mn > 0:
                need = (mn * float(getattr(config, "min_notional_buffer", 1.02))) / px
                if qty < need:
                    qty = need
            step = 0.0
            try:
                for f in ((market.get("info") or {}).get("filters") or []):
                    if f.get("filterType") in ("LOT_SIZE", "MARKET_LOT_SIZE"):
                        step = float(f.get("stepSize", 0) or 0)
                        if step > 0:
                            break
            except Exception:
                step = 0.0
            if step <= 0:
                prec = market_qty_precision(market)
                step = (10 ** (-prec)) if isinstance(prec, int) and prec >= 0 else 0.0
            if step > 0:
                qty = math.floor(qty / step) * step
            dec = market_qty_precision(market)
            if isinstance(dec, int) and dec >= 0:
                qty = round(qty, dec)
            return qty if qty > 0 else 0.0
        except Exception:
            return 0.0

    @staticmethod
    def _pyramid_new_stop(avg_entry: float, atr_live: float, venue_tick: float,
                          old_stop: float, precision: int) -> float:
        """Adaptive breakeven+ stop for a staged add.

        buffer = max(fee-floor, k*ATR_live, venue_tick) -- EVERY term derived from the live
        ATR and exchangeInfo tick (k = config.pyramid_sl_buffer_atr_mult, env SL_BUFFER_ATR_MULT),
        never a dead literal. Guarantees SL_new > SL_old (long) AND > avg_entry (breakeven+);
        the increment used to clear the old stop is itself ATR/tick-scaled, not hardcoded."""
        try:
            e = float(avg_entry or 0.0)
            if e <= 0:
                return 0.0
            a = float(atr_live or 0.0) or (e * float(getattr(config, "atr_fallback_pct", 0.02)))
            k = float(getattr(config, "pyramid_sl_buffer_atr_mult", 0.15) or 0.0)
            tick = float(venue_tick or 0.0)
            fee_floor = e * (float(config.round_trip_fee_rate()) + float(getattr(config, "fee_buffer_pct", 0.001)))
            buf = max(fee_floor, k * a, tick)
            sl = round(e + buf, int(precision))
            os_ = float(old_stop or 0.0)
            if os_ > 0 and sl <= os_:
                sl = round(os_ + max(tick, k * a), int(precision))
            return sl
        except Exception:
            return 0.0

    def _pyramid_watch(self, sym: str, coin: "BasketCoinState",
                       trader: Optional[BinanceFuturesTrader], live_price: float,
                       is_live_execution: bool):
        """FILL-WATCHER for the pyramiding ladder. Completely inert unless
        config.pyramiding_auto is true (kill-switch, default OFF).

        For each open position, as live price reaches the next plan rung (0.5xATR then
        1.0xATR) it places that staged add -- but ONLY after re-seating the protective
        stop to breakeven+ over the NEW weighted-avg entry, covering the TOTAL contracts,
        and CONFIRMING it live on the exchange. No add is ever submitted while the old
        stop still stands. The ladder is abandoned once price runs past the last rung
        with a rung still unfilled, or the stop is hit."""
        if not bool(getattr(config, "pyramiding_auto", False)):
            return
        if getattr(coin, "pyramid_done", False) or abs(float(coin.position_contracts or 0.0)) <= 0:
            return
        plan = list(coin.pyramid_plan or [])
        if len(plan) < 2:
            return
        idx = int(getattr(coin, "pyramid_next_idx", 1) or 0)
        if idx >= len(plan):
            coin.pyramid_done = True
            return
        sl_now = float(getattr(getattr(coin, "grid_bot", None), "stop_loss", 0.0) or 0.0)
        if sl_now > 0 and live_price <= sl_now:
            coin.pyramid_done = True          # SL hit -> stop pyramiding
            return
        trigger = float(plan[idx].get("trigger") or 0.0)
        last_trigger = float(plan[-1].get("trigger") or 0.0)
        if trigger <= 0 or live_price < trigger:
            return
        if not (is_live_execution and trader):
            return
        # ---- venue-true market data from exchangeInfo (never hardcoded) ----
        try:
            resolved = trader.resolve_symbol(sym)
            if not trader.exchange.markets:
                trader.exchange.load_markets()
            market = trader.exchange.market(resolved) or {}
        except Exception as e:
            self.add_log(f"⏸️ [{sym} PYRAMID] exchangeInfo tak tersedia ({e}); add ditunda.", "warning")
            return
        price_prec = market_price_precision(market) or int(getattr(getattr(coin, "grid_bot", None), "precision", 4) or 4)
        venue_min = BotWebManager._venue_min_notional(market, live_price)
        try:
            venue_tick = float(trader._stop_tick_size(trader.resolve_symbol(sym)) or 0.0)
        except Exception:
            venue_tick = 0.0
        # ---- size this slice from the venue step + venue min notional ----
        inv_slice = float(plan[idx].get("investment") or 0.0)
        _lev = int(getattr(coin, "leverage", 0) or self.leverage or 1)
        add_qty = BotWebManager._pyramid_add_qty(inv_slice, live_price, _lev, market, venue_min)
        if add_qty <= 0:
            coin.pyramid_next_idx = idx + 1
            self.add_log(f"⚠️ [{sym} PYRAMID] Slice ${inv_slice:.2f} tak bisa memenuhi min notional venue ${venue_min:.2f}; rung dilewati.", "warning")
            return
        # ---- projected weighted-avg entry AFTER the add ----
        old_ct = abs(float(coin.position_contracts or 0.0))
        old_avg = float(getattr(coin, "entry_price", 0.0) or 0.0)
        if old_avg <= 0:
            try:
                _p = trader.get_open_positions(sym)
                old_avg = float((_p[0].get("entry_price") if _p else 0.0) or 0.0)
            except Exception:
                old_avg = 0.0
        total_ct = old_ct + add_qty
        proj_avg = (((old_ct * old_avg) + (add_qty * live_price)) / total_ct) if total_ct > 0 else old_avg
        # ---- adaptive breakeven+ SL (ATR/tick based; strictly ABOVE the old stop) ----
        atr_live = float(getattr(getattr(coin, "grid_bot", None), "atr", 0.0) or 0.0) or (live_price * float(getattr(config, "atr_fallback_pct", 0.02)))
        new_sl = BotWebManager._pyramid_new_stop(proj_avg, atr_live, venue_tick, sl_now, price_prec)
        if not (sl_now <= 0 or new_sl > sl_now):
            self.add_log(f"⏸️ [{sym} PYRAMID] SL baru ${new_sl} tak > SL lama ${sl_now}; add ditunda.", "warning")
            return
        # ---- STEP 1: seat the NEW SL for TOTAL contracts & CONFIRM live on the venue ----
        sl_ok = trader.set_protective_stop(sym, new_sl, amount=total_ct, position_side="LONG")
        if not sl_ok:
            self.add_log(f"⛔ [{sym} PYRAMID] SL baru ${new_sl} GAGAL dikonfirmasi di bursa — add TIDAK dieksekusi (posisi lama tetap terlindungi).", "error")
            return
        # ---- STEP 2: only NOW place the staged add ----
        res = trader.place_market_order(sym, "BUY", add_qty)
        if not res:
            self.add_log(f"⛔ [{sym} PYRAMID] SL ${new_sl} terpasang, tapi add gagal ({getattr(trader, 'last_error', '') or 'ditolak'}). Ladder dihentikan.", "error")
            coin.pyramid_done = True
            return
        filled = float(res.get("amount") or res.get("filled") or add_qty)
        # ---- reconcile against the exchange's real weighted-avg entry ----
        try:
            _p2 = trader.get_open_positions(sym)
            if _p2:
                coin.position_contracts = float(_p2[0].get("contracts", total_ct) or total_ct)
                _avg = float(_p2[0].get("entry_price") or proj_avg)
                if _avg > 0:
                    coin.entry_price = _avg
                    if coin.grid_bot:
                        coin.grid_bot.entry_price = _avg
        except Exception:
            coin.position_contracts = total_ct
        if coin.grid_bot:
            coin.grid_bot.stop_loss = new_sl
        coin._hard_stop_synced = new_sl
        coin.pyramid_next_idx = idx + 1
        self.add_log(f"➕ [{sym} PYRAMID STAGE {idx + 1}] Add {filled} kontrak @ ${live_price}. Weighted-avg entry → ${proj_avg:.6f}. SL breakeven+ diangkat ke ${new_sl} (buffer ATR/tick ${max(venue_tick, float(getattr(config, 'pyramid_sl_buffer_atr_mult', 0.15)) * atr_live):.6f}) COVER {total_ct} kontrak — dikonfirmasi di bursa SEBELUM add.", "success")
        # ---- STEP 3: ran-away guard ----
        if last_trigger > 0 and live_price > last_trigger and coin.pyramid_next_idx < len(plan):
            coin.pyramid_done = True
            self.add_log(f"🛑 [{sym} PYRAMID] Harga lewat 1.0xATR tanpa rung tersisa terisi — pyramiding dihentikan.", "warning")

    @staticmethod
    def _finalize_protective_stop(entry_price: float, stop_price: float, precision: int) -> float:
        """SINGLE SOURCE OF TRUTH for the protective stop. Applies, in order: the R:R clamp
        (risk <= reward / min_RR), the config min_stop_loss_pct floor, and the never-at-or-
        above-entry guard. SIZING, the frozen r_anchor_stop, and the exchange STOP_MARKET all
        consume THIS value, so a live loss can never drift from the sized risk (the SUI
        -3.68R bug). A profit-locked stop at/above entry (BEP+/runner) is returned untouched.
        Every threshold is config-driven via getattr — no literals."""
        try:
            entry = float(entry_price or 0.0)
            stop = float(stop_price or 0.0)
            if entry <= 0 or stop <= 0:
                return stop
            if stop >= entry:
                return stop  # BEP+/runner stop — never re-widen a locked-in profit
            tp1_dist = entry * float(getattr(config, "tp1_target_pct", 0.03))
            max_sl_dist = tp1_dist / max(float(getattr(config, "min_risk_reward_ratio", 1.5)), 0.1)
            min_allowed = round(entry - max_sl_dist, precision)
            if stop < min_allowed:
                stop = min_allowed
            floor_sl = round(entry * (1.0 - float(getattr(config, "min_stop_loss_pct", 0.018))), precision)
            if stop > floor_sl:
                stop = floor_sl
            if stop >= entry:
                stop = round(entry * 0.999, precision)
            return stop
        except Exception:
            return float(stop_price or 0.0)

    @staticmethod
    def _resolve_risk_anchor(coin) -> tuple:
        """Risk per unit for the per-position loss cap: the frozen entry->stop anchors, with a
        FAIL-SAFE fallback to the config risk distance when the true protective stop cannot be
        resolved (missing, or >= entry). Never returns a near-zero distance (which would turn a
        real loss into a huge R) and never skips the cap. All thresholds config-driven."""
        ra_e = float(getattr(coin, "r_anchor_entry", 0.0) or 0.0) or float(getattr(coin, "entry_price", 0.0) or 0.0)
        ra_s = float(getattr(coin, "r_anchor_stop", 0.0) or 0.0) or float(getattr(getattr(coin, "grid_bot", None), "stop_loss", 0.0) or 0.0)
        if not (ra_e > 0 and 0 < ra_s < ra_e):
            _fb_pct = float(getattr(config, "position_loss_fallback_pct", 0.05) or 0.0)
            ra_s = ra_e * (1.0 - _fb_pct) if (ra_e > 0 and _fb_pct > 0) else 0.0
        return ra_e, ra_s, (ra_e - ra_s)

    def _init_basket_coin(self, symbol: str, investment: float, session_id: int,
                          is_live_execution: bool, trader: Optional[BinanceFuturesTrader]) -> Optional[BasketCoinState]:
        """Initializes grid bot, market settings, ATR bounds, and places initial limit orders for one coin."""
        clean_sym = normalize_symbol_dynamic(symbol, default_quote=self.quote_asset)
        quote_currency = clean_sym.split('/')[1].split(':')[0] if '/' in clean_sym else getattr(self, "quote_asset", "USDT")

        # AUDIT FIX: a LIVE open position must be adopted and managed, never entry-gated.
        # _init_basket_coin used to run all entry-quality gates (cooldown, news, triad,
        # quant, R:R, edge, junk bans) BEFORE the resume-adopt block, so a position that
        # was already open got "ENTRY DITOLAK" and dropped from the basket — unmanaged
        # (no TP1/trailing/self-heal). Detect the live position FIRST; when present, all
        # entry gates below degrade to log-only and the position flows to the adopt path.
        pre_has_position = False
        if is_live_execution and trader:
            try:
                _pre_pos = trader.get_open_positions(clean_sym)
                pre_has_position = bool(_pre_pos and abs(_pre_pos[0].get("contracts", 0)) > 0)
            except Exception:
                pre_has_position = False
        if pre_has_position:
            self.add_log(f"♻️ [{clean_sym} RESUME] Posisi live terdeteksi ({_pre_pos[0]['contracts']} kontrak). Gate entry dilewati — posisi diadopsi & dikelola.", "success")

        # FIX 1(a): JUNK-COIN BAN at the single entry chokepoint — before ANY order is placed.
        # Rejects malformed / non-ASCII / extreme-multiplier meme symbols outright.
        if not is_valid_symbol(clean_sym):
            self.add_log(f"🚫 [{clean_sym} JUNK BAN] Simbol tidak valid (non-ASCII / multiplier meme / malformed). Entry diblokir total.", "warning")
            return None

        # CHURN CAP (fresh entries only): hard-stop the re-scan churn that bleeds fees. Fee
        # audit showed high-entry days produced ~0.47x the gross edge of low-entry days. This
        # caps NEW entries per UTC day; an adopted position (pre_has_position) is exempt so an
        # already-open position is always managed. max_daily_entries=0 disables.
        if self._entry_blocked_by_churn_cap(pre_has_position):
            self.add_log(f"🛑 [{clean_sym} CHURN CAP] Batas entry baru harian tercapai ({self.entry_count_utc}/{self.max_daily_entries}) — mencegah overtrading yang menggerus fee. Koin dilewati.", "warning")
            return None

        # SELF-LEARNING MEMORY: Check quarantine cooldown for symbols with recent stop-loss
        if hasattr(self, 'memory') and self.memory.is_symbol_in_cooldown(clean_sym):
            if pre_has_position:
                self.add_log(f"♻️ [{clean_sym} RESUME] Karantina dilewati untuk posisi live (bukan entry baru).", "info")
            else:
                rem = self.memory.get_symbol_cooldown_remaining_minutes(clean_sym)
                self.add_log(f"🧠 [SELF-LEARNING MEMORY] {clean_sym} sedang dalam masa karantina evaluasi pasca-loss ({rem}m tersisa). Koin dilewati.", "warning")
                return None

        coin = BasketCoinState(
            symbol=clean_sym,
            investment=investment,
            grid_count=self.grid_count,
            leverage=self.leverage,
            custom_stop_loss=self.custom_stop_loss
        )

        snapshot = self.feeder.get_market_analysis_context(clean_sym, self.timeframe)
        if not snapshot:
            # AUDIT FIX: never fabricate an ATR (used to be curr_price*0.025) — a synthetic
            # volatility silently corrupts the whole grid geometry and the stop math.
            self.add_log(f"⚠️ [{clean_sym}] Snapshot pasar tidak tersedia (feed gagal). Koin dilewati demi menghindari grid berbasis ATR palsu.", "warning")
            return None
        else:
            curr_price = snapshot["current_price"]
            atr = snapshot["indicators"]["atr_14"]
            # AUDIT FIX: refuse to enter on a fabricated/unknown ATR. Downstream grid geometry,
            # TP distance and trailing all scale off ATR; a 0.0 means the indicator feed is broken
            # (or the source now reports 0.0 instead of inventing `price*0.015`). Skip the coin.
            if not (atr and float(atr) > 0):
                self.add_log(f"⚠️ [{clean_sym}] ATR tak tersedia (indikator rusak/NaN). Koin dilewati — tak menaruh order berbasis volatilitas palsu.", "warning")
                return None

        if curr_price <= 0:
            self.add_log(f"⚠️ Harga live Binance tidak valid ({curr_price}) untuk {clean_sym}. Melewati koin.", "warning")
            return None

        coin.current_price = curr_price

        # MULTI-TIMEFRAME CONFLUENCE (read-only): capture the timeframe view for the dashboard
        # from the snapshot already fetched above. No new network call, no order-path impact.
        _mtf = self._mtf_payload(snapshot, self.timeframe)
        coin.mtf_1h = _mtf["mtf_1h"]
        coin.mtf_primary = _mtf["mtf_primary"]

        # FOREX FACTORY MACRO NEWS GUARD + POST-NEWS VOLATILITY GATE
        news_guard = self.news_sentinel.check_news_guard()
        if news_guard.get("is_danger"):
            if pre_has_position:
                self.add_log(f"♻️ [{clean_sym} RESUME] Pause berita dilewati untuk posisi live (tetap dikelola).", "info")
            else:
                # Volatility gate: the biggest spikes come in the minutes AFTER a release.
                # Pre-release -> always block. Post-release -> stay blocked until measured
                # volatility settles (ATR ratio back under threshold) OR the calm window
                # elapses, so a 15-min timer can no longer re-open entries into live chop.
                settled, vol_reason = self._post_news_volatility_settled(snapshot, news_guard)
                if settled:
                    self.add_log(f"🟢 [{clean_sym} VOLATILITY CLEAR] {vol_reason}. Berita high-impact sudah reda, entry kembali diizinkan.", "success")
                else:
                    self.add_log(f"⚠️ [NEWS/VOLATILITY HOLD] {news_guard['caution_reason']} {vol_reason}. Menahan entry baru {clean_sym} — volatilitas belum reda.", "warning")
                    return None

        # DUAL-ENGINE HYBRID ARBITRATION (Quant Math Engine + 9router AI + Self-Learning Memory)
        ai_snap = dict(snapshot) if snapshot else {
            "symbol": clean_sym,
            "current_price": curr_price,
            "trend_bias": "NEUTRAL",
            "indicators": {"atr_14": atr, "rsi_14": 50.0}
        }
        ai_snap["news_guard"] = news_guard
        # AUDIT FIX: inject the LIVE taker fee (Binance VIP-tier aware) so the AI cost model and the
        # net-RR gate price the trade with the real venue fee rather than a static config guess.
        if self.trader:
            try:
                _lf = self.trader.get_trading_fee_rate(clean_sym)
                if _lf and _lf > 0:
                    ai_snap["fees"] = {"taker_fee_rate": float(_lf)}
            except Exception:
                pass
        coin.initial_snapshot = ai_snap
        decision = self.hybrid.arbitrate(ai_snap, coin.custom_stop_loss)
        coin.consensus_status = decision.status
        coin.quant_score = decision.quant_score
        coin.ai_confidence = decision.ai_confidence
        coin.consensus_explanation = decision.explanation

        self.add_log(f"⚡ [DUAL-ENGINE] {clean_sym} Konsensus: {decision.status} (Skor Quant: {decision.quant_score}% | AI: {int(decision.ai_confidence*100)}%)", "info")
        self.add_log(f"📋 [KEPUTUSAN SINERGI] {decision.explanation}", "success" if decision.status == "FULL_CONSENSUS" else "warning")

        # STRICT ENTRY QUALITY GATEKEEPER:
        # Menolak mentah-mentah entry jika AI/Quant mendeteksi pelemahan atau resiko tinggi.
        # Resume-adopted positions NEVER fail here: a live position is managed, not re-entered.
        if decision.action != "LONG":
            if pre_has_position:
                self.add_log(f"♻️ [{clean_sym} RESUME] Gate entry dilewati ({decision.status}) — posisi live tetap diadopsi & dikelola.", "info")
            else:
                self.add_log(f"🛑 [{clean_sym} ENTRY DITOLAK] Sistem Triad menolak entry: {decision.status}. Menghindari open trade beresiko tinggi demi menjaga modal.", "warning")
                return None

        # P0.1: HARD-REJECT SIDEWAYS / CONSOLIDATION
        # Jika trend_bias == "NEUTRAL" atau status mengandung "CONSOLIDATION", tolak mutlak
        _snap_tb = str(snapshot.get("trend_bias", "NEUTRAL")).upper() if snapshot else "NEUTRAL"
        _is_sideways = (
            _snap_tb == "NEUTRAL"
            or "NEUTRAL" in _snap_tb
            or "CONSOLIDATION" in _snap_tb
            or "CONSOLIDATION" in str(decision.status).upper()
        )
        if bool(getattr(config, "hard_reject_consolidation", True)) and _is_sideways:
            if pre_has_position:
                self.add_log(f"♻️ [{clean_sym} RESUME] Gate konsolidasi dilewati ({decision.status} | trend: {_snap_tb}) — posisi live tetap dikelola.", "info")
            else:
                self.add_log(f"🛑 [{clean_sym} ENTRY DITOLAK] Pasar sideways / konsolidasi (trend: {_snap_tb} | status: {decision.status}). Hard-reject aktif — dilarang entry buy limit saat sideways.", "warning")
                return None

        if decision.quant_score < config.min_quant_score:
            if pre_has_position:
                self.add_log(f"♻️ [{clean_sym} RESUME] Gate quant dilewati (skor {decision.quant_score}%) — posisi live tetap dikelola.", "info")
            else:
                self.add_log(f"⚠️ [{clean_sym} ENTRY DITOLAK] Skor Quant terlalu rendah ({decision.quant_score}% < {config.min_quant_score:.0f}%). Sinyal teknikal belum cukup kuat (rawan sideways/chop).", "warning")
                return None

        # ====================================================================
        # AUDIT 2026-10-06 — PRE-ENTRY GATE LAYER (strict, env-overridable)
        # Skip bila pre_has_position (posisi live = adopt, bukan entry baru).
        # ====================================================================
        if not pre_has_position and snapshot:
            _indic  = snapshot.get("indicators") or {}
            _ema20  = float(_indic.get("ema_20", curr_price) or curr_price)
            _volr   = float(_indic.get("volume_vs_average", 1.0) or 1.0)

            # GATE 1 — Reclaim Strength
            # LONG : (price - EMA20) / EMA20 >= MIN_RECLAIM_PCT
            # SHORT: (EMA20 - price) / EMA20 >= MIN_RECLAIM_PCT
            # HYPE-2026-10-06 audit: dist=+0.08% < 0.15% → ditolak
            _min_reclaim = float(getattr(config, "min_reclaim_pct", 0.0015))
            if _ema20 == 0:
                self.add_log(f"⚠️ [{clean_sym} GATE RECLAIM] EMA20 == 0 — indikator belum siap, entry dilewati.", "warning")
                return None
            if _ema20 > 0:
                if decision.action == "LONG":
                    _reclaim = (curr_price - _ema20) / _ema20
                else:
                    _reclaim = (_ema20 - curr_price) / _ema20
                if _reclaim < _min_reclaim:
                    self.add_log(
                        f"🛑 [{clean_sym} GATE RECLAIM] Jarak EMA20 {_reclaim*100:.3f}% < "
                        f"MIN_RECLAIM_PCT {_min_reclaim*100:.3f}% — terlalu tipis. Entry ditolak.",
                        "warning"
                    )
                    return None

            # GATE 2 — Volume Confirmation (P0.2: Hard-Gate Volume)
            # volRatio >= MIN_ENTRY_VOL_RATIO → lolos; di bawah → tolak mutlak jika hard_gate_volume aktif
            _min_vol   = float(getattr(config, "min_entry_vol_ratio", 0.8))
            _vol_pen   = float(getattr(config, "vol_score_penalty_pct", 8.0))
            _eff_score = float(decision.quant_score)
            if _volr < _min_vol:
                if bool(getattr(config, "hard_gate_volume", True)):
                    self.add_log(
                        f"🛑 [{clean_sym} GATE VOLUME] volRatio {_volr:.2f}x < "
                        f"MIN_ENTRY_VOL_RATIO {_min_vol:.2f}x — volume terlalu tipis di bawah threshold minimum. Entry ditolak mutlak.",
                        "warning"
                    )
                    return None
                else:
                    _eff_score -= _vol_pen
                    self.add_log(
                        f"⚠️ [{clean_sym} GATE VOLUME] volRatio {_volr:.2f}x < {_min_vol:.1f}x "
                        f"→ skor dipotong {_vol_pen:.0f}: {decision.quant_score:.0f}→{_eff_score:.0f}.",
                        "warning"
                    )
                    if _eff_score < float(getattr(config, "min_quant_score", 58.0)):
                        self.add_log(
                            f"🛑 [{clean_sym} GATE VOLUME] Skor efektif {_eff_score:.0f}% < min "
                            f"{config.min_quant_score:.0f}% setelah penalti volume. Entry ditolak.",
                            "warning"
                        )
                        return None

            # GATE 3 — AI Failover Strict
            # is_failover_active → threshold = min_quant_score + failover_quant_bump
            # Bila _eff_score < threshold → tolak
            if bool(getattr(decision, "is_failover_active", False)):
                _bump      = float(getattr(config, "failover_quant_bump", 10.0))
                _threshold = float(getattr(config, "min_quant_score", 60.0)) + _bump
                if _eff_score < _threshold:
                    self.add_log(
                        f"🛑 [{clean_sym} GATE FAILOVER] AI offline — threshold dinaikkan ke "
                        f"{_threshold:.0f}% ({config.min_quant_score:.0f}+{_bump:.0f}). "
                        f"Skor efektif {_eff_score:.0f}% tidak cukup. Entry ditolak.",
                        "warning"
                    )
                    return None
                self.add_log(
                    f"⚠️ [{clean_sym} FAILOVER ACTIVE] AI offline; quant {_eff_score:.0f}% >= "
                    f"{_threshold:.0f}% → lanjut hati-hati.",
                    "warning"
                )

        # P2: R:R ENFORCEMENT GATE — refuse entries whose reward does not clearly beat risk.
        # Without this the bot opened asymmetric trades (risk ~2.2% vs first reward ~0.9%).
        if config.enforce_min_rr:
            eff_rr = float(getattr(decision, "risk_reward_ratio", 0.0) or 0.0)
            if eff_rr < config.min_risk_reward_ratio:
                if pre_has_position:
                    self.add_log(f"♻️ [{clean_sym} RESUME] Gate R:R dilewati ({eff_rr:.2f}x) — posisi live tetap dikelola.", "info")
                else:
                    self.add_log(f"🛑 [{clean_sym} ENTRY DITOLAK] R:R {eff_rr:.2f}x < minimum {config.min_risk_reward_ratio}x. Reward tidak sepadan dengan risiko; menghindari trade expectancy negatif.", "warning")
                    return None

        # P6: REGIME EDGE GATE — the only statistically-validated entry filter (see
        # edge_gate.py: survives in-sample AND out-of-sample, 6/8 symbols, PF ~1.5).
        # Reject entries that are either (a) in a fully-confirmed BULL trend (the engine's
        # worst bucket, -0.10R) or (b) on dead volume (< 0.8x average), which whipsaws.
        if config.enforce_edge_gate and snapshot:
            trend_b = str(snapshot.get("trend_bias", "NEUTRAL")).upper()
            vol_r = float((snapshot.get("indicators") or {}).get("volume_vs_average", 1.0) or 1.0)
            if trend_b == "BULLISH" and not bool(getattr(config, "edge_allow_bullish_fallback", False)):
                if pre_has_position:
                    self.add_log(f"♻️ [{clean_sym} RESUME] EDGE GATE dilewati (BULLISH) — posisi live tetap dikelola.", "info")
                else:
                    self.add_log(f"🛑 [{clean_sym} EDGE GATE] Trend BULLISH penuh = bucket expectancy negatif (-0.10R historis). Entry ditolak.", "warning")
                    return None
            # NOTE: the volume filter is deliberately NOT applied here. Measured live (edge
            # x-bucket, n=555): NEUTRAL|surge expectancy = -0.238R while NEUTRAL|normal =
            # +0.287R, so "dead volume" is NOT the losing slice — the confirmed BULL trend is.
            # Rejecting low-volume NEUTRAL would discard the single best-performing bucket.

        # Optimized grid count & harmonized stop loss from consensus
        effective_grids = decision.grid_count if decision.grid_count > 0 else self.grid_count
        coin.grid_count = effective_grids

        # P3: Wire the memory-driven sizing multiplier into the LIVE path.
        # Previously computed by the quant engine (Kelly-ish, from historical pattern
        # expectancy) but never applied — every position got a flat allocation.
        sizing_mult = float(getattr(decision, "sizing_multiplier", 1.0) or 1.0)
        sizing_mult = max(0.5, min(1.5, sizing_mult))  # clamp: never over-leverage on memory
        if abs(sizing_mult - 1.0) > 1e-9:
            investment = round(investment * sizing_mult, 2)
            self.add_log(f"⚖️ [{clean_sym} RISK-SIZING] Memori/expectancy menskalakan alokasi {sizing_mult:.2f}x => ${investment:.2f}.", "info")

        # Pre-query market specifications from exchange if live
        market = {}
        min_amount = 0.0
        min_cost = 0.0
        free_margin = 0.0
        if is_live_execution and trader:
            try:
                if not trader.exchange.markets:
                    trader.exchange.load_markets()
                resolved = trader.resolve_symbol(clean_sym)
                if not resolved or resolved not in trader.exchange.markets:
                    self.add_log(f"⚠️ [{clean_sym}] Kontrak {resolved} tidak tersedia di Binance ({'Testnet' if trader.is_testnet else 'Live'}). Melewati koin ini.", "warning")
                    return None
                trader.setup_symbol_settings(clean_sym, self.leverage, config.margin_type)
                market = trader.exchange.market(resolved)
                min_amount = float(market.get('limits', {}).get('amount', {}).get('min') or 0.0)
                min_cost = float(market.get('limits', {}).get('cost', {}).get('min') or 0.0)

                # FIX 1(b): JUNK-COIN BAN — liquidity & listing-age gate on live markets.
                # A feed error on either check degrades to 'allow' (never blocks on a hiccup),
                # but a positive finding (thin volume / brand-new listing) blocks the entry.
                try:
                    tk = trader.exchange.fetch_ticker(resolved)
                    qv = float(tk.get('quoteVolume') or 0.0)
                    if qv > 0 and qv < float(config.ban_min_quote_volume) and not pre_has_position:
                        self.add_log(f"🚫 [{clean_sym} JUNK BAN] Volume 24j ${qv:,.0f} < minimum ${config.ban_min_quote_volume:,.0f}. Koin terlalu tipis (rawan spread/slippage). Entry diblokir.", "warning")
                        return None
                except Exception as e:
                    self.add_log(f"ℹ️ [{clean_sym}] Cek volume 24j tidak tersedia ({e}). Degradasi ke 'izinkan'.", "info")

                try:
                    onboard_ms = int((market.get('info') or {}).get('onboardDate') or 0)
                    if onboard_ms > 0:
                        age_days = (time.time() * 1000.0 - onboard_ms) / 86_400_000.0
                        if age_days < float(config.min_listing_age_days) and not pre_has_position:
                            self.add_log(f"🚫 [{clean_sym} JUNK BAN] Umur listing {age_days:.1f} hari < minimum {config.min_listing_age_days} hari. Koin baru tanpa rekam jejak. Entry diblokir.", "warning")
                            return None
                except Exception as e:
                    self.add_log(f"ℹ️ [{clean_sym}] Cek umur listing tidak tersedia ({e}). Degradasi ke 'izinkan'.", "info")

                bal = trader.get_balance(quote_currency)
                free_margin = float(bal.get('free', 0.0) or 0.0)
            except Exception as e:
                self.add_log(f"⚠️ [{clean_sym}] Setup pasar notice: {e}", "info")

        if market:
            effective_min_notional = BotWebManager._venue_min_notional(market, curr_price)
        else:
            effective_min_notional = float(config.min_notional_threshold)
        single_min_margin = effective_min_notional / max(1, self.leverage)

        # AUDIT FIX: sync grid price/qty precision from Binance exchangeInfo (tick/step)
        price_prec = market_price_precision(market) if market else None
        qty_prec = market_qty_precision(market) if market else None

        # Pre-check affordability against available balance to prevent Margin is Insufficient.
        # Skipped for an adopted live position: its margin is already committed, so the free
        # balance can legitimately sit below one order's minimum while the position is open.
        if is_live_execution and trader and free_margin > 0 and not pre_has_position:
            if free_margin < single_min_margin:
                self.add_log(f"⚠️ [{clean_sym}] Saldo Free (${free_margin:.2f} {quote_currency}) tidak mencukupi untuk 1 order minimal (${single_min_margin:.2f} {quote_currency}). Melewati koin ini.", "warning")
                return None

        # Initialize Futures Grid engine with exact Binance min notional & Quant-Memory Profit Expansion
        profit_mult = getattr(decision, "profit_multiplier", 1.0)
        grid_fee_rate = None
        if is_live_execution and trader:
            try:
                live_fee = trader.get_trading_fee_rate(clean_sym)
                if live_fee:
                    grid_fee_rate = live_fee * 2.0  # round-trip (entry + exit)
            except Exception:
                grid_fee_rate = None
        grid_bot = FuturesGridBot(
            symbol=clean_sym,
            current_price=curr_price,
            atr=atr,
            leverage=self.leverage,
            total_investment=investment,
            grid_count=effective_grids,
            min_notional=effective_min_notional,
            fee_rate=grid_fee_rate,
            price_precision=price_prec,
            qty_precision=qty_prec,
            profit_multiplier=profit_mult
        )
        # P2: Align the stop-loss with the grid's own structural floor.
        # The grid designs its SL BELOW the deepest buy order; overwriting it with a
        # tighter consensus SL silently discards those grid orders (averaging cut off).
        # We keep the LOWER (more distant) of the two so the grid stays functional.
        if decision.stop_loss and decision.stop_loss > 0:
            grid_bot.stop_loss = min(float(decision.stop_loss), grid_bot.stop_loss)
        elif coin.custom_stop_loss and coin.custom_stop_loss > 0:
            grid_bot.stop_loss = min(float(coin.custom_stop_loss), grid_bot.stop_loss)
        # FIX 1: SINGLE SOURCE OF TRUTH — run every clamp ONCE through the shared finalizer.
        # The resulting grid_bot.stop_loss is consumed verbatim by the risk-based SIZING, the
        # frozen r_anchor_stop, and the exchange STOP_MARKET, so the three can never diverge
        # (the SUI -3.68R drift: sized risk vs. recorded risk_dist vs. placed stop).
        try:
            _pre_final = grid_bot.stop_loss
            grid_bot.stop_loss = self._finalize_protective_stop(curr_price, grid_bot.stop_loss, grid_bot.precision)
            if grid_bot.stop_loss != _pre_final:
                self.add_log(f"⚖️ [{clean_sym} STOP FINALIZED] Stop ${_pre_final} -> ${grid_bot.stop_loss} (R:R clamp + min_stop_loss_pct {float(getattr(config, 'min_stop_loss_pct', 0.018))*100:.2f}% + <entry). Risiko tidak boleh melebihi reward.", "info")
        except Exception:
            pass

        coin.grid_bot = grid_bot
        coin.grid_summary = grid_bot.get_summary()
        coin.active_grids = grid_bot.active_grids

        # RISK-BASED POSITION SIZING (LIVE path; shared by demo+live, never forked by mode).
        # Dormant RiskManager concept, now actually wired: risk per trade is capped and the
        # position is DERIVED from the stop distance, so a wider stop => smaller size. The
        # stop used is the FINAL grid_bot.stop_loss (post R:R repair) — the same level the
        # protective order will use — so risk is anchored to the real risk, not a stale one.
        # Adopted positions are EXEMPT: an already-open position must always be managed.
        # DASHBOARD RISK POLICY (read-only, additive): ALWAYS expose the effective CAP the NEXT
        # fresh entry would risk, for fresh AND adopted coins alike — an adopted position (managed,
        # never re-sized) still shows the policy; the panel label then honestly flags "no entry".
        _base_risk = float(getattr(config, "risk_per_trade_pct", 0.0) or 0.0)
        _ai_risk = getattr(decision, "suggested_risk_pct", None) if not pre_has_position else None
        _risk_pct = self._clamp_risk_pct(_base_risk, _ai_risk)
        _equity = float(self.wallet_balance or 0.0) or float(investment or 0.0)
        # P2: risk fixed-dollar cap — risk_per_trade_pct (2%) but never more than risk_cap_usd.
        _risk_cap_usd = float(getattr(config, "risk_cap_usd", 0.0) or 0.0)
        if _risk_cap_usd > 0 and _equity > 0:
            _risk_pct = min(_risk_pct, _risk_cap_usd / _equity)
        coin.risk_pct = round(_risk_pct * 100, 2)
        coin.risk_usd = round(_equity * _risk_pct, 2)
        coin.risk_min_notional = round(float(effective_min_notional), 2)
        coin.risk_fee_pct = round(float(grid_bot.fee_rate or 0.0) * 100, 3)
        coin.risk_sizing_source = "NO_ENTRY" if pre_has_position else "QUANT"
        if not pre_has_position:
            if _equity > 0 and _risk_pct > 0:
                _risk_inv = self._risk_based_investment(_equity, _risk_pct, curr_price, grid_bot.stop_loss, self.leverage)
                # P1: POSITION SCALING — open at position_scaling_low (50%) of the risk size;
                # the pyramiding ladder deploys the rest on confirmation up to scaling_high (100%).
                _scale_low = float(getattr(config, "position_scaling_low", 0.50) or 1.0)
                _risk_inv = round(_risk_inv * _scale_low, 2)
                coin.pyramid_plan = BotWebManager._pyramid_plan(
                    self._risk_based_investment(_equity, _risk_pct, curr_price, grid_bot.stop_loss, self.leverage),
                    curr_price, atr,
                )
                if _risk_inv > 0 and _risk_inv < float(investment or 0.0):
                    investment = _risk_inv
                    grid_bot.total_investment = investment
                    grid_bot.notional_per_grid = (investment * grid_bot.leverage) / max(1, grid_bot.grid_count)
                    grid_bot.margin_per_grid = investment / max(1, grid_bot.grid_count)
                    grid_bot._initialize_grid_orders()
                    coin.investment = investment
                    coin.active_grids = grid_bot.active_grids
                    coin.grid_summary = grid_bot.get_summary()
                    self.add_log(f"📐 [{clean_sym} RISK SIZING] Risiko {_risk_pct*100:.2f}% (${coin.risk_usd:.2f}) / jarak stop {abs(curr_price-grid_bot.stop_loss):.6f} => alokasi ${investment:.2f} (margin ${grid_bot.margin_per_grid:.2f}). Stop lebih lebar = posisi lebih kecil.", "info")
                # MIN-NOTIONAL GUARD (flexible): a risk-derived grid below the venue floor
                # RAMPS grid_count DOWN so notional/grid >= venue min — never clamped UP
                # (that would breach the risk cap). Skipped only when even the minimal
                # viable grids (config.grid_ramp_min_grids) cannot meet the floor. Adopts
                # are exempt (handled by the pre_has_position gate). min_notional is
                # venue-true from exchangeInfo.
                if float(grid_bot.notional_per_grid or 0.0) < float(effective_min_notional or 0.0):
                    _ramped = BotWebManager._ramp_grids_for_min_notional(investment, self.leverage, grid_bot.grid_count, effective_min_notional)
                    _ramp_floor = max(2, int(getattr(config, "grid_ramp_min_grids", 2) or 2))
                    _ramped_notional = (float(investment or 0.0) * max(1, self.leverage)) / max(1, _ramped)
                    if _ramped >= _ramp_floor and _ramped_notional >= float(effective_min_notional or 0.0):
                        grid_bot.grid_count = _ramped
                        grid_bot.notional_per_grid = (investment * grid_bot.leverage) / max(1, grid_bot.grid_count)
                        grid_bot.margin_per_grid = investment / max(1, grid_bot.grid_count)
                        grid_bot._initialize_grid_orders()
                        coin.grid_count = _ramped
                        coin.investment = investment
                        coin.active_grids = grid_bot.active_grids
                        coin.grid_summary = grid_bot.get_summary()
                        coin.risk_sizing_source = "GRID_RAMP"
                        self.add_log(f"📉 [{clean_sym} GRID RAMP] Notional/grid di bawah minimum bursa — grid diramping ke {_ramped} (notional ${_ramped_notional:.2f} >= ${effective_min_notional:.2f}). Tanpa menaikkan notional.", "info")
                    else:
                        coin.risk_sizing_source = "MIN_NOTIONAL_GUARD"
                        self.add_log(f"⚠️ [{clean_sym} MIN-NOTIONAL GUARD] Notional risiko ${grid_bot.notional_per_grid:.2f} < minimum bursa ${effective_min_notional:.2f} bahkan pada grid minimal {_ramp_floor}. Entry DILEWATI demi menjaga batas risiko (tidak menaikkan notional).", "warning")
                        return None

        # (Adopted coin: fields above hold the POLICY cap with source "NO_ENTRY" — managed, not re-sized.)

        coin.entry_price = curr_price
        coin.highest_price = curr_price
        # P2: TP1 target anchored to config R:R ladder (fee-aware), not an arbitrary multiple
        coin.tp1_price = round(curr_price * (1.0 + config.tp1_target_pct), grid_bot.precision)
        # P7 FORWARD-TEST: freeze the R-denominator (entry -> initial stop) once, here.
        coin.r_anchor_entry = curr_price
        coin.r_anchor_stop = float(grid_bot.stop_loss or 0.0)

        # Deploy initial orders if live or demo mode
        if is_live_execution and trader:
            deployed = {}
            existing_pos = trader.get_open_positions(clean_sym)
            has_position = bool(existing_pos and abs(existing_pos[0].get("contracts", 0)) > 0)
            existing_orders = trader.get_open_orders(clean_sym)

            # SEAMLESS RESUME: adopt an already-open position and whatever orders exist.
            # AUDIT FIX: require only the POSITION, not limit orders. A position protected
            # by a single STOP_MARKET (no grid limits — the common case for a resumed/adopted
            # position) has zero entries in get_open_orders (which never returns conditional
            # orders), so the old `and len(existing_orders) > 0` guard sent it to the
            # fresh-start branch -> cancel_all_orders() -> cancel_conditional_orders() ->
            # the protective STOP was deleted, briefly leaving the position NAKED.
            if has_position:
                self.add_log(f"♻️ [{clean_sym} RESUME] Mengadopsi posisi aktif ({existing_pos[0]['contracts']} kontrak) dan {len(existing_orders)} open order dari Binance. Melanjutkan grid tanpa interupsi!", "success")
                coin.position_contracts = existing_pos[0]["contracts"]
                coin.unrealized_pnl = existing_pos[0]["unrealized_pnl"]
                self._sync_net_income_baseline(clean_sym, coin)  # zero point for the true close delta
                # Adopt the REAL entry price from Binance (not the current tick), so the TP1 target
                # and PnL are anchored to where we actually got in. Otherwise a resumed position
                # computes TP1 = current*1.03 and drifts from the true R:R ladder.
                real_entry = float(existing_pos[0].get("entry_price") or 0.0)
                if real_entry > 0:
                    coin.entry_price = real_entry
                    grid_bot.entry_price = real_entry
                    coin.tp1_price = round(real_entry * (1.0 + config.tp1_target_pct), grid_bot.precision)
                    coin.r_anchor_entry = real_entry
                for o in existing_orders:
                    side = (o.get("side") or "BUY").upper()
                    price = float(o.get("price") or 0.0)
                    qty = float(o.get("amount") or 0.0)
                    deployed[str(o["id"])] = {
                        "grid_price": price,
                        "side": side,
                        "quantity": qty,
                        "target_sell_price": round(price + grid_bot.grid_step, grid_bot.precision) if side == "BUY" else 0.0,
                        "target_buy_price": round(price - grid_bot.grid_step, grid_bot.precision) if side == "SELL" else 0.0,
                        "est_profit": grid_bot.grid_step * qty
                    }
                coin.deployed_orders = deployed
                if len(deployed) > 0:
                    self.add_log(f"✨ [{clean_sym}] {len(deployed)} Grid Orders Berhasil Di-Resume! Range: {coin.grid_summary['range']} | SL: ${grid_bot.stop_loss}", "success")
                # AUDIT FIX: adopt the EXISTING protective stop instead of overwriting it.
                # A resumed position already has a real exchange stop (possibly tighter and
                # fee-adjusted vs the freshly-computed grid SL). Adopting it — and marking it
                # synced — keeps the position protected exactly as designed and makes the
                # self-heal loop a no-op; overwriting it would silently re-price the risk on
                # every restart. If NO stop exists, fall through to _sync_hard_stop so a naked
                # adopted position still gets one placed immediately.
                adopted_stop = 0.0
                try:
                    for o in trader.get_conditional_orders(clean_sym):
                        if "STOP" in (o.get("orderType") or "").upper():
                            adopted_stop = float(o.get("triggerPrice") or o.get("stopPrice") or 0.0)
                            if adopted_stop > 0:
                                break
                except Exception as e:
                    logger.debug(f"Adopt stop fetch failed: {e}")
                    adopted_stop = 0.0
                if adopted_stop > 0:
                    # FIX 1(b): the adoption path MUST apply the same min_stop_loss_pct floor (and
                    # R:R guard) as a fresh entry. The raw exchange stop can sit far tighter than the
                    # sized risk (SUI: 0.27% vs the intended ~2%), which is exactly the drift that
                    # turned a -0.5R budget into a -3.68R live loss. finalize ONCE, then anchor.
                    adopted_stop = self._finalize_protective_stop(coin.entry_price, adopted_stop, grid_bot.precision)
                    grid_bot.stop_loss = adopted_stop
                    coin._hard_stop_synced = adopted_stop
                    coin.r_anchor_stop = adopted_stop  # keep the R-multiple / loss-cap anchored to the real stop
                    # TP1-ALREADY-FIRED inference: the exchange stop only rises ABOVE entry once BEP+
                    # (or the runner trailing stop) is active, and both are set exclusively after a
                    # TP1 cash-in. `tp1_filled` is in-memory only, so a restart mid-runner used to
                    # adopt tp1_filled=False and re-sell 50% of the runner the moment price sat above
                    # TP1 — silently cashing out ~75%. Detect it from ground truth instead.
                    if self._tp1_already_fired(coin.entry_price, adopted_stop):
                        coin.tp1_filled = True
                        coin.is_breakeven_active = True
                        self.add_log(f"🛡️ [{clean_sym} RESUME] Stop ${adopted_stop} > entry ${coin.entry_price} → TP1 sudah tercapai sebelumnya; runner dilanjutkan (tanpa cash-in ganda).", "info")
                    # Refresh the summary so the dashboard shows the REAL exchange stop,
                    # not the pre-adoption grid SL (which R:R repair may have repriced).
                    try:
                        coin.grid_summary = grid_bot.get_summary()
                    except Exception:
                        pass
                    self.add_log(f"🛡️ [{clean_sym} RESUME] Stop existing diadopsi @ ${adopted_stop} (tidak ditimpa). Posisi tetap terlindungi.", "success")
                else:
                    self._sync_hard_stop(clean_sym, coin, trader, reason="resume posisi (tanpa stop existing)")
                return coin

            # Fresh start: clean previous orders
            trader.cancel_all_orders(clean_sym)
            time.sleep(0.5)

            # Margin budgeting: calculate max orders that fit within free margin
            alloc_margin = min(free_margin * 0.85, investment) if free_margin > 0 else investment
            max_viable_orders = max(1, int(alloc_margin / max(0.1, single_min_margin)))

            buy_grids = [g for g in grid_bot.active_grids if g["side"] == "BUY"]

            # Standard Binance Long Grid: Initial Base Entry
            if not has_position:
                try:
                    base_qty = max(grid_bot.notional_per_grid / max(curr_price, 0.0001), min_amount, (min_cost * 1.05) / max(curr_price, 0.0001))
                    step = float(market.get('precision', {}).get('amount') or 0.0)
                    if step > 0 and (float(trader.exchange.amount_to_precision(trader.resolve_symbol(clean_sym), base_qty)) * curr_price) < min_cost:
                        import math
                        steps_needed = math.ceil(((min_cost * 1.02) / max(curr_price, 0.0001)) / max(step, 1e-8))
                        base_qty = steps_needed * step
                    if hasattr(trader.exchange, "amount_to_precision"):
                        base_qty = float(trader.exchange.amount_to_precision(trader.resolve_symbol(clean_sym), base_qty))
                    
                    # 1. SMART ASYMMETRIC ENTRY & REAL CONSENSUS TARGETS
                    ema_20 = float(snapshot.get("indicators", {}).get("ema_20", curr_price)) if snapshot and isinstance(snapshot.get("indicators"), dict) else curr_price
                    vol_ratio = float(snapshot.get("indicators", {}).get("volume_vs_average", 1.0)) if snapshot and isinstance(snapshot.get("indicators"), dict) else 1.0
                    atr_val = atr if atr > 0 else curr_price * 0.02
                    dist_to_ema20 = (curr_price - ema_20) / max(curr_price, 0.0001) if curr_price > 0 and ema_20 > 0 else 0.0
                    
                    # Extract Real Price Action confirmation
                    pa = snapshot.get("price_action", {}) if snapshot and isinstance(snapshot.get("price_action"), dict) else {}
                    is_candle_green = pa.get("is_candle_green", True)
                    is_falling_micro = pa.get("is_falling_micro", False)

                    # Dynamic Asymmetric TP Price derived from AI + Quant Math Consensus
                    consensus_tp = getattr(decision, "take_profit", 0.0)
                    if consensus_tp > curr_price:
                        tp_price = round(consensus_tp, grid_bot.precision)
                    else:
                        # Fallback to true Asymmetric Structural Target (at least 1.8x ATR, never just 1 tiny grid step)
                        tp_price = round(curr_price + max(1.8 * atr_val, curr_price * 0.018), grid_bot.precision)

                    coin.tp1_price = tp_price

                    # STRICT ANTI-PUCUK REAL PRICE ACTION CONFIRMATION:
                    # Require price to be tight against support (-0.8% to +1.2% from EMA20), candle is green, and not dumping micro.
                    # NEVER market buy when price is stretched > +1.2% above EMA20, even if volume is surging!
                    is_near_support = (-0.008 <= dist_to_ema20 <= 0.012)
                    can_market_buy = is_near_support and is_candle_green and (not is_falling_micro or vol_ratio >= 1.2)

                    if can_market_buy:
                        market_buy = trader.place_market_order(clean_sym, "BUY", base_qty)
                        if market_buy:
                            bought_qty = float(market_buy.get("amount") or market_buy.get("filled") or base_qty)
                            est_profit_val = (tp_price - curr_price) * bought_qty
                            # TP1 is SOFTWARE-managed: the monitor loop cashes out 50% at TP1 and rides
                            # the rest as a trailing runner (see the "AUTO TP1: Cash-in 50%" block).
                            # We deliberately do NOT place a resting limit SELL for the FULL position
                            # here: that used to dump 100% at TP1 (killing the runner) and race the
                            # software TP1 (double-sell) at the exact same price.
                            coin.tp1_price = tp_price
                            coin.entry_price = curr_price
                            grid_bot.entry_price = curr_price
                            rr_show = getattr(decision, "risk_reward_ratio", 2.0)
                            self.add_log(f"  🎯 [{clean_sym} SMART ENTRY @ SUPPORT] Konfirmasi Pantulan Sehat! Base Market BUY dieksekusi @ ${curr_price}. Target TP1 Asimetris: ${tp_price} (Target Cuan: +${est_profit_val:.4f} {quote_currency} | R:R {rr_show}x) - exit 50% di TP1 + runner 50% (trailing).", "tp")
                            coin.position_contracts = bought_qty
                            coin.unrealized_pnl = 0.0
                            self._sync_hard_stop(clean_sym, coin, trader, reason="entry")
                            # GATE 4 — Entry Decision Snapshot (audit trail, tidak rekonstruksi)
                            try:
                                from src.trade_ledger import ledger as _ledger
                                _snap_ind = (snapshot or {}).get("indicators") or {}
                                _ledger.append(
                                    "entry_decision",
                                    symbol=clean_sym,
                                    entry_price=curr_price,
                                    action=decision.action,
                                    quant_score=float(decision.quant_score),
                                    ai_confidence=float(decision.ai_confidence),
                                    ai_failover=bool(getattr(decision, "is_failover_active", False)),
                                    status=decision.status,
                                    risk_reward=float(getattr(decision, "risk_reward_ratio", 0.0) or 0.0),
                                    mtf_1h=str((coin.mtf_1h or {}).get("trend", "N/A")),
                                    ema20=float(_snap_ind.get("ema_20", 0.0) or 0.0),
                                    dist_ema20_pct=round(
                                        (curr_price - float(_snap_ind.get("ema_20", curr_price) or curr_price))
                                        / max(float(_snap_ind.get("ema_20", curr_price) or curr_price), 1e-9) * 100,
                                        4
                                    ),
                                    vol_ratio=float(_snap_ind.get("volume_vs_average", 1.0) or 1.0),
                                    explanation=str(decision.explanation)[:200],
                                )
                            except Exception as e:
                                self.add_log(f"⚠️ [{clean_sym}] Gate 4 snapshot gagal disimpan: {e}", "warning")
                        else:
                            err_msg = getattr(trader, 'last_error', '') or 'Order ditolak Binance (cek saldo/margin)'
                            self.add_log(f"  ❌ [{clean_sym}] Base Market BUY gagal: {err_msg}", "error")
                            return None
                    else:
                        # Price is falling micro or candle is red: NEVER market buy a falling knife!
                        # ANTI-PUCUK: Place Base Limit BUY at optimal support zone (EMA20 or pullback)
                        smart_entry_price = round(max(ema_20, curr_price - (0.5 * atr_val)), grid_bot.precision)
                        if smart_entry_price >= curr_price:
                            smart_entry_price = round(curr_price * (1.0 - config.pullback_entry_pct), grid_bot.precision)

                        smart_buy_order = trader.place_limit_order(clean_sym, "BUY", base_qty, smart_entry_price)
                        if smart_buy_order:
                            target_sell_price = round(max(smart_entry_price + (1.8 * atr_val), consensus_tp if consensus_tp > smart_entry_price else smart_entry_price * 1.02), grid_bot.precision)
                            deployed[smart_buy_order["id"]] = {
                                "grid_price": smart_entry_price,
                                "side": "BUY",
                                "quantity": smart_buy_order.get("amount", base_qty),
                                "target_sell_price": target_sell_price,
                                "is_base_entry": True
                            }
                            coin.entry_price = smart_entry_price
                            grid_bot.entry_price = smart_entry_price
                            self.add_log(f"  🎯 [{clean_sym} SMART PULLBACK ENTRY] Candle mikro sedang merah/turun. Menolak Market BUY agar tidak menangkap pisau jatuh! Memasang Base Limit BUY di area support ${smart_entry_price} (Target TP: ${target_sell_price}).", "order")
                        else:
                            err_msg = getattr(trader, 'last_error', '') or 'Order ditolak Binance (cek saldo/margin)'
                            self.add_log(f"  ❌ [{clean_sym}] Smart Base Limit BUY gagal: {err_msg}", "error")
                            return None
                except Exception as seed_err:
                    self.add_log(f"  ❌ [{clean_sym}] Base entry notice: {seed_err}", "error")
                    return None
            else:
                # Already has open contracts: place TP order for existing position without double buying
                try:
                    pos_info = existing_pos[0]
                    existing_qty = abs(pos_info["contracts"])
                    consensus_tp = getattr(decision, "take_profit", 0.0)
                    tp_price = round(consensus_tp if consensus_tp > curr_price else curr_price + max(1.8 * atr, curr_price * 0.018), grid_bot.precision)
                    # Software-managed TP1 (50% cash-in + BEP+ + trailing runner) owns the exit.
                    # No resting full-position limit SELL here: it would dump 100% at TP1 (no runner)
                    # and race the software TP1 at the same price.
                    coin.tp1_price = tp_price
                    self.add_log(f"  🎯 [{clean_sym} RESUME TP1] Posisi eksisting ({existing_qty} kontrak) diadopsi. Target TP1 (software: 50% cash-in + runner): ${tp_price} (Target: +${(tp_price - curr_price) * existing_qty:.4f} {quote_currency})", "tp")
                    coin.position_contracts = pos_info["contracts"]
                    coin.unrealized_pnl = pos_info["unrealized_pnl"]
                    self._sync_net_income_baseline(clean_sym, coin)  # zero point for the true close delta
                    real_entry = float(pos_info.get("entry_price") or 0.0)
                    if real_entry > 0:
                        coin.entry_price = real_entry
                        grid_bot.entry_price = real_entry
                        coin.tp1_price = round(real_entry * (1.0 + config.tp1_target_pct), grid_bot.precision)
                        coin.r_anchor_entry = real_entry
                    # Re-assert the hard stop too: the fresh-start path above just wiped all
                    # orders (incl. any exchange stop), so an adopted position would otherwise
                    # sit naked with only a TP. This closes that hole.
                    self._sync_hard_stop(clean_sym, coin, trader, reason="existing pos, no grid")
                except Exception as seed_err:
                    self.add_log(f"  [{clean_sym}] Existing TP notice: {seed_err}", "info")

            # Place resting limit buy orders strictly capped by remaining available margin
            remaining_slots = max(0, max_viable_orders - 1)
            viable_buy_grids = buy_grids[:remaining_slots]

            for bg in viable_buy_grids:
                if self.stop_requested or self.session_id != session_id:
                    break
                price = float(bg["price"])
                if price <= 0:
                    continue
                raw_qty = bg["quantity"]
                required_qty = max(raw_qty, min_amount, (min_cost * 1.05) / max(price, 0.0001))
                step = float(market.get('precision', {}).get('amount') or 0.0)
                if step > 0 and (float(trader.exchange.amount_to_precision(trader.resolve_symbol(clean_sym), required_qty)) * price) < min_cost:
                    import math
                    steps_needed = math.ceil(((min_cost * 1.02) / max(price, 0.0001)) / max(step, 1e-8))
                    required_qty = steps_needed * step
                order = trader.place_limit_order(clean_sym, "BUY", required_qty, price)
                if order:
                    deployed[order["id"]] = {
                        "grid_price": price,
                        "side": "BUY",
                        "quantity": order.get("amount", required_qty),
                        "target_sell_price": bg["target_sell_price"]
                    }
                    self.add_log(f"  [{clean_sym}] Limit BUY @ ${price} (Qty: {order.get('amount')})", "order")
                else:
                    err_msg = getattr(trader, 'last_error', '') or 'Order ditolak Binance (cek saldo/margin)'
                    self.add_log(f"  ❌ [{clean_sym}] Limit BUY @ ${price} ditolak Binance: {err_msg}", "error")

            coin.deployed_orders = deployed
            if len(deployed) > 0:
                self.add_log(f"✨ [{clean_sym}] {len(deployed)} Grid Orders Deployed! Range: {coin.grid_summary['range']} | SL: ${grid_bot.stop_loss}", "success")
            elif abs(coin.position_contracts) > 0:
                # Base entry filled but no resting grid orders were accepted: keep managing the live
                # position (software TP1 + hard stop) instead of discarding the coin.
                self.add_log(f"✨ [{clean_sym}] Posisi aktif tanpa grid order (mode TP1 + runner). SL: ${grid_bot.stop_loss}", "info")
            else:
                self.add_log(f"⚠️ [{clean_sym}] 0 Grid Orders Deployed! Seluruh order ditolak Binance. Cek saldo/margin di log error di atas.", "warning")
                return None
        else:
            coin.entry_price = curr_price
            grid_bot.entry_price = curr_price
            sim_contracts = round((investment * self.leverage / max(1, effective_grids)) / max(curr_price, 0.0001), grid_bot.precision if grid_bot else 4) if curr_price > 0 else 0.0
            coin.position_contracts = sim_contracts
            coin.unrealized_pnl = 0.0
            self.add_log(f"✨ [{clean_sym}] Grid Ready (Sim)! Range: {coin.grid_summary['range']} | SL: ${grid_bot.stop_loss} | Posisi: {sim_contracts} kontrak", "info")

        # Count this as a fresh entry for the daily churn cap (adopts are not new entries).
        if not pre_has_position:
            with self.lock:
                self.entry_count_utc += 1

        return coin

    def _replace_basket_coin(self, old_symbol: str, trader: Optional[BinanceFuturesTrader],
                             session_id: int, blacklist_symbols: set, inv_per_coin: float, is_live_execution: bool):
        """Cleanly closes old coin and dynamically introduces a new affordable asymmetric alpha coin to the basket."""
        quote_currency = old_symbol.split("/")[1].split(":")[0] if "/" in old_symbol else getattr(self, "quote_asset", config.default_quote_asset)
        self.add_log(f"💎 [AUTO-REPLACE ALPHA] Memindai koin Asimetris (Profit Maksimal, Risiko Minimal) untuk menggantikan {old_symbol}...", "info")
        blacklist_symbols.add(old_symbol)

        # Remove old coin from active basket
        with self.lock:
            if old_symbol in self.basket_coins:
                del self.basket_coins[old_symbol]

        # FOREX FACTORY MACRO NEWS GUARD
        news_guard = self.news_sentinel.check_news_guard()
        if news_guard.get("is_danger"):
            self.add_log(f"⚠️ [FOREX FACTORY RED NEWS PAUSE] {news_guard['caution_reason']}. Menunda penggantian koin baru untuk menghindari guncangan berita macro USD.", "warning")
            return

        if trader and not trader.exchange.markets:
            try:
                trader.exchange.load_markets()
            except Exception:
                pass

        # 1. Gather all candidates in order of priority: Asymmetric Alpha -> Volume -> Available Markets
        alphas = self._edge_aligned_scan(quote_currency, 35)
        current_active = set(self.basket_coins.keys())
        
        candidates_to_try = []
        if alphas:
            for b in alphas:
                sym = b["symbol"]
                if sym not in blacklist_symbols and sym not in current_active:
                    if hasattr(self, 'memory') and self.memory.is_symbol_in_cooldown(sym):
                        continue
                    candidates_to_try.append({
                        "symbol": sym,
                        "price": float(b.get("price", 0) or 0),
                        "meta": b
                    })

        # Fallback 1: High-Volume liquid tickers (>= $5,000,000 USDT)
        if len(candidates_to_try) < 10:
            try:
                dyn_pairs = self.feeder.exchange.fetch_tickers()
                sorted_vol = sorted(
                    [k.split(':')[0] for k, v in dyn_pairs.items() if (k.endswith(f'/{quote_currency}') or k.endswith(f'/{quote_currency}:{quote_currency}')) and is_valid_symbol(k.split(':')[0]) and float(v.get('last') or 0) > 0 and float(v.get('quoteVolume', 0) or 0) >= float(getattr(config, "ban_min_quote_volume", 50_000_000))],
                    key=lambda s: float(dyn_pairs.get(s, {}).get('quoteVolume', 0) or 0),
                    reverse=True
                )
                for sym in sorted_vol:
                    if sym not in blacklist_symbols and sym not in current_active and not any(c["symbol"] == sym for c in candidates_to_try):
                        if hasattr(self, 'memory') and self.memory.is_symbol_in_cooldown(sym):
                            continue
                        candidates_to_try.append({
                            "symbol": sym,
                            "price": float(dyn_pairs.get(sym, {}).get('last', 0) or 0),
                            "meta": None
                        })
            except Exception:
                pass

        # Fallback 2: Curated Liquid Major Symbols (Only from config / major tokens, never zero-volume micro-caps)
        if len(candidates_to_try) < 5:
            for s in config.trading_symbols:
                s_clean = s.split(':')[0]
                if not is_valid_symbol(s_clean):
                    continue
                if not s_clean.endswith(f"/{quote_currency}"):
                    s_clean = f"{s_clean.split('/')[0]}/{quote_currency}"
                if s_clean not in blacklist_symbols and s_clean not in current_active and not any(c["symbol"] == s_clean for c in candidates_to_try):
                    if hasattr(self, 'memory') and self.memory.is_symbol_in_cooldown(s_clean):
                        continue
                    candidates_to_try.append({"symbol": s_clean, "price": 0.0, "meta": None})

        # 2. Iterate candidates, checking affordability and successfully deploying orders
        successfully_deployed = False
        for item in candidates_to_try:
            if self.stop_requested or self.session_id != session_id:
                break
            cand_sym = item["symbol"]
            cand_price = item["price"]
            cand_meta = item["meta"]

            # Filter for contract availability on exchange
            if trader and trader.exchange.markets:
                resolved = trader.resolve_symbol(cand_sym)
                if not resolved or resolved not in trader.exchange.markets:
                    continue

            # Affordability Filter: Reject coins with oversized min lots (like AAVE on small balance)
            if not self._is_coin_affordable(cand_sym, cand_price, inv_per_coin, trader):
                continue

            if cand_meta:
                self.add_log(f"💎 [PILIHAN EDGE-ALIGNED] {cand_sym} terpilih! (trend: {cand_meta.get('trend_bias','?')} | Vol: {cand_meta.get('volume_ratio','?')}x | Vol USDT: {cand_meta.get('volume_usdt',0)})", "success")

            try:
                new_coin = self._init_basket_coin(cand_sym, inv_per_coin, session_id, is_live_execution, trader)
                if new_coin and (not is_live_execution or len(new_coin.deployed_orders) > 0 or abs(new_coin.position_contracts) > 0):
                    with self.lock:
                        self.basket_coins[cand_sym] = new_coin
                        self.symbol = list(self.basket_coins.keys())[0]
                    self.add_log(f"🚀 [BASKET NEW MEMBER AKTIF] Koin baru berhasil aktif di Basket: {cand_sym} (${inv_per_coin:.2f} {quote_currency}, {len(new_coin.deployed_orders)} orders)", "success")
                    self.broadcast_event({"event": "STATE_UPDATE", "data": self.get_state()})
                    successfully_deployed = True
                    break
                else:
                    blacklist_symbols.add(cand_sym)
                    self.add_log(f"⚠️ [{cand_sym}] Gagal deploy order/posisi. Mencoba alternatif koin berikutnya...", "warning")
            except Exception as cand_err:
                blacklist_symbols.add(cand_sym)
                logger.warning(f"Error initializing candidate {cand_sym}: {cand_err}")
                self.add_log(f"⚠️ [{cand_sym}] Gagal deploy order/posisi ({cand_err}). Mencoba alternatif koin berikutnya...", "warning")

        if not successfully_deployed:
            self.add_log(f"⚠️ [BASKET NOTICE] Tidak ada koin alternatif yang berhasil dideploy saat ini.", "warning")

    def _run_loop(self, session_id: int, auto_pick: bool):
        current_thread_id = threading.get_ident()
        with self.lock:
            if self.session_id != session_id:
                return
            self.active_thread_id = current_thread_id
            self.is_running = True

        blacklist_symbols = set()
        _markets_refresh_ttl = float(os.environ.get("MARKETS_REFRESH_TTL", "3600"))
        _last_markets_refresh = 0.0

        # AUDIT FIX: a single empty scan must NOT kill the bot. _execute_grid returns
        # after one scan cycle; previously `while ...: _execute_grid(); break` halted the
        # process the moment that one cycle found no enterable coin (e.g. a momentary
        # all-BULLISH pump the edge gate correctly rejects), leaving the dashboard stuck
        # at is_running=False until a human pressed Start again. Instead, keep the session
        # alive and re-scan on a cadence (config.retry_scan_seconds, default 60s) until a
        # basket is built or the user stops the bot.
        retry_scan_seconds = float(getattr(config, "retry_scan_seconds", 60))
        while not self.stop_requested and self.session_id == session_id:
            if self._kill_switch_active():
                self.add_log(f"🚨 [KILL SWITCH] File '{getattr(config, 'kill_switch_file', 'KILL_SWITCH')}' terdeteksi — bot dihentikan. Hapus file itu untuk mengaktifkan kembali.", "error")
                with self.lock:
                    self.stop_requested = True
                    self.is_running = False
                break
            try:
                _now = time.time()
                if _now - _last_markets_refresh >= _markets_refresh_ttl:
                    try:
                        trader = getattr(self, "trader", None)
                        if trader and hasattr(trader, "exchange"):
                            trader.exchange.load_markets(reload=True)
                            self.add_log(f"🔄 [MARKETS REFRESH] load_markets(reload=True) selesai (TTL={int(_markets_refresh_ttl)}s).", "info")
                    except Exception as _me:
                        self.add_log(f"⚠️ [MARKETS REFRESH] Gagal: {_me}", "warning")
                    _last_markets_refresh = time.time()
                self._execute_grid(session_id, auto_pick, blacklist_symbols)
            except Exception as e:
                import traceback
                self.add_log(f"❌ Error in bot loop: {e}", "error")
                logger.error(f"Error in bot loop: {e}\n{traceback.format_exc()}")
            # _execute_grid blocks for the lifetime of a live session (its internal polling
            # loop). If it returned, no basket was built and the bot would have stopped --
            # so back off and try again instead of exiting.
            if self.stop_requested or self.session_id != session_id:
                break
            self.add_log(f"🔄 [SCAN ULANG] Belum ada koin edge-aligned yang bisa di-entry saat ini. Memindai ulang dalam {int(retry_scan_seconds)}s (bot tetap aktif)...", "info")
            for _ in range(int(max(1, retry_scan_seconds))):
                if self.stop_requested or self.session_id != session_id:
                    break
                time.sleep(1)
            # Clear the per-cycle blacklist so a coin rejected only by a transient gate this
            # round can be reconsidered next cycle (cooldowns re-apply from memory each pass).
            blacklist_symbols.clear()
        
        # Cleanup when bot finally halts
        if self.session_id == session_id:
            # Snapshot each coin's intended stop BEFORE clearing state: cancel_all_orders also
            # removes conditional stops, so a halt that leaves an OPEN position (kill switch,
            # circuit breaker) must re-arm its STOP_MARKET or it goes NAKED on the exchange.
            protect_map = {}
            for _s, _c in self.basket_coins.items():
                _sl = float(getattr(getattr(_c, "grid_bot", None), "stop_loss", 0.0) or 0.0)
                if _sl > 0:
                    protect_map[_s] = _sl
            with self.lock:
                self.is_running = False
                self.deployed_orders = {}
                for c in self.basket_coins.values():
                    c.deployed_orders = {}
            is_live_execution = self.mode in ["demo", "live"]
            if is_live_execution and self.trader:
                for sym in list(self.basket_coins.keys()) + ([self.symbol] if self.symbol else []):
                    try:
                        self.trader.cancel_all_orders(sym)
                    except Exception:
                        pass
                # SAFETY: cancel_all_orders wiped the conditional stops too. Re-arm a protective
                # STOP_MARKET for every position STILL open (halt != close) so nothing is naked.
                try:
                    for pos in self.trader.get_open_positions():
                        psym = pos.get("symbol")
                        if psym and abs(float(pos.get("contracts", 0) or 0)) > 0:
                            sl = float(protect_map.get(psym, 0.0) or 0.0)
                            if sl > 0:
                                self.trader.set_protective_stop(psym, sl, position_side=pos.get("side", "LONG"))
                                self.add_log(f"🛡️ [{psym}] Posisi masih terbuka saat halt — STOP pelindung ${sl} di-arm ulang (tidak telanjang).", "info")
                except Exception as _pe:
                    logger.error(f"[halt] re-protect notice: {_pe}")
                self.add_log("Semua active basket orders cancelled cleanly on Binance.", "info")
            self.broadcast_event({"event": "STATE_UPDATE", "data": self.get_state()})

    def _execute_grid(self, session_id: int, auto_pick: bool, blacklist_symbols: Optional[set] = None):
        self.add_log(f"🚀 Initializing AI Futures Grid Basket [{self.mode.upper()} MODE]...", "info")
        if blacklist_symbols is None:
            blacklist_symbols = set()
        if hasattr(self, 'memory') and hasattr(self.memory, 'symbol_cooldowns'):
            now_ts = time.time()
            for s_cool, expiry in list(self.memory.symbol_cooldowns.items()):
                if expiry > now_ts:
                    blacklist_symbols.add(s_cool)

        is_live_execution = self.mode in ["demo", "live"]
        trader: Optional[BinanceFuturesTrader] = None

        if is_live_execution:
            is_testnet = (self.mode == "demo")
            if not self.trader or getattr(self.trader, 'is_testnet', None) != is_testnet:
                self.trader = BinanceFuturesTrader(is_testnet=is_testnet)
            trader = self.trader
            target_quote = getattr(self, "quote_asset", "USDT")
            bal = trader.get_balance(asset=target_quote, force=True)
            with self.lock:
                self.wallet_balance = bal.get("total", 0.0)
                self.available_balance = bal.get("free", 0.0)
            self.add_log(f"Connected to Binance! Balance: ${self.wallet_balance:.2f} {target_quote} (Free: ${self.available_balance:.2f})", "success")

            try:
                rp = trader.get_realized_pnl()
                with self.lock:
                    self.realized_profit = rp
            except Exception:
                pass

            # Check if there are ALREADY active open positions on Binance to automatically resume!
            existing_pos = trader.get_open_positions() if trader else []
            active_pos_symbols = [p["symbol"] for p in existing_pos if abs(p.get("contracts", 0)) > 0]
            has_active_positions = len(active_pos_symbols) > 0

            # Automatic Stale Margin Recovery: If open orders exist on a symbol with ZERO active position (e.g. TP hit), cancel them to free margin!
            try:
                all_open_orders = trader.get_open_orders()
                order_symbols = {o["symbol"].split(":")[0] for o in all_open_orders}
                stale_symbols = order_symbols - set(active_pos_symbols)
                if stale_symbols:
                    freed_count = 0
                    for s_sym in stale_symbols:
                        freed_count += trader.cancel_all_orders(s_sym)
                    if freed_count > 0:
                        self.add_log(f"🧹 [MARGIN RECOVERY] Menghapus {freed_count} sisa order lama pada {list(stale_symbols)} (posisi sudah selesai/TP) untuk memulihkan margin.", "info")
                        bal = trader.get_balance(asset=target_quote, force=True)
                        with self.lock:
                            self.wallet_balance = bal.get("total", self.wallet_balance)
                            self.available_balance = bal.get("free", self.available_balance)
            except Exception:
                pass

            # Balance guard: Only block if NO active positions to resume AND balance is genuinely exhausted
            if not has_active_positions and self.available_balance < 3.0 and self.wallet_balance < 5.0:
                self.add_log(f"🛑 [SALDO TIDAK CUKUP] Saldo Free Binance Anda saat ini: ${self.available_balance:.2f} {target_quote} (Total: ${self.wallet_balance:.2f}).", "error")
                if self.mode == "live":
                    other = trader.check_other_wallets(asset=target_quote) if trader else {}
                    if other.get("earn", 0) > 0:
                        self.add_log(f"💡 Terdeteksi saldo {other['earn']:.2f} {target_quote} di dompet Binance Earn (Simple Earn Flexible)!", "info")
                        self.add_log(f"👉 Cara pindahkan: Buka Binance App -> Wallets -> Earn -> Pilih {target_quote} -> Klik Redeem (Tebus) ke Spot -> Lalu Transfer dari Spot ke USD-M Futures.", "warning")
                    elif other.get("spot", 0) > 0:
                        self.add_log(f"💡 Terdeteksi saldo {other['spot']:.2f} {target_quote} di dompet Binance Spot!", "info")
                        self.add_log(f"👉 Cara pindahkan: Buka Binance App -> Wallets -> Futures -> Klik Transfer dari Spot ke USD-M Futures.", "warning")
                    else:
                        self.add_log(f"💡 PENTING: Saldo Futures LIVE Anda masih $0.00 {target_quote}. Silakan buka aplikasi Binance -> Wallet -> Futures -> Transfer {target_quote} dari Spot ke USD-M Futures terlebih dahulu.", "warning")
                else:
                    self.add_log(f"💡 Silakan isi saldo {target_quote} pada akun Demo Testnet Futures Anda.", "warning")
                with self.lock:
                    self.is_running = False
                    self.investment = 0.0
                    self.basket_coins.clear()
                    self.deployed_orders.clear()
                self.broadcast_event({"event": "STATE_UPDATE", "data": self.get_state()})
                return

            usable_wallet = round(self.available_balance * float(getattr(config, "margin_usage_pct", 0.85)), 2)
            if self.investment <= 0 or self.investment > usable_wallet:
                if has_active_positions and usable_wallet < 3.0:
                    with self.lock:
                        self.investment = round(self.wallet_balance, 2)
                    self.add_log(f"♻️ [RESUME] Melanjutkan posisi aktif dengan total modal: ${self.investment:.2f} {target_quote} (Free: ${self.available_balance:.2f})", "info")
                else:
                    with self.lock:
                        self.investment = usable_wallet
                    self.add_log(f"💰 Margin trading disinkronkan 100% dari saldo Binance: ${self.investment:.2f} {target_quote} (Free: ${self.available_balance:.2f})", "info")
            else:
                self.add_log(f"💰 Margin trading diset: ${self.investment:.2f} {target_quote} (dari Free: ${self.available_balance:.2f})", "info")
        else:
            with self.lock:
                if self.wallet_balance is None or self.wallet_balance <= 0 or self.wallet_balance < 50.0:
                    self.wallet_balance = config.paper_initial_balance
                    self.available_balance = config.paper_initial_balance
                if self.investment <= 0:
                    self.investment = config.default_investment
            self.add_log(f"🎮 Initializing Simulation Mode. Saldo Virtual: ${self.wallet_balance:,.2f} {self.quote_asset}", "info")

        # Determine target basket coins
        target_symbols: List[str] = []
        target_basket_size = self.basket_size
        is_resume_mode = False

        # MANUAL_SYMBOLS (2026-10-15): operator-managed positions (e.g. XAU ladder
        # entries via entry_ladder.py) are EXCLUDED from auto-adopt. The bot's
        # trailing would otherwise close them before the operator's sell-ladder
        # rungs fire. Their exchange stop (armed by entry_ladder.py) protects them.
        _manual_syms = set()
        try:
            _ms_raw = os.getenv("MANUAL_SYMBOLS", "")
            _manual_syms = {s.strip().upper() for s in _ms_raw.split(",") if s.strip()}
        except Exception:
            _manual_syms = set()
        if _manual_syms:
            self.add_log(f"🛠️ [MANUAL SYMBOLS] {sorted(_manual_syms)} dikelola operator — bot tidak akan adopt/mengelola posisi ini.", "info")

        if is_live_execution and active_pos_symbols:
            # Manage EVERY open position. Previously this sliced to [:basket_size], which could
            # silently drop live positions (leaving them unmanaged/naked). Bump the basket size so
            # the resume set is never treated as "incomplete" and re-filled with freshly-scanned
            # coins -- that was the "bot keeps hunting new coins instead of resuming" complaint.
            target_symbols = [s for s in active_pos_symbols
                              if s not in blacklist_symbols
                              and s.split(":")[0].upper() not in _manual_syms]
            # Lock the basket to EXACTLY the resumed positions (no empty slots to hunt for):
            # a resumed bot must manage what it already holds, not scan for new coins.
            if target_symbols:
                target_basket_size = len(target_symbols)
            is_resume_mode = True
            self.add_log(f"🔄 [RESUME OTOMATIS] Terdeteksi posisi aktif di Binance: {target_symbols}. Melanjutkan pengelolaan posisi eksisting tanpa interupsi!", "success")

        # Determine quote currency dynamically (USDT or USDC)
        quote_currency = getattr(self, "quote_asset", config.default_quote_asset)
        if self.symbol and "/" in self.symbol:
            quote_currency = self.symbol.split("/")[1].split(":")[0]
        elif target_symbols and "/" in target_symbols[0]:
            quote_currency = target_symbols[0].split("/")[1].split(":")[0]

        # FOREX FACTORY RED NEWS STANDBY GUARD:
        # If there are NO active positions running and high-impact Red News danger window is active,
        # put bot in graceful STANDBY mode rather than halting or picking pre-news risky coins.
        if not target_symbols:
            ng = self.news_sentinel.check_news_guard()
            last_reported_m = -999.0
            while ng.get("is_danger") and not self.stop_requested and self.session_id == session_id:
                rem_m = float(ng.get("minutes_to_release", 0) or 0)
                reason = ng.get("caution_reason", "High-Impact Red News")
                res_time = ng.get("resume_time_str", "")
                mins_left = float(ng.get("minutes_until_resume", 0) or 0)
                timing_str = f"(target resume: {res_time}, ~{mins_left:.0f} mnt lagi)" if res_time else f"(~{rem_m:.0f} mnt lagi)"
                if abs(rem_m - last_reported_m) >= 2.0 or last_reported_m < -900:
                    self.add_log(f"⏳ [STANDBY SIAGA BERITA] {reason} Bot standby siaga aktif dan akan OTOMATIS mulai trading saat volatilitas reda {timing_str}. Jangan di-stop, biarkan bot berjalan.", "warning")
                    last_reported_m = rem_m
                # Post-release only: release standby early once the MARKET-WIDE tape calms,
                # so the bot resumes the moment volatility settles instead of sleeping out the
                # full calm window. Pre-release stays locked (spike is still ahead).
                if ng.get("is_post_release"):
                    try:
                        probe = self.feeder.get_market_analysis_context(
                            getattr(config, "volatility_probe_symbol", "BTC/USDT"), self.timeframe)
                        settled, vol_reason = self._post_news_volatility_settled(probe, ng)
                        if settled:
                            self.add_log(f"🟢 [VOLATILITAS REDA] Pasar ({getattr(config, 'volatility_probe_symbol', 'BTC/USDT')}) tenang: {vol_reason}. Keluar dari standby lebih awal.", "success")
                            break
                    except Exception:
                        pass
                time.sleep(15)
                ng = self.news_sentinel.check_news_guard()
            if self.stop_requested or self.session_id != session_id:
                return
            if last_reported_m > -900:
                self.add_log("🟢 [RED NEWS BERLALU] Jeda berita high-impact selesai dan pasar mulai stabil. Bot otomatis melanjutkan pemindaian setup Alpha Asimetris!", "success")

        if len(target_symbols) < target_basket_size and (auto_pick or not self.symbol):
            needed = target_basket_size - len(target_symbols)
            est_inv = self.investment / max(1, target_basket_size)
            self.add_log(f"💎 [AUTO-PICK ASIMETRIS] AI & Quant memindai koin setup Asimetris (Profit Maksimal, Risiko Minimal) untuk mengisi {needed} koin ke Basket {target_basket_size}...", "info")
            alphas = self._edge_aligned_scan(quote_currency, 35)
            candidates = [b for b in alphas if b["symbol"] not in blacklist_symbols] if alphas else []
            
            if trader:
                if not trader.exchange.markets:
                    try:
                        trader.exchange.load_markets()
                    except Exception:
                        pass
                for b in candidates:
                    cand = trader.resolve_symbol(b["symbol"])
                    if cand in trader.exchange.markets and b["symbol"] not in target_symbols:
                        if not self._is_coin_affordable(b["symbol"], float(b.get("price", 0) or 0), est_inv, trader):
                            continue
                        target_symbols.append(b["symbol"])
                        self.add_log(f"💎 [PILIHAN EDGE-ALIGNED] {b['symbol']} terpilih! (trend: {b.get('trend_bias','?')} | Vol: {b.get('volume_ratio','?')}x | Vol USDT: {b.get('volume_usdt',0)})", "success")
                        if len(target_symbols) >= target_basket_size:
                            break
            elif candidates:
                for b in candidates:
                    if b["symbol"] not in target_symbols:
                        target_symbols.append(b["symbol"])
                        self.add_log(f"💎 [PILIHAN EDGE-ALIGNED] {b['symbol']} terpilih! (trend: {b.get('trend_bias','?')} | Vol: {b.get('volume_ratio','?')}x | Vol USDT: {b.get('volume_usdt',0)})", "success")
                        if len(target_symbols) >= target_basket_size:
                            break

            # If candidates are still fewer than target_basket_size, top up with edge-gate-aligned
            # coins (NOT the old bullish scanner, which hunted fully-confirmed bull trends the
            # Entry Edge Gate rejects — that filled the basket with never-enterable symbols).
            if len(target_symbols) < target_basket_size:
                edge_alts = self._edge_aligned_scan(quote_currency, 15)
                for b in (edge_alts or []):
                    sym_clean = b["symbol"]
                    if sym_clean not in target_symbols and sym_clean not in blacklist_symbols:
                        if not self._is_coin_affordable(sym_clean, float(b.get("price", 0) or 0), est_inv, trader):
                            continue
                        target_symbols.append(sym_clean)
                        self.add_log(f"📈 [PILIHAN EDGE-ALIGNED] {sym_clean} terpilih (trend: {b.get('trend_bias','?')} | Vol: {b.get('volume_ratio','?')}x | Skor: {b.get('asymmetric_score', 0)})", "info")
                        if len(target_symbols) >= target_basket_size:
                            break

            # Fallback 2 to configured trading symbols if API was unreachable
            if len(target_symbols) < target_basket_size:
                for s in config.trading_symbols:
                    if s not in target_symbols and s not in blacklist_symbols:
                        target_symbols.append(s)
                        if len(target_symbols) >= target_basket_size:
                            break
        else:
            # GUARD: only fall back to the manually-typed symbol when we have NO targets yet.
            # Previously this unconditionally did `target_symbols = [self.symbol]`. When the user
            # launched via auto-pick (symbol=None/''), a healthy resume target such as
            # ['XLM/USDT'] got clobbered into [''] -> "Harga live Binance tidak valid (0.0) untuk ."
            # and the bot re-scanned new coins instead of resuming the open position.
            if not target_symbols and self.symbol:
                target_symbols = [self.symbol]

        if not target_symbols:
            try:
                if trader and trader.exchange.markets:
                    mkts = [m.split(':')[0] for m in trader.exchange.markets.keys() if m.endswith(f'/{quote_currency}') or m.endswith(f'/{quote_currency}:{quote_currency}')]
                    target_symbols = mkts[:target_basket_size]
            except Exception:
                pass
        if not target_symbols:
            # No enterable candidate this cycle. Do NOT force majors (BTC/ETH/SOL): in an
            # uptrend they are BULLISH (gate-rejected) and in a downtrend BEARISH (the engine
            # forces HOLD), so the blind fallback only wasted a fetch + logged a rejection.
            # Leave the list empty and let _run_loop back off and re-scan.
            self.add_log("⏳ Tidak ada kandidat edge-aligned pada siklus ini. Bot tetap aktif; menunggu siklus pindai berikutnya.", "info")
            return

        # MANDATE: enforce the total-notional ceiling BEFORE any order is placed. Gross
        # exposure = Σ(margin_per_coin * leverage). The ceiling is the STRICTER of:
        #   - absolute: max_total_notional_usdt (USD)
        #   - balance-scaled: max_exposure_mult * wallet_balance (self-scales with the account)
        # Either 0 disables that leg. If the planned basket would exceed the ceiling, scale the
        # whole allocation down so Σ notional == ceiling.
        _cap = BotWebManager._effective_notional_cap(self.wallet_balance)
        _single_flex = bool(getattr(config, "single_coin_flexible", True)) and len(target_symbols) == 1
        if _single_flex and _cap > 0:
            self.add_log(f"🧺 [SINGLE-COIN FLEX] 1 koin: pakai 100% alokasi ${self.investment:.2f} tanpa split basket / scaling exposure-cap agresif.", "info")
        if _cap > 0 and not _single_flex:
            _planned = self.investment * max(1, self.leverage)
            if _planned > _cap and self.leverage > 0:
                _new_inv = round(_cap / max(1, self.leverage), 2)
                self.add_log(f"🧱 [MANDATE EXPOSURE CAP] Rencana notional ${_planned:.2f} ({self.investment:.2f}×{self.leverage}x) melewati batas ${_cap:.2f} (abs ${getattr(config,'max_total_notional_usdt',0):.2f}, {getattr(config,'max_exposure_mult',0)}x equity=${getattr(config,'max_exposure_mult',0)*float(self.wallet_balance or 0):.2f}). Alokasi diturunkan ke ${_new_inv:.2f}.", "warning")
                with self.lock:
                    self.investment = _new_inv

        # SINGLE-COIN FLEX: basket_size==1 skips the basket split — 100% of the flexible
        # investment goes to the single coin.
        inv_per_coin = self.investment if len(target_symbols) == 1 else self.investment / max(1, len(target_symbols))
        self.add_log(f"🧺 Portfolio Basket: {target_symbols} | Alokasi: ${inv_per_coin:.2f} per koin (Total: ${self.investment:.2f})", "success")

        # Initialize all basket coins (with auto-replenish fallback)
        basket_coins = {}
        for sym in list(target_symbols):
            if self.stop_requested or self.session_id != session_id:
                return
            try:
                c_state = self._init_basket_coin(sym, inv_per_coin, session_id, is_live_execution, trader)
                if c_state and (not is_live_execution or len(c_state.deployed_orders) > 0 or abs(c_state.position_contracts) > 0):
                    basket_coins[sym] = c_state
                else:
                    blacklist_symbols.add(sym)
            except Exception as e:
                self.add_log(f"⚠️ Notice inisialisasi {sym}: {e}", "warning")
                blacklist_symbols.add(sym)

        # Replenish basket if any coin was unavailable on current exchange/account
        if len(basket_coins) < target_basket_size and not is_resume_mode and trader and trader.exchange.markets:
            alphas_rep = self._edge_aligned_scan(quote_currency, 35)
            rep_cands = [b for b in (alphas_rep or []) if b["symbol"] not in basket_coins and b["symbol"] not in blacklist_symbols]
            for cand in rep_cands:
                if len(basket_coins) >= target_basket_size or self.stop_requested or self.session_id != session_id:
                    break
                fsym = cand["symbol"]
                if not self._is_coin_affordable(fsym, float(cand.get("price", 0) or 0), inv_per_coin, trader):
                    continue
                try:
                    fb_state = self._init_basket_coin(fsym, inv_per_coin, session_id, is_live_execution, trader)
                    if fb_state and (not is_live_execution or len(fb_state.deployed_orders) > 0 or abs(fb_state.position_contracts) > 0):
                        basket_coins[fsym] = fb_state
                    else:
                        blacklist_symbols.add(fsym)
                except Exception:
                    pass

        # Final fallback to any exchange market if basket is still unfilled
        if len(basket_coins) < target_basket_size and not is_resume_mode and trader and trader.exchange.markets:
            avail_syms = [m.split(':')[0] for m in trader.exchange.markets.keys() if (m.endswith(f'/{quote_currency}') or m.endswith(f'/{quote_currency}:{quote_currency}')) and m.split(':')[0] not in basket_coins and m.split(':')[0] not in blacklist_symbols]
            for fallback_sym in avail_syms:
                if len(basket_coins) >= target_basket_size or self.stop_requested or self.session_id != session_id:
                    break
                if not self._is_coin_affordable(fallback_sym, 0.0, inv_per_coin, trader):
                    continue
                try:
                    fb_state = self._init_basket_coin(fallback_sym, inv_per_coin, session_id, is_live_execution, trader)
                    if fb_state and (not is_live_execution or len(fb_state.deployed_orders) > 0 or abs(fb_state.position_contracts) > 0):
                        basket_coins[fallback_sym] = fb_state
                    else:
                        blacklist_symbols.add(fallback_sym)
                except Exception:
                    pass

        if not basket_coins:
            # No coin survived the entry gates this cycle. Keep the session ALIVE (do not set
            # is_running=False) so _run_loop re-scans on its cadence instead of the bot dying
            # and needing a manual Start.
            self.add_log("⏳ Tidak ada koin yang lolos Edge Gate + filter entry saat ini. Bot tetap aktif dan akan memindai ulang.", "warning")
            return

        with self.lock:
            self.basket_coins = basket_coins
            self.symbol = list(basket_coins.keys())[0]
            # Primary coin view for ladder
            primary = basket_coins[self.symbol]
            self.grid_summary = primary.grid_summary
            self.active_grids = primary.active_grids
            self.current_price = primary.current_price
            self.deployed_orders = primary.deployed_orders

        # Balance refresh after initial order deployment
        if is_live_execution and trader:
            bal = trader.get_balance(asset=quote_currency, force=True)
            with self.lock:
                self.wallet_balance = bal.get("total", self.wallet_balance)
                self.available_balance = bal.get("free", self.available_balance)
            self.broadcast_event({"event": "STATE_UPDATE", "data": self.get_state()})

        # Main active multi-coin polling loop
        tick_counter = 0
        # AUDIT FIX: drive slow cadences off wall-clock seconds so changing the loop
        # interval (config.loop_interval_seconds) keeps the monitor timing correct.
        loop_start_ts = time.time()
        _next_analysis_ts = loop_start_ts
        _next_stopcheck_ts = loop_start_ts
        while not self.stop_requested and self.session_id == session_id:
            if self._kill_switch_active():
                self.add_log(f"🚨 [KILL SWITCH] File '{getattr(config, 'kill_switch_file', 'KILL_SWITCH')}' terdeteksi — sesi live dihentikan & order grid dicabut. STOP pelindung posisi DIBIARKAN hidup. Hapus file untuk aktif kembali.", "error")
                with self.lock:
                    self.stop_requested = True
                    self.is_running = False
                break
            time.sleep(self.interval)
            if self.stop_requested or self.session_id != session_id:
                break
            tick_counter += 1

            # Pilar 1: Disiplin otomatis mengatur batas transaksi harian & batas kerugian harian
            discipline = self.check_daily_discipline_guardrail()
            if not discipline["allowed"] and self.daily_circuit_breaker_triggered:
                self.add_log(f"🛑 [DISIPLIN HARIAN AKTIF] {discipline['reason']}", "warning")
                self.add_log("🛡️ [PROTEKSI MODAL] Bot menghentikan order hari ini demi mengamankan modal tersisa.", "warning")
                with self.lock:
                    self.is_running = False
                    self.stop_requested = True
                break

            # Periodic Dual-Engine Analysis pulse (queries 9router + Quant cross-check every ~5 mins)
            _now_ts = time.time()
            if _now_ts >= _next_analysis_ts:
                _next_analysis_ts = _now_ts + float(getattr(config, "analysis_pulse_seconds", 300))
                for a_sym in list(self.basket_coins.keys()):
                    try:
                        snap = self.feeder.get_market_analysis_context(a_sym, self.timeframe)
                        if snap:
                            dec = self.hybrid.arbitrate(snap)
                            c_ref = self.basket_coins.get(a_sym)
                            if c_ref:
                                c_ref.consensus_status = dec.status
                                c_ref.quant_score = dec.quant_score
                                c_ref.ai_confidence = dec.ai_confidence
                                c_ref.consensus_explanation = dec.explanation
                                _mtf = self._mtf_payload(snap, self.timeframe)
                                c_ref.mtf_1h = _mtf["mtf_1h"]
                                c_ref.mtf_primary = _mtf["mtf_primary"]
                            self.add_log(f"⚡ [MONITOR KONSENSUS] {a_sym} ({dec.status}): Quant {dec.quant_score}% | AI {int(dec.ai_confidence*100)}% => {dec.explanation}", "info")
                            # If guardrail detected danger on an active coin, dynamically protect position
                            if dec.status in ["GUARDRAIL_OVERRIDE", "DEFENSIVE_GRID", "AI_CAUTION_OVERRIDE"]:
                                if c_ref and c_ref.grid_bot and dec.stop_loss > c_ref.grid_bot.stop_loss:
                                    c_ref.grid_bot.stop_loss = dec.stop_loss
                                    self.add_log(f"🛡️ [PROTEKSI DINAMIS] Stop-loss {a_sym} dinaikkan ke ${dec.stop_loss} untuk mengunci profit/menghindari pembalikan arah!", "warning")
                                    self._sync_hard_stop(a_sym, c_ref, trader, reason="proteksi dinamis")
                    except Exception:
                        pass

            # Iterate over each active coin in the basket independently
            for sym, coin in list(self.basket_coins.items()):
                if self.stop_requested or self.session_id != session_id:
                    break

                live_price = self.feeder.fetch_current_price(sym)
                if live_price <= 0:
                    continue
                coin.current_price = live_price

                # EXTERNAL-CLOSE RECONCILE (2026-10-15): a position closed outside the
                # bot (manual sell, exchange trailing stop, liquidation) leaves this coin's
                # state stale — active_grids keeps a ghost ladder on the dashboard while the
                # exchange holds nothing. Re-fetch the real position each tick; when state
                # says open but exchange says flat, record the outcome and drop the coin.
                if is_live_execution and trader and abs(float(coin.position_contracts or 0.0)) > 0:
                    try:
                        _real_pos = trader.get_open_positions(sym)
                        _real_ct = abs(float(_real_pos[0].get("contracts", 0) or 0)) if _real_pos else 0.0
                        if _real_ct <= 0:
                            _cl = self._realized_since_baseline(sym, coin, 0.0)
                            self.add_log(f"🧹 [{sym} REKONSILIASI] Posisi tidak lagi ada di exchange (ditutup eksternal/manual). State grid dibersihkan. Realized: {_cl:+.4f}.", "info")
                            self._record_memory_outcome(sym, coin, _cl, "EXTERNAL_CLOSE", live_price)
                            coin.deployed_orders.clear()
                            coin.active_grids = []
                            coin.grid_summary = {}
                            coin.position_contracts = 0.0
                            coin.unrealized_pnl = 0.0
                            if auto_pick:
                                self._replace_basket_coin(sym, trader, session_id, blacklist_symbols, inv_per_coin, is_live_execution)
                            else:
                                with self.lock:
                                    if sym in self.basket_coins:
                                        del self.basket_coins[sym]
                                if not self.basket_coins:
                                    with self.lock:
                                        self.is_running = False
                                        self.stop_requested = True
                                    return
                            continue
                    except Exception:
                        pass  # transient read failure: retried next tick

                # -------------------------------------------------------------
                # SMART PROFIT HARVESTER: Auto TP1 (+1.5%), BEP+, and Trailing Runner
                # -------------------------------------------------------------
                if coin.entry_price > 0 and abs(coin.position_contracts) > 0:
                    if coin.highest_price <= 0:
                        coin.highest_price = max(coin.entry_price, live_price)
                    else:
                        coin.highest_price = max(coin.highest_price, live_price)

                    # 1. AUTO TP1: Cash-in 50% & Lock BEP+ when reaching TP1 (config.tp1_target_pct, 3.0%)
                    tp1_target = getattr(coin, 'tp1_price', 0.0)
                    if not coin.tp1_filled and tp1_target > 0 and live_price >= tp1_target:
                        if is_live_execution and trader:
                            half_qty = abs(coin.position_contracts) * 0.5
                            try:
                                half_formatted = float(trader.exchange.amount_to_precision(trader.resolve_symbol(sym), half_qty))
                            except Exception:
                                half_formatted = half_qty
                            if half_formatted > 0:
                                res_tp1 = trader.place_market_order(sym, "SELL", half_formatted, reduce_only=True)
                                if res_tp1:
                                    coin.tp1_filled = True
                                    sq = sym.split('/')[1].split(':')[0] if '/' in sym else 'USDT'
                                    est_gain = (live_price - coin.entry_price) * half_formatted
                                    # NET of real round-trip fees (not gross estimate)
                                    net_gain = self._net_of_fee(est_gain, live_price * half_formatted, getattr(coin.grid_bot, "fee_rate", None))
                                    coin.realized_profit += net_gain
                                    coin.matched_trades += 1
                                    with self.lock:
                                        self.realized_profit += net_gain
                                        self.matched_trades += 1
                                    self.add_log(f"🎯 [{sym} SMART TP1 TERCAPAI!] 50% Posisi ditutup @ ${live_price} (Net +${net_gain:.4f} {sq} setelah fee)! Sisa kontrak dijadikan Runner.", "tp")
                                    self.record_daily_trade(net_gain)
                                    # Advance the income cursor past the TP1 cash-in so the runner's
                                    # close measures ONLY the runner (never double-counts the TP1 slice).
                                    self._sync_net_income_baseline(sym, coin)

                                    # Move Stop-Loss to Break-Even Plus (BEP+ +0.30% to cleanly cover all fees)
                                    bep_plus = round(coin.entry_price * (coin.grid_bot.bep_markup if coin.grid_bot else config.breakeven_markup()), coin.grid_bot.precision if coin.grid_bot else 4)
                                    if coin.grid_bot and bep_plus > coin.grid_bot.stop_loss:
                                        coin.grid_bot.stop_loss = bep_plus
                                    coin.is_breakeven_active = True
                                    self.add_log(f"🛡️ [{sym} BREAK-EVEN PLUS AKTIF] 50% profit telah diamankan! Stop-Loss runner dikunci di atas modal (${bep_plus}) => Posisi bebas resiko!", "success")
                                    self._sync_hard_stop(sym, coin, trader, reason="TP1 break-even+")
                                    self._audit_tp_rotation("TP1_HIT", sym, entry_price=float(coin.entry_price or 0.0), tp1_price=float(getattr(coin, "tp1_price", 0.0) or 0.0), live_price=float(live_price or 0.0), mode="live")
                                    try:
                                        self._maybe_tp_rotate(sym, coin, trader, live_price, True, session_id, blacklist_symbols, inv_per_coin, auto_pick)
                                    except Exception:
                                        pass
                        else:
                            # Simulation mode TP1
                            coin.tp1_filled = True
                            bep_plus = round(coin.entry_price * (coin.grid_bot.bep_markup if coin.grid_bot else config.breakeven_markup()), coin.grid_bot.precision if coin.grid_bot else 4)
                            if coin.grid_bot and bep_plus > coin.grid_bot.stop_loss:
                                coin.grid_bot.stop_loss = bep_plus
                            coin.is_breakeven_active = True
                            self.add_log(f"🎯 [{sym} SMART TP1 SIMULASI] 50% Posisi dikunci @ ${live_price}! SL dinaikkan ke modal+cuan (${bep_plus}).", "tp")
                            self._audit_tp_rotation("TP1_HIT", sym, entry_price=float(coin.entry_price or 0.0), tp1_price=float(getattr(coin, "tp1_price", 0.0) or 0.0), live_price=float(live_price or 0.0), mode="sim")
                            try:
                                self._maybe_tp_rotate(sym, coin, None, live_price, False, session_id, blacklist_symbols, inv_per_coin, auto_pick)
                            except Exception:
                                pass

                    # 2. DYNAMIC TRAILING TAKE PROFIT: Runner trailing the peak
                    # P1: trailing_stop_trigger_pct (0.3% profit) activates the trail; the stop
                    # then follows the peak by trailing_stop_delta_pct (0.15%). Config-driven.
                    _ts_trigger = float(getattr(config, "trailing_stop_trigger_pct", 0.003))
                    if live_price >= (coin.entry_price * (1.0 + _ts_trigger)):
                        coin.trailing_tp_active = True
                        _ts_delta = float(getattr(config, "trailing_stop_delta_pct", 0.0015))
                        atr_val = getattr(coin.grid_bot, 'atr', live_price * 0.02)
                        trail_dist = max(atr_val * 0.8, coin.highest_price * _ts_delta)
                        new_ts = round(coin.highest_price - trail_dist, coin.grid_bot.precision if coin.grid_bot else 4)
                        if coin.grid_bot and new_ts > coin.grid_bot.stop_loss:
                            coin.grid_bot.stop_loss = new_ts
                            coin.trailing_stop_price = new_ts
                            self.add_log(f"📈 [{sym} TRAILING STOP RUNNER] Puncak baru ${coin.highest_price:.4f}! Stop-Loss dinaikkan ke ${new_ts:.4f} untuk mengunci profit runner.", "tp")
                            self._sync_hard_stop(sym, coin, trader, reason="trailing")

                    # 2b. SELF-HEALING HARD STOP: keep a REAL stop bound to the live position.
                    #     Fresh exchange-check every ~5 min; re-assert the moment the bot's stop
                    #     drifted OR the binding stop vanished (manual cancel, restart, or a
                    #     position adopted without orders). Prevents a naked position on Binance.
                    if is_live_execution and trader and _now_ts >= _next_stopcheck_ts:
                        _next_stopcheck_ts = _now_ts + float(getattr(config, "hard_stop_recheck_seconds", 100))
                        sl_now = float(getattr(coin.grid_bot, "stop_loss", 0.0) or 0.0)
                        if sl_now > 0:
                            if abs(sl_now - float(getattr(coin, "_hard_stop_synced", 0.0) or 0.0)) >= 1e-9:
                                self._sync_hard_stop(sym, coin, trader, reason="self-heal (drift)")
                            else:
                                real_stops = [o for o in trader.get_conditional_orders(sym) if "STOP" in (o.get("orderType") or "").upper()]
                                if not real_stops:
                                    coin._hard_stop_synced = 0.0  # force re-place
                                    self._sync_hard_stop(sym, coin, trader, reason="self-heal (missing)")

                # -------------------------------------------------------------
                # PYRAMID AUTO-PLACE (P1): staged adds @0.5x/1.0xATR with the mandatory
                # re-seat-to-breakeven+ guard. Inert unless config.pyramiding_auto (OFF default).
                # -------------------------------------------------------------
                try:
                    self._pyramid_watch(sym, coin, trader, live_price, is_live_execution)
                except Exception as _py_e:
                    self.add_log(f"⚠️ [{sym} PYRAMID] watcher error: {_py_e}", "warning")

                # -------------------------------------------------------------
                # FIX 2: ENFORCED PER-POSITION LOSS CAP (R-BASED)
                # Evaluated EVERY tick before the other triggers. Uses the frozen
                # entry->stop anchors, so it also protects adopted/manual positions
                # whose coin.investment is wrong or zero. Risk per unit = ra_e - ra_s;
                # the position is cut when floating loss reaches cap_usdt.
                # -------------------------------------------------------------
                # FIX 1(b) FAIL-SAFE lives in the helper: an UNRESOLVABLE protective stop must
                # NOT lift the cap, and must never present a near-zero distance as a huge R.
                ra_e, ra_s, risk_per_unit = self._resolve_risk_anchor(coin)
                if ra_e > 0 and 0 < ra_s < ra_e and risk_per_unit > 0:
                    cap_usdt = risk_per_unit * abs(float(coin.position_contracts or 0.0)) * float(config.max_position_loss_r)
                    if cap_usdt > 0 and float(coin.unrealized_pnl or 0.0) <= -cap_usdt:
                        r_mult = abs(float(coin.unrealized_pnl or 0.0)) / cap_usdt * float(config.max_position_loss_r)
                        self.add_log(f"🛑 [{sym} LOSS CAP {config.max_position_loss_r}R TERLAMPAUI] Floating loss (-${abs(coin.unrealized_pnl):.2f}) menyentuh batas {r_mult:.2f}R (cap ${cap_usdt:.2f}: risk/unit ${risk_per_unit:.6f} x {abs(coin.position_contracts)} kontrak). Emergency Cut-Loss (anti-adopsi/manual)!", "cl")
                        cap_r_loss = float(coin.unrealized_pnl or 0.0)
                        if is_live_execution and trader:
                            trader.cancel_all_orders(sym)
                            res_close = trader.close_position(sym)
                            if res_close.get("closed"):
                                cap_r_loss = self._realized_since_baseline(sym, coin, cap_r_loss)
                                coin.realized_profit += cap_r_loss
                                with self.lock:
                                    self.realized_profit += cap_r_loss
                                sq_rc = sym.split('/')[1].split(':')[0] if '/' in sym else 'USDT'
                                self.add_log(f"  🛑 [{sym} LOSS CAP SELESAI] Posisi ditutup oleh R-based cap. Realized Loss: {cap_r_loss:+.4f} {sq_rc} (net fee).", "cl")
                                self.record_daily_trade(cap_r_loss, r_multiple=-abs(float(config.max_position_loss_r)))
                            coin.deployed_orders.clear()
                            coin.unrealized_pnl = 0.0
                            coin.position_contracts = 0.0
                        else:
                            coin.realized_profit += cap_r_loss
                            with self.lock:
                                self.realized_profit += cap_r_loss
                                self.wallet_balance += cap_r_loss
                                self.available_balance += (coin.investment + cap_r_loss)
                            sq_rc = sym.split('/')[1].split(':')[0] if '/' in sym else 'USDT'
                            self.add_log(f"  🛑 [{sym} LOSS CAP SIMULASI SELESAI] Posisi ditutup @ ${live_price}. Realized Loss: ${cap_r_loss:+.4f} {sq_rc}.", "cl")
                            self.record_daily_trade(cap_r_loss)
                            coin.deployed_orders.clear()
                            coin.unrealized_pnl = 0.0
                            coin.position_contracts = 0.0

                        # SELF-LEARNING MEMORY RECORDING
                        self._record_memory_outcome(sym, coin, cap_r_loss, "STOP_LOSS", live_price)

                        if auto_pick:
                            self._replace_basket_coin(sym, trader, session_id, blacklist_symbols, inv_per_coin, is_live_execution)
                        else:
                            with self.lock:
                                if sym in self.basket_coins:
                                    del self.basket_coins[sym]
                            if not self.basket_coins:
                                with self.lock:
                                    self.is_running = False
                                    self.stop_requested = True
                                return
                        continue

                # -------------------------------------------------------------
                # CRITICAL SAFETY TRIGGER 1: Price Drops Below Stop-Loss (Per Coin)
                # -------------------------------------------------------------
                sl_price = getattr(coin.grid_bot, "stop_loss", 0.0)
                if sl_price > 0 and live_price <= sl_price:
                    self.add_log(f"🛑 [{sym} CUT-LOSS (CL) TRIGGERED] Harga live (${live_price}) menembus safety floor (${sl_price})! Mengeksekusi penutupan posisi...", "cl")
                    realized_cl_loss = -abs(coin.investment * float(getattr(config, "realized_cl_fallback_pct", 0.05)))
                    if is_live_execution and trader:
                        trader.cancel_all_orders(sym)
                        old_pos = trader.get_open_positions(sym)
                        if old_pos:
                            ep = float(old_pos[0].get("entry_price", live_price) or live_price)
                            cnt = abs(float(old_pos[0].get("contracts", 0) or 0))
                            side = old_pos[0].get("side", "LONG")
                            realized_cl_loss = (live_price - ep) * cnt if side == "LONG" else (ep - live_price) * cnt
                        res_close = trader.close_position(sym)
                        if res_close.get("closed"):
                            # NET, fee-inclusive realized loss straight from Binance's ledger
                            realized_cl_loss = self._realized_since_baseline(sym, coin, realized_cl_loss)
                            coin.realized_profit += realized_cl_loss
                            with self.lock:
                                self.realized_profit += realized_cl_loss
                            sq = sym.split('/')[1].split(':')[0] if '/' in sym else 'USDT'
                            self.add_log(f"  🛑 [{sym} CUT-LOSS SELESAI] Seluruh posisi ditutup pada pasar @ ${live_price}. Realized Loss: {realized_cl_loss:+.4f} {sq} (net fee). Modal diselamatkan!", "cl")
                            self.record_daily_trade(realized_cl_loss)
                        coin.deployed_orders.clear()
                        coin.unrealized_pnl = 0.0
                        coin.position_contracts = 0.0
                    else:
                        sim_contracts = abs(coin.position_contracts) if abs(coin.position_contracts) > 0 else (((coin.investment * self.leverage / max(1, coin.grid_count)) / max(coin.entry_price, 0.0001)) if coin.entry_price > 0 else 0.0)
                        realized_cl_loss = round((live_price - coin.entry_price) * sim_contracts, 4) if coin.entry_price > 0 else -abs(coin.investment * float(getattr(config, "realized_cl_fallback_pct", 0.05)))
                        coin.realized_profit += realized_cl_loss
                        with self.lock:
                            self.realized_profit += realized_cl_loss
                            self.wallet_balance += realized_cl_loss
                            self.available_balance += (coin.investment + realized_cl_loss)
                        sq = sym.split('/')[1].split(':')[0] if '/' in sym else 'USDT'
                        self.add_log(f"  🛑 [{sym} CUT-LOSS SIMULASI SELESAI] Posisi ditutup @ ${live_price}. Realized Loss: ${realized_cl_loss:+.4f} {sq}.", "cl")
                        self.record_daily_trade(realized_cl_loss)
                        coin.deployed_orders.clear()
                        coin.unrealized_pnl = 0.0
                        coin.position_contracts = 0.0

                    # SELF-LEARNING MEMORY RECORDING
                    self._record_memory_outcome(sym, coin, realized_cl_loss, "STOP_LOSS", live_price)

                    if auto_pick:
                        self._replace_basket_coin(sym, trader, session_id, blacklist_symbols, inv_per_coin, is_live_execution)
                    else:
                        with self.lock:
                            if sym in self.basket_coins:
                                del self.basket_coins[sym]
                        if not self.basket_coins:
                            with self.lock:
                                self.is_running = False
                                self.stop_requested = True
                            return
                    continue

                # -------------------------------------------------------------
                # CRITICAL SAFETY TRIGGER 2: Floating Loss Exceeds 15% Coin Margin
                # -------------------------------------------------------------
                # FIX 2: robust margin basis — adopted/manual coins can carry a wrong or
                # zero `investment`, which silently disabled this breaker (0 * pct = 0 ⇒
                # never trips). Fall back to contracts x entry / leverage for real margin.
                if coin.investment > 0:
                    pos_margin = coin.investment
                else:
                    pos_margin = abs(float(coin.position_contracts or 0.0)) * float(coin.entry_price or 0.0) / max(1, self.leverage)
                max_coin_loss = pos_margin * config.max_floating_loss_pct
                if coin.unrealized_pnl < -max_coin_loss:
                    self.add_log(f"🛑 [{sym} CIRCUIT BREAKER CUT-LOSS (CL)] Floating loss (-${abs(coin.unrealized_pnl):.2f}) > {config.max_floating_loss_pct*100:.0f}% margin (${max_coin_loss:.2f})! Emergency Cut-Loss...", "cl")
                    cl_cb_loss = coin.unrealized_pnl
                    if is_live_execution and trader:
                        trader.cancel_all_orders(sym)
                        res_close = trader.close_position(sym)
                        if res_close.get("closed"):
                            # NET, fee-inclusive realized loss straight from Binance's ledger
                            cl_cb_loss = self._realized_since_baseline(sym, coin, cl_cb_loss)
                            coin.realized_profit += cl_cb_loss
                            with self.lock:
                                self.realized_profit += cl_cb_loss
                            sq = sym.split('/')[1].split(':')[0] if '/' in sym else 'USDT'
                            self.add_log(f"  🛑 [{sym} CUT-LOSS DARURAT] Posisi ditutup seketika oleh Circuit Breaker. Realized Loss: {cl_cb_loss:+.4f} {sq} (net fee).", "cl")
                            self.record_daily_trade(cl_cb_loss)
                        coin.deployed_orders.clear()
                        coin.unrealized_pnl = 0.0
                        coin.position_contracts = 0.0
                    else:
                        coin.realized_profit += cl_cb_loss
                        with self.lock:
                            self.realized_profit += cl_cb_loss
                            self.wallet_balance += cl_cb_loss
                            self.available_balance += (coin.investment + cl_cb_loss)
                        sq = sym.split('/')[1].split(':')[0] if '/' in sym else 'USDT'
                        self.add_log(f"  🛑 [{sym} CUT-LOSS DARURAT SIMULASI] Posisi ditutup seketika oleh Circuit Breaker. Realized Loss: ${cl_cb_loss:+.4f} {sq}.", "cl")
                        self.record_daily_trade(cl_cb_loss)
                        coin.deployed_orders.clear()
                        coin.unrealized_pnl = 0.0
                        coin.position_contracts = 0.0

                    # SELF-LEARNING MEMORY RECORDING
                    self._record_memory_outcome(sym, coin, cl_cb_loss, "STOP_LOSS", live_price)

                    if auto_pick:
                        self._replace_basket_coin(sym, trader, session_id, blacklist_symbols, inv_per_coin, is_live_execution)
                    else:
                        with self.lock:
                            if sym in self.basket_coins:
                                del self.basket_coins[sym]
                        if not self.basket_coins:
                            with self.lock:
                                self.is_running = False
                                self.stop_requested = True
                            return
                    continue

                # -------------------------------------------------------------
                # Order Fills and Rebalancing (Per Coin)
                # -------------------------------------------------------------
                if is_live_execution and trader:
                    open_orders = trader.get_open_orders(sym)
                    open_order_ids = {o["id"] for o in open_orders}

                    completed_ids = []
                    for oid, ginfo in list(coin.deployed_orders.items()):
                        if oid not in open_order_ids:
                            completed_ids.append(oid)
                            
                            is_filled = False
                            try:
                                check_order = trader.exchange.fetch_order(oid, trader.resolve_symbol(sym))
                                if check_order.get("status") == "closed" or float(check_order.get("filled", 0) or 0) > 0:
                                    is_filled = True
                            except Exception:
                                pos_check = trader.get_open_positions(sym)
                                if pos_check and float(pos_check[0].get("contracts", 0) or 0) > 0:
                                    is_filled = True

                            if not is_filled:
                                continue

                            if ginfo["side"] == "BUY":
                                target_sell = ginfo["target_sell_price"]
                                qty = ginfo["quantity"]
                                est_profit = (target_sell - ginfo["grid_price"]) * qty
                                self.add_log(f"📥 [{sym} BUY FILLED] Dip bought @ ${ginfo['grid_price']}! Target: ${target_sell}", "fill")

                                if ginfo.get("is_base_entry"):
                                    coin.entry_price = ginfo["grid_price"]
                                    if coin.grid_bot:
                                        coin.grid_bot.entry_price = ginfo["grid_price"]
                                    self._sync_net_income_baseline(sym, coin)  # zero point for the true close delta

                                sell_price = max(target_sell, round(live_price * (coin.grid_bot.bep_markup if coin.grid_bot else config.breakeven_markup()), coin.grid_bot.precision))
                                sell_order = trader.place_limit_order(sym, "SELL", qty, sell_price, reduce_only=True)
                                if sell_order:
                                    coin.deployed_orders[sell_order["id"]] = {
                                        "grid_price": sell_price,
                                        "side": "SELL",
                                        "quantity": qty,
                                        "target_buy_price": ginfo["grid_price"],
                                        "est_profit": est_profit
                                    }
                                    sq = sym.split('/')[1].split(':')[0] if '/' in sym else 'USDT'
                                    self.add_log(f"  🎯 [{sym} TAKE-PROFIT ORDER] Pasang target jual @ ${sell_price} (Target Profit: +${est_profit:.4f} {sq})", "tp")
                                    pos = trader.get_open_positions(sym)
                                    if pos:
                                        coin.position_contracts = pos[0]["contracts"]
                                        coin.unrealized_pnl = pos[0]["unrealized_pnl"]
                                    self._sync_hard_stop(sym, coin, trader, reason="grid buy fill")

                            elif ginfo["side"] == "SELL":
                                est_gross = float(ginfo.get("est_profit", 0.0) or 0.0)
                                # NET, fee-inclusive realized PnL straight from Binance's ledger
                                realized = self._realized_since_baseline(sym, coin, est_gross)
                                coin.realized_profit += realized
                                coin.matched_trades += 1
                                with self.lock:
                                    self.realized_profit += realized
                                    self.matched_trades += 1
                                sq = sym.split('/')[1].split(':')[0] if '/' in sym else 'USDT'
                                self.add_log(f"🎯 [TAKE-PROFIT (TP) TERCAPAI!] {sym} Limit Sell @ ${ginfo['grid_price']} tereksekusi! Net Profit: {realized:+.4f} {sq} setelah fee (Total Sukses TP: {coin.matched_trades})", "tp")
                                self.record_daily_trade(realized)

                                # SELF-LEARNING MEMORY RECORDING
                                self._record_memory_outcome(sym, coin, realized, "TAKE_PROFIT", ginfo["grid_price"])

                                # 3. AUTO BREAK-EVEN PLUS STOP-LOSS (Risk-Free Trade Locking)
                                if not coin.is_breakeven_active and coin.entry_price > 0 and coin.grid_bot:
                                    be_price = round(coin.entry_price * (coin.grid_bot.bep_markup if coin.grid_bot else config.breakeven_markup()), coin.grid_bot.precision)
                                    if be_price > coin.grid_bot.stop_loss:
                                        coin.grid_bot.stop_loss = be_price
                                        coin.is_breakeven_active = True
                                        self.add_log(f"🛡️ [{sym} BREAK-EVEN PLUS AKTIF] TP berhasil dikunci ({realized:+.4f} {sq})! Stop-Loss otomatis dinaikkan di atas modal (${be_price}) => Posisi 100% BEBAS RESIKO!", "success")
                                        self._sync_hard_stop(sym, coin, trader, reason="break-even")

                                re_buy = ginfo.get("target_buy_price", round(ginfo["grid_price"] - coin.grid_bot.grid_step, coin.grid_bot.precision))
                                buy_order = trader.place_limit_order(sym, "BUY", ginfo["quantity"], re_buy)
                                if buy_order:
                                    coin.deployed_orders[buy_order["id"]] = {
                                        "grid_price": re_buy,
                                        "side": "BUY",
                                        "quantity": ginfo["quantity"],
                                        "target_sell_price": ginfo["grid_price"]
                                    }

                    for cid in completed_ids:
                        coin.deployed_orders.pop(cid, None)

                    # Update position stats for this coin
                    pos = trader.get_open_positions(sym)
                    if pos:
                        coin.unrealized_pnl = pos[0]["unrealized_pnl"]
                        coin.position_contracts = pos[0]["contracts"]
                    else:
                        coin.unrealized_pnl = 0.0
                        coin.position_contracts = 0.0
                else:
                    # Simulation mode per coin: update floating unrealized PnL with live Binance ticker
                    if coin.entry_price > 0 and abs(coin.position_contracts) > 0:
                        coin.unrealized_pnl = round((live_price - coin.entry_price) * coin.position_contracts, 4)

                    events = coin.grid_bot.update_price(live_price)
                    for ev in events:
                        if ev.get("event") == "BUY_GRID_FILLED":
                            self.add_log(f"📥 [{sym} SIM BUY] @ ${ev['price']} -> Target TP: ${ev['next_target']}", "fill")
                        elif ev.get("event") == "SELL_GRID_PROFIT_LOCKED":
                            coin.realized_profit += ev["realized_profit"]
                            coin.matched_trades += 1
                            with self.lock:
                                self.realized_profit += ev["realized_profit"]
                                self.matched_trades += 1
                                self.wallet_balance += ev["realized_profit"]
                                self.available_balance += ev["realized_profit"]
                            sq = sym.split('/')[1].split(':')[0] if '/' in sym else 'USDT'
                            self.add_log(f"🎯 [{sym} TAKE-PROFIT (TP) SIMULASI] Sold @ ${ev['price']} (+${ev['realized_profit']} {sq})!", "tp")
                            self.record_daily_trade(ev["realized_profit"])

                            # SELF-LEARNING MEMORY RECORDING
                            self._record_memory_outcome(sym, coin, ev["realized_profit"], "TAKE_PROFIT", ev.get('price', live_price))

                        elif ev.get("event") == "BREAKEVEN_STOP_ACTIVATED":
                            coin.is_breakeven_active = True
                            self.add_log(f"🛡️ [{sym} BREAK-EVEN STOP AKTIF (SIM)] {ev.get('message')}", "success")
                        elif ev.get("event") == "STOP_LOSS_TRIGGERED":
                            sim_contracts = abs(coin.position_contracts) if abs(coin.position_contracts) > 0 else (((coin.investment * self.leverage / max(1, coin.grid_count)) / max(coin.entry_price, 0.0001)) if coin.entry_price > 0 else 0.0)
                            sim_loss = round((ev['price'] - coin.entry_price) * sim_contracts, 4) if coin.entry_price > 0 else -abs(coin.investment * float(getattr(config, "realized_cl_fallback_pct", 0.05)))
                            coin.realized_profit += sim_loss
                            with self.lock:
                                self.realized_profit += sim_loss
                                self.wallet_balance += sim_loss
                                self.available_balance += (coin.investment + sim_loss)
                            coin.unrealized_pnl = 0.0
                            coin.position_contracts = 0.0
                            sq = sym.split('/')[1].split(':')[0] if '/' in sym else 'USDT'
                            self.add_log(f"🛑 [{sym} CUT-LOSS (CL) SIMULASI] Harga live menyentuh Stop Loss (${ev['price']})! Realized Loss: ${sim_loss:+.4f} {sq}. Posisi ditutup.", "cl")
                            self.record_daily_trade(sim_loss)

                            # SELF-LEARNING MEMORY RECORDING
                            self._record_memory_outcome(sym, coin, sim_loss, "STOP_LOSS", ev.get('price', live_price))

                            if auto_pick:
                                self._replace_basket_coin(sym, trader, session_id, blacklist_symbols, inv_per_coin, is_live_execution)

            # Heartbeat stream every 3 ticks
            if tick_counter % 3 == 0:
                summary_parts = []
                for s, c in self.basket_coins.items():
                    pos_txt = f"{c.position_contracts}" if abs(c.position_contracts) > 0 else "0"
                    pnl_txt = f"+${c.unrealized_pnl:.4f}" if c.unrealized_pnl >= 0 else f"-${abs(c.unrealized_pnl):.4f}"
                    clean_s = s.split('/')[0]
                    summary_parts.append(f"{clean_s}: ${c.current_price} ({pnl_txt}, {len(c.deployed_orders)} ord)")
                tot_pnl = sum(c.unrealized_pnl for c in self.basket_coins.values())
                tot_pnl_str = f"+${tot_pnl:.4f}" if tot_pnl >= 0 else f"-${abs(tot_pnl):.4f}"
                self.add_log(f"⚡ [BASKET HEARTBEAT] {' | '.join(summary_parts)} || Total PnL: {tot_pnl_str} {quote_currency}", "tick")

            # Refresh balance periodically
            if is_live_execution and trader and tick_counter % 2 == 0:
                bal = trader.get_balance(asset=quote_currency)
                with self.lock:
                    self.wallet_balance = bal.get("total", self.wallet_balance)
                    self.available_balance = bal.get("free", self.available_balance)

            # Broadcast state
            self.broadcast_event({"event": "STATE_UPDATE", "data": self.get_state()})

bot_manager = BotWebManager()

