import os
from typing import List, Literal
from pydantic import BaseModel, Field
from dotenv import load_dotenv, find_dotenv

# Load .env file with explicit override
load_dotenv(find_dotenv(), override=True)

# Scanner-side TradFi exclusion flag, resolved ONCE at module level.
_SCAN_BLACKLIST_TRADFI = os.getenv("SCAN_BLACKLIST_TRADFI", "false").strip().lower() in ("1", "true", "yes", "on")

class BotConfig(BaseModel):
    # Execution mode
    trading_mode: Literal["paper", "testnet", "live"] = Field(
        default=os.getenv("TRADING_MODE", "paper").lower()  # type: ignore
    )

    # Binance credentials (Supports separate LIVE and DEMO keys)
    binance_api_key: str = Field(default=os.getenv("BINANCE_API_KEY", ""))
    binance_api_secret: str = Field(default=os.getenv("BINANCE_API_SECRET", ""))
    binance_live_api_key: str = Field(default=os.getenv("BINANCE_LIVE_API_KEY", os.getenv("BINANCE_API_KEY", "")))
    binance_live_api_secret: str = Field(default=os.getenv("BINANCE_LIVE_API_SECRET", os.getenv("BINANCE_API_SECRET", "")))
    binance_demo_api_key: str = Field(default=os.getenv("BINANCE_DEMO_API_KEY", os.getenv("BINANCE_API_KEY", "")))
    binance_demo_api_secret: str = Field(default=os.getenv("BINANCE_DEMO_API_SECRET", os.getenv("BINANCE_API_SECRET", "")))

    # AI Provider settings (Supports: 'gemini', 'omniroute', '9router', 'openai')
    ai_provider: str = Field(default=os.getenv("AI_PROVIDER", "9router").lower())
    ai_base_url: str = Field(default=os.getenv("AI_BASE_URL", ""))
    ai_api_key: str = Field(default=os.getenv("AI_API_KEY", ""))
    ai_model: str = Field(default=os.getenv("AI_MODEL", "auto-models"))

    # Gemini AI credentials (Legacy fallback)
    gemini_api_key: str = Field(default=os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = Field(default=os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"))




    # Market & Pairs
    trading_symbols: List[str] = Field(
        default=[s.strip() for s in os.getenv("TRADING_SYMBOLS", "").split(",") if s.strip()]
    )
    timeframe: str = Field(default=os.getenv("TIMEFRAME", "15m"))
    leverage: int = Field(default=int(os.getenv("LEVERAGE", "5")))
    margin_type: Literal["ISOLATED", "CROSSED"] = Field(
        default=os.getenv("MARGIN_TYPE", "ISOLATED").upper()  # type: ignore
    )

    default_quote_asset: str = Field(default=os.getenv("QUOTE_ASSET", "USDT").upper())
    default_grid_count: int = Field(default=int(os.getenv("GRID_COUNT", "6")))
    default_investment: float = Field(default=float(os.getenv("DEFAULT_INVESTMENT", "50.0")))
    # FIX 3/4: fee & overtrading reduction. Each matched grid must net >= this % of price,
    # so grids are wider and fewer (0.8% floor instead of the old ~0.7% churn).
    min_grid_step_pct: float = Field(default=float(os.getenv("MIN_GRID_STEP_PCT", "0.008")))

    # FIX 1: junk-coin ban thresholds (enforced at the single entry chokepoint)
    ban_min_quote_volume: float = Field(default=float(os.getenv("BAN_MIN_QUOTE_VOLUME", "50000000")))
    min_listing_age_days: int = Field(default=int(os.getenv("MIN_LISTING_AGE_DAYS", "14")))

    # Risk Management Guardrails
    max_risk_per_trade: float = Field(default=float(os.getenv("MAX_RISK_PER_TRADE", "0.02")))
    # RISK-BASED SIZING (LIVE grid path): risk per trade = this fraction of the wallet; the
    # position is derived from the STOP DISTANCE (qty = risk_usd / |entry - stop|), so a wider
    # stop yields a smaller position. The AI may only LOWER it, within [floor, cap]. The cap is
    # risk_per_trade_pct; the floor stops an over-cautious AI from sizing down to nothing.
    risk_per_trade_pct: float = Field(default=float(os.getenv("RISK_PER_TRADE_PCT", "0.02")))
    risk_per_trade_floor_pct: float = Field(default=float(os.getenv("RISK_PER_TRADE_FLOOR_PCT", "0.005")))
    max_daily_loss_pct: float = Field(default=float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05")))
    max_daily_trades: int = Field(default=int(os.getenv("MAX_DAILY_TRADES", "12")))
    # CHURN CAP: fresh ENTRIES per UTC day (distinct from max_daily_trades which counts
    # CLOSED trades). Fee audit: the runaway day burned 45 entries for -0.74 while healthy
    # days ran <=7 entries and stayed GREEN (+0.17/+0.33). The bleed is churn, not variance.
    # 8 allows the healthy band while hard-capping the runaway re-scan churn. 0 disables.
    # Adopts are exempt -- an already-open position must always be managed, never blocked.
    max_daily_entries: int = Field(default=int(os.getenv("MAX_DAILY_ENTRIES", "8")))
    max_margin_per_trade_pct: float = Field(default=float(os.getenv("MAX_MARGIN_PER_TRADE_PCT", "0.35")))
    min_notional_threshold: float = Field(default=float(os.getenv("MIN_NOTIONAL_THRESHOLD", "5.50")))
    # SNAPSHOT PRECISION. Fixed `round(x, 4)` zeroes the ATR (and distorts the price) of
    # sub-$0.10 assets: GMT's ATR 3.9e-05 and JASMY's 2.6e-05 both collapse to 0.0, which the
    # entry guard then reads as "ATR tak tersedia" and skips the coin. Significant-figure
    # rounding is correct at every magnitude; 8 sig-figs keeps sub-dollar loss < 1e-6% and is
    # exact for large prices. Env-overridable (no hardcoded literal).
    indicator_price_sig_digits: int = Field(default=int(os.getenv("INDICATOR_PRICE_SIG_DIGITS", "8")))
    taker_fee_rate: float = Field(default=float(os.getenv("TAKER_FEE_RATE", "0.0005")))
    maker_fee_rate: float = Field(default=float(os.getenv("MAKER_FEE_RATE", "0.0002")))
    use_exchange_hard_stop: bool = Field(default=os.getenv("USE_EXCHANGE_HARD_STOP", "true").lower() in ("1", "true", "yes", "on"))
    # TRADFI-PERPS (-4411): auto-sign the TradFi agreement then retry the order once.
    # tradfi_blacklist_tradifi=True rejects TRADIFI_PERPETUAL symbols in resolve_symbol.
    tradfi_auto_sign: bool = Field(default=os.getenv("TRADFI_AUTO_SIGN", "true").lower() in ("1", "true", "yes", "on"))
    tradfi_blacklist_tradifi: bool = Field(default=os.getenv("TRADFI_BLACKLIST_TRADIFI", "false").lower() in ("1", "true", "yes", "on"))
    # Scanner-side twin of tradfi_blacklist_tradifi: keeps TRADIFI_PERPETUAL
    # tokenized-stock/commodity perps (SAMSUNG, XAU, TSLA — 221 markets) out of the
    # auto-scan pool. Default false = current behavior preserved; set
    # SCAN_BLACKLIST_TRADFI=true in .env to exclude them from scanning.
    # SCAN_BLACKLIST_TRADFI resolved at module level (constant below).
    scan_blacklist_tradifi: bool = Field(default=_SCAN_BLACKLIST_TRADFI)

    # P2: Exit ladder (R:R redesign). Expressed as fraction of entry price.
    # TP1 widened 1.8%->3.0% from sweep_tp1_oos.py: on the edge-gated population the
    # 1.8% half-target left ~+0.07..0.12R/trade on the table; 2.8..3.5% is a flat,
    # out-of-sample-robust plateau (wins at bars=700/1000/1400), 3.0% is its center.
    breakeven_trigger_pct: float = Field(default=float(os.getenv("BREAKEVEN_TRIGGER_PCT", "0.012")))
    tp1_target_pct: float = Field(default=float(os.getenv("TP1_TARGET_PCT", "0.030")))
    trailing_start_pct: float = Field(default=float(os.getenv("TRAILING_START_PCT", "0.028")))
    trailing_dist_pct: float = Field(default=float(os.getenv("TRAILING_DIST_PCT", "0.015")))
    enforce_min_rr: bool = Field(default=os.getenv("ENFORCE_MIN_RR", "true").lower() in ("1", "true", "yes", "on"))
    # P6: regime edge gate — from pooled OOS scan, entries that are NOT in a confirmed
    # bull trend AND NOT on dead volume carry +0.17..0.19R expectancy (PF ~1.5) vs -0.02R
    # baseline. This is the statistical edge the weighted-sum score never had.
    enforce_edge_gate: bool = Field(default=os.getenv("ENFORCE_EDGE_GATE", "true").lower() in ("1", "true", "yes", "on"))
    edge_min_volume_ratio: float = Field(default=float(os.getenv("EDGE_MIN_VOLUME_RATIO", "0.7")))
    # AI prompt Rule 3: force HOLD when volume_condition == LOW_VOLUME_DANGER. This collides
    # with the validated edge, which LIVES in the low-volume NEUTRAL/consolidation bucket
    # (NEUTRAL|normal measured +0.29R; dead volume is NOT a losing bucket). Set false to align
    # the AI filter with the edge so it stops vetoing ~44% of gate-passing candidates.
    # MACD/trend contradiction still forces HOLD regardless — only the volume clause is toggled.
    ai_volume_hold_enabled: bool = Field(default=os.getenv("AI_VOLUME_HOLD_ENABLED", "true").lower() in ("1", "true", "yes", "on"))
    min_stop_loss_pct: float = Field(default=float(os.getenv("MIN_STOP_LOSS_PCT", "0.018")))
    use_closed_candle_only: bool = Field(default=os.getenv("USE_CLOSED_CANDLE_ONLY", "true").lower() in ("1", "true", "yes", "on"))
    loop_interval_seconds: int = Field(default=int(os.getenv("LOOP_INTERVAL_SECONDS", "5")))
    # Filesystem kill switch: the mere PRESENCE of this file halts trading no matter what
    # state the dashboard, HTTP API or systemd are in — the panic switch that still works
    # when every other control surface is down. Delete the file to re-enable. Path is
    # env-overridable; the check reads this value, never a hardcoded literal.
    kill_switch_file: str = Field(default=os.getenv("KILL_SWITCH_FILE", "KILL_SWITCH"))
    max_open_positions: int = Field(default=int(os.getenv("MAX_OPEN_POSITIONS", "2")))
    # MANDATE: hard exposure ceiling. max_leverage caps the multiplier the API may request
    # (unbounded before this — leverage=125 from the dashboard was accepted, so gross notional
    # could reach 0.85*balance*125). max_total_notional_usdt caps Σ(margin*leverage) across the
    # whole basket; 0 disables the check. Both env-overridable, never literals at the call site.
    max_leverage: int = Field(default=int(os.getenv("MAX_LEVERAGE", "10")))
    max_total_notional_usdt: float = Field(default=float(os.getenv("MAX_TOTAL_NOTIONAL_USDT", "0")))
    # Balance-scaled ceiling: gross notional (Σ margin*leverage) <= max_exposure_mult * equity.
    # Self-scales with the account, so it stays correct as the wallet grows/shrinks (an absolute
    # USD cap is meaningless on a $10 wallet and dangerous on a $10k one). 0 disables. When both
    # this and max_total_notional_usdt are set, the STRICTER (smaller) ceiling wins.
    max_exposure_mult: float = Field(default=float(os.getenv("MAX_EXPOSURE_MULT", "2.0")))
    # Append-only, tamper-evident audit ledger (JSONL, SHA-256 hash-chained). Env-overridable.
    ledger_file: str = Field(default=os.getenv("LEDGER_FILE", "trade_ledger.jsonl"))
    min_risk_reward_ratio: float = Field(default=float(os.getenv("MIN_RISK_REWARD_RATIO", "1.5")))
    min_ai_confidence: float = Field(default=float(os.getenv("MIN_AI_CONFIDENCE", "0.70")))

    # ========================================================================
    # AUDIT 2026-10-06 — PRE-ENTRY GATE LAYER (ketat, env-overridable)
    # ========================================================================
    # (Pre-entry gate fields are defined ONCE in the P0 block below.)

    # Simulation settings
    paper_initial_balance: float = Field(default=float(os.getenv("PAPER_INITIAL_BALANCE", "1000.0")))
    check_interval_minutes: int = Field(default=int(os.getenv("CHECK_INTERVAL_MINUTES", "15")))

    # Telegram alerts (Optional)
    telegram_bot_token: str = Field(default=os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = Field(default=os.getenv("TELEGRAM_CHAT_ID", ""))

    def round_trip_fee_rate(self) -> float:
        """Round-trip taker fee (entry + exit) — the true cost floor of a trade."""
        return self.taker_fee_rate * 2.0

    def breakeven_markup(self) -> float:
        """Price multiplier that lifts SL above entry enough to cover round-trip fee + slippage."""
        return 1.0 + self.round_trip_fee_rate() + self.fee_buffer_pct

    # P7: de-hardcode risk knobs so they live in config/.env, not in the loop body.
    max_floating_loss_pct: float = Field(default=float(os.getenv("MAX_FLOATING_LOSS_PCT", "0.15")))
    # FIX 2: hard per-position loss cap in R multiples of the frozen entry->stop risk.
    # Robust for adopted/manual coins whose `investment` is wrong or zero.
    max_position_loss_r: float = Field(default=float(os.getenv("MAX_POSITION_LOSS_R", "1.5")))
    # FIX 2 (stop churn): before a (re)placement of the protective stop, skip it when the new
    # price is within this many symbol TICKS of the currently-live stop (tick read from
    # exchangeInfo, never a literal). A pct-of-price epsilon is the last-resort tolerance when
    # the venue publishes no tick. Kills the ~155 re-placements/15h churn.
    stop_replace_tick_tolerance: float = Field(default=float(os.getenv("STOP_REPLACE_TICK_TOLERANCE", "1.0")))
    stop_replace_price_epsilon_pct: float = Field(default=float(os.getenv("STOP_REPLACE_PRICE_EPSILON_PCT", "0.0005")))
    # Slippage margin tolerated above max_position_loss_r when reconciling a realized stop-out R.
    loss_cap_slippage_margin_r: float = Field(default=float(os.getenv("LOSS_CAP_SLIPPAGE_MARGIN_R", "0.25")))
    fee_buffer_pct: float = Field(default=float(os.getenv("FEE_BUFFER_PCT", "0.001")))
    pullback_entry_pct: float = Field(default=float(os.getenv("PULLBACK_ENTRY_PCT", "0.005")))

    # ========================================================================
    # AUDIT 2026-10-04 — DE-HARDCODE LAYER. Every previously-inline literal now
    # lives here and is env-overridable, so nothing is baked into the code.
    # ========================================================================

    # Sizing / wallet usage
    margin_usage_pct: float = Field(default=float(os.getenv("MARGIN_USAGE_PCT", "0.85")))
    min_daily_loss_floor: float = Field(default=float(os.getenv("MIN_DAILY_LOSS_FLOOR", "2.0")))
    min_notional_buffer: float = Field(default=float(os.getenv("MIN_NOTIONAL_BUFFER", "1.02")))
    fee_cache_ttl_seconds: int = Field(default=int(os.getenv("FEE_CACHE_TTL_SECONDS", "300")))

    # Grid geometry (ATR multiples / percent floors) — engine no longer hardcodes these
    range_lower_atr_mult: float = Field(default=float(os.getenv("RANGE_LOWER_ATR_MULT", "1.8")))
    range_lower_pct_floor: float = Field(default=float(os.getenv("RANGE_LOWER_PCT_FLOOR", "0.022")))
    range_upper_atr_mult: float = Field(default=float(os.getenv("RANGE_UPPER_ATR_MULT", "2.5")))
    range_upper_pct_floor: float = Field(default=float(os.getenv("RANGE_UPPER_PCT_FLOOR", "0.035")))
    sl_cushion_atr_mult: float = Field(default=float(os.getenv("SL_CUSHION_ATR_MULT", "0.7")))
    sl_cushion_pct: float = Field(default=float(os.getenv("SL_CUSHION_PCT", "0.010")))
    stop_grid_clearance_atr: float = Field(default=float(os.getenv("STOP_GRID_CLEARANCE_ATR", "0.5")))
    grid_step_fallback_pct: float = Field(default=float(os.getenv("GRID_STEP_FALLBACK_PCT", "0.008")))
    atr_fallback_pct: float = Field(default=float(os.getenv("ATR_FALLBACK_PCT", "0.02")))

    # Indicator periods (Wilder-standard defaults)
    rsi_period: int = Field(default=int(os.getenv("RSI_PERIOD", "14")))
    atr_period: int = Field(default=int(os.getenv("ATR_PERIOD", "14")))
    ema_fast: int = Field(default=int(os.getenv("EMA_FAST", "20")))
    ema_slow: int = Field(default=int(os.getenv("EMA_SLOW", "50")))
    ema_trend: int = Field(default=int(os.getenv("EMA_TREND", "200")))
    macd_fast: int = Field(default=int(os.getenv("MACD_FAST", "12")))
    macd_slow: int = Field(default=int(os.getenv("MACD_SLOW", "26")))
    macd_signal: int = Field(default=int(os.getenv("MACD_SIGNAL", "9")))
    bb_period: int = Field(default=int(os.getenv("BB_PERIOD", "20")))
    bb_std_mult: float = Field(default=float(os.getenv("BB_STD_MULT", "2.0")))
    volatility_window: int = Field(default=int(os.getenv("VOLATILITY_WINDOW", "20")))

    # Multi-timeframe / OHLCV fetch windows
    mtf_timeframe: str = Field(default=os.getenv("MTF_TIMEFRAME", "1h"))
    mtf_limit: int = Field(default=int(os.getenv("MTF_LIMIT", "50")))
    mtf_min_bars: int = Field(default=int(os.getenv("MTF_MIN_BARS", "30")))
    ohlcv_limit: int = Field(default=int(os.getenv("OHLCV_LIMIT", "150")))
    ohlcv_min_bars: int = Field(default=int(os.getenv("OHLCV_MIN_BARS", "40")))
    mtf_macro_rsi_bull: float = Field(default=float(os.getenv("MTF_MACRO_RSI_BULL", "46.0")))
    mtf_macro_rsi_bear: float = Field(default=float(os.getenv("MTF_MACRO_RSI_BEAR", "48.0")))

    # Hybrid engine blend + entry cutoffs
    quant_weight: float = Field(default=float(os.getenv("QUANT_WEIGHT", "0.70")))
    ai_weight: float = Field(default=float(os.getenv("AI_WEIGHT", "0.30")))
    entry_score_long: float = Field(default=float(os.getenv("ENTRY_SCORE_LONG", "62.0")))
    entry_score_short: float = Field(default=float(os.getenv("ENTRY_SCORE_SHORT", "35.0")))
    ai_fallback_confidence: float = Field(default=float(os.getenv("AI_FALLBACK_CONFIDENCE", "0.60")))
    defensive_sl_pct: float = Field(default=float(os.getenv("DEFENSIVE_SL_PCT", "0.012")))
    min_risk_dist_atr_mult: float = Field(default=float(os.getenv("MIN_RISK_DIST_ATR_MULT", "0.7")))
    min_risk_dist_pct: float = Field(default=float(os.getenv("MIN_RISK_DIST_PCT", "0.005")))
    target_rr_mult: float = Field(default=float(os.getenv("TARGET_RR_MULT", "2.0")))
    target_atr_mult: float = Field(default=float(os.getenv("TARGET_ATR_MULT", "1.8")))
    grid_vol_high_pct: float = Field(default=float(os.getenv("GRID_VOL_HIGH_PCT", "3.5")))
    grid_vol_low_pct: float = Field(default=float(os.getenv("GRID_VOL_LOW_PCT", "1.5")))
    grid_count_high_vol: int = Field(default=int(os.getenv("GRID_COUNT_HIGH_VOL", "6")))
    grid_count_low_vol: int = Field(default=int(os.getenv("GRID_COUNT_LOW_VOL", "10")))
    grid_count_mid_vol: int = Field(default=int(os.getenv("GRID_COUNT_MID_VOL", "8")))

    # AI provider runtime
    ai_temperature: float = Field(default=float(os.getenv("AI_TEMPERATURE", "0.1")))
    ai_request_timeout: float = Field(default=float(os.getenv("AI_REQUEST_TIMEOUT", "10.0")))
    ai_max_retries: int = Field(default=int(os.getenv("AI_MAX_RETRIES", "2")))
    ai_retry_backoff_seconds: float = Field(default=float(os.getenv("AI_RETRY_BACKOFF_SECONDS", "1.5")))
    ai_confidence_min: float = Field(default=float(os.getenv("AI_CONFIDENCE_MIN", "0.50")))
    ai_confidence_max: float = Field(default=float(os.getenv("AI_CONFIDENCE_MAX", "0.98")))

    # Scanner thresholds (all previously inline literals)
    scanner_min_volume: float = Field(default=float(os.getenv("SCANNER_MIN_VOLUME", "10000000")))
    scanner_fallback_volume: float = Field(default=float(os.getenv("SCANNER_FALLBACK_VOLUME", "2000000")))
    scanner_alpha_fallback_volume: float = Field(default=float(os.getenv("SCANNER_ALPHA_FALLBACK_VOLUME", "3000000")))
    scanner_min_chg_pct: float = Field(default=float(os.getenv("SCANNER_MIN_CHG_PCT", "0.5")))
    scanner_max_chg_pct: float = Field(default=float(os.getenv("SCANNER_MAX_CHG_PCT", "15.0")))
    scanner_alpha_min_chg_pct: float = Field(default=float(os.getenv("SCANNER_ALPHA_MIN_CHG_PCT", "-8.0")))
    scanner_alpha_max_chg_pct: float = Field(default=float(os.getenv("SCANNER_ALPHA_MAX_CHG_PCT", "12.0")))
    scanner_bull_rsi_min: float = Field(default=float(os.getenv("SCANNER_BULL_RSI_MIN", "44.0")))
    scanner_bull_rsi_max: float = Field(default=float(os.getenv("SCANNER_BULL_RSI_MAX", "60.0")))
    scanner_alpha_rsi_max: float = Field(default=float(os.getenv("SCANNER_ALPHA_RSI_MAX", "55.0")))
    scanner_alpha_rsi_min: float = Field(default=float(os.getenv("SCANNER_ALPHA_RSI_MIN", "38.0")))
    scanner_bull_dist_low: float = Field(default=float(os.getenv("SCANNER_BULL_DIST_LOW", "-0.008")))
    scanner_bull_dist_high: float = Field(default=float(os.getenv("SCANNER_BULL_DIST_HIGH", "0.015")))
    scanner_max_dist_ema20_pct: float = Field(default=float(os.getenv("SCANNER_MAX_DIST_EMA20_PCT", "0.015")))
    scanner_ema50_floor: float = Field(default=float(os.getenv("SCANNER_EMA50_FLOOR", "0.99")))
    scanner_ema_slope_min: float = Field(default=float(os.getenv("SCANNER_EMA_SLOPE_MIN", "0.997")))
    scanner_vol_dead_ratio: float = Field(default=float(os.getenv("SCANNER_VOL_DEAD_RATIO", "0.6")))
    scanner_vol_confirm_ratio: float = Field(default=float(os.getenv("SCANNER_VOL_CONFIRM_RATIO", "1.2")))
    scanner_bull_vol_ratio: float = Field(default=float(os.getenv("SCANNER_BULL_VOL_RATIO", "0.7")))
    scanner_risk_atr_mult: float = Field(default=float(os.getenv("SCANNER_RISK_ATR_MULT", "1.8")))
    scanner_risk_pct_floor: float = Field(default=float(os.getenv("SCANNER_RISK_PCT_FLOOR", "0.02")))
    scanner_reward_atr_mult: float = Field(default=float(os.getenv("SCANNER_REWARD_ATR_MULT", "2.2")))
    scanner_min_rr: float = Field(default=float(os.getenv("SCANNER_MIN_RR", "1.8")))
    scanner_rr_score_mult: float = Field(default=float(os.getenv("SCANNER_RR_SCORE_MULT", "14.0")))
    scanner_vol_ratio_cap: float = Field(default=float(os.getenv("SCANNER_VOL_RATIO_CAP", "2.0")))
    scanner_ohlcv_limit: int = Field(default=int(os.getenv("SCANNER_OHLCV_LIMIT", "60")))
    scanner_ohlcv_min_bars: int = Field(default=int(os.getenv("SCANNER_OHLCV_MIN_BARS", "35")))
    scanner_bull_pool: int = Field(default=int(os.getenv("SCANNER_BULL_POOL", "20")))
    scanner_alpha_pool: int = Field(default=int(os.getenv("SCANNER_ALPHA_POOL", "30")))
    scanner_edge_pool: int = Field(default=int(os.getenv("SCANNER_EDGE_POOL", "60")))

    # Memory / learning
    memory_cooldown_seconds: int = Field(default=int(os.getenv("MEMORY_COOLDOWN_SECONDS", "14400")))
    memory_guard_min_sample: int = Field(default=int(os.getenv("MEMORY_GUARD_MIN_SAMPLE", "15")))
    memory_cluster_min_sample: int = Field(default=int(os.getenv("MEMORY_CLUSTER_MIN_SAMPLE", "10")))
    # COLD-START GUARD for the AI-facing lesson feed. get_recent_lessons_for_ai_prompt()
    # injected raw losses from the FIRST trade on, poisoning the AI with a 0%-win-rate
    # narrative before any real sample existed (it then vetoes everything). Reuses the same
    # min-sample concept as the score penalty so both agree on when memory is meaningful.
    memory_lesson_feed_min_sample: int = Field(default=int(os.getenv("MEMORY_LESSON_FEED_MIN_SAMPLE", "15")))
    # (ai_caution_min_confidence is defined in the P0 block below.)

    # Live safety: how often the exchange-side hard stop is re-verified (seconds)
    hard_stop_recheck_seconds: int = Field(default=int(os.getenv("HARD_STOP_RECHECK_SECONDS", "100")))
    # Slow monitor cadence: dual-engine analysis pulse interval (seconds)
    analysis_pulse_seconds: int = Field(default=int(os.getenv("ANALYSIS_PULSE_SECONDS", "300")))
    # When a scan cycle yields no enterable (edge-aligned) coin, back off this long and
    # re-scan instead of halting the session. Keeps the bot alive through transient
    # all-BULLISH pumps the edge gate correctly rejects.
    retry_scan_seconds: int = Field(default=int(os.getenv("RETRY_SCAN_SECONDS", "60")))
    # Post-news volatility gate: once a high-impact event releases, keep blocking NEW
    # entries until measured volatility has settled, not on a fixed timer. An entry is only
    # allowed when ATR(now)/ATR(20-avg) <= volatility_gate_atr_ratio OR the calm window
    # (minutes since release >= volatility_calm_minutes) has fully elapsed. Either condition
    # alone opens the gate; both together guarantee a real settle.
    volatility_gate_enabled: bool = Field(default=os.getenv("VOLATILITY_GATE_ENABLED", "true").lower() == "true")
    volatility_gate_atr_ratio: float = Field(default=float(os.getenv("VOLATILITY_GATE_ATR_RATIO", "1.5")))
    volatility_calm_minutes: int = Field(default=int(os.getenv("VOLATILITY_CALM_MINUTES", "45")))
    # Market-wide volatility probe (proxy symbol) used by the pre-scan STANDBY loop so it can
    # release early once the tape calms, instead of sleeping out the whole calm window.
    volatility_probe_symbol: str = Field(default=os.getenv("VOLATILITY_PROBE_SYMBOL", "BTC/USDT"))
    # Fallback realized-loss estimate when a live close cannot be reconciled (% of investment)
    realized_cl_fallback_pct: float = Field(default=float(os.getenv("REALIZED_CL_FALLBACK_PCT", "0.05")))
    # Hard per-position loss cap fallback (when the R anchor is unknown)
    position_loss_fallback_pct: float = Field(default=float(os.getenv("POSITION_LOSS_FALLBACK_PCT", "0.05")))

    # Quant score factors (previously inline literals in QuantMathEngine)
    ema_dist_overextended_pct: float = Field(default=float(os.getenv("EMA_DIST_OVEREXTENDED_PCT", "1.5")))
    ema_dist_ideal_low: float = Field(default=float(os.getenv("EMA_DIST_IDEAL_LOW", "-0.6")))
    ema_dist_ideal_high: float = Field(default=float(os.getenv("EMA_DIST_IDEAL_HIGH", "1.0")))
    quant_rsi_overbought: float = Field(default=float(os.getenv("QUANT_RSI_OVERBOUGHT", "74.0")))
    quant_rsi_oversold: float = Field(default=float(os.getenv("QUANT_RSI_OVERSOLD", "32.0")))
    quant_rsi_healthy_low: float = Field(default=float(os.getenv("QUANT_RSI_HEALTHY_LOW", "45.0")))
    quant_rsi_healthy_high: float = Field(default=float(os.getenv("QUANT_RSI_HEALTHY_HIGH", "68.0")))
    quant_vol_strong_ratio: float = Field(default=float(os.getenv("QUANT_VOL_STRONG_RATIO", "1.2")))
    quant_vol_weak_ratio: float = Field(default=float(os.getenv("QUANT_VOL_WEAK_RATIO", "0.6")))
    quant_sl_multiplier: float = Field(default=float(os.getenv("QUANT_SL_MULTIPLIER", "1.5")))

    # Quant decision cutoffs & consensus blend (were inline literals)
    quant_long_cutoff: float = Field(default=float(os.getenv("QUANT_LONG_CUTOFF", "62.0")))
    quant_short_cutoff: float = Field(default=float(os.getenv("QUANT_SHORT_CUTOFF", "35.0")))
    # Grid-count recommendation bands (volatility pct)
    vol_high_pct: float = Field(default=float(os.getenv("VOL_HIGH_PCT", "3.5")))
    vol_low_pct: float = Field(default=float(os.getenv("VOL_LOW_PCT", "1.5")))
    # Quant TP/SL distance builders
    tp_rr_mult: float = Field(default=float(os.getenv("TP_RR_MULT", "2.0")))
    tp_atr_mult: float = Field(default=float(os.getenv("TP_ATR_MULT", "1.8")))
    # Fallback confidences (avoid fabricating when AI output is unusable)
    ai_default_confidence: float = Field(default=float(os.getenv("AI_DEFAULT_CONFIDENCE", "0.65")))
    fallback_ai_confidence: float = Field(default=float(os.getenv("FALLBACK_AI_CONFIDENCE", "0.60")))
    # If AI omits confidence, force HOLD instead of injecting a fabricated value
    ai_missing_confidence_hold: bool = Field(default=os.getenv("AI_MISSING_CONFIDENCE_HOLD", "true").lower() == "true")
    # Net-of-cost modelling injected into the AI prompt
    expected_slippage_pct: float = Field(default=float(os.getenv("EXPECTED_SLIPPAGE_PCT", "0.0005")))
    # Confidence bounds advertised to the AI prompt (the gate is min_ai_confidence)
    ai_conf_floor: float = Field(default=float(os.getenv("AI_CONF_FLOOR", "0.50")))
    max_ai_confidence: float = Field(default=float(os.getenv("MAX_AI_CONFIDENCE", "0.99")))
    # Fee cache TTL for live trading-fee lookups
    # Minimum-notional safety buffer when sizing orders
    # Last-resort ATR fallback (% of price) when candles are unavailable — logged as synthetic
    # Opt-in override for the validated edge gate: allow BULLISH trend when no NEUTRAL
    # candidates exist (default False keeps the validated edge intact).
    edge_allow_bullish_fallback: bool = Field(default=os.getenv("EDGE_ALLOW_BULLISH_FALLBACK", "false").lower() == "true")
    # Fallback rule-engine ATR multipliers (used only when the LLM path is unavailable)
    fallback_sl_atr_mult: float = Field(default=float(os.getenv("FALLBACK_SL_ATR_MULT", "1.8")))
    fallback_tp1_atr_mult: float = Field(default=float(os.getenv("FALLBACK_TP1_ATR_MULT", "2.8")))
    fallback_tp2_atr_mult: float = Field(default=float(os.getenv("FALLBACK_TP2_ATR_MULT", "4.2")))
    # FLEX GUARD (opsi 3 + single-coin flexible): grid-ramping floor, affordability
    # fractions, and the single-coin bypass. All env-overridable, never literals.
    grid_ramp_min_grids: int = Field(default=int(os.getenv("GRID_RAMP_MIN_GRIDS", "2")))
    affordable_investment_frac: float = Field(default=float(os.getenv("AFFORDABLE_INVESTMENT_FRAC", "0.75")))
    affordable_free_frac: float = Field(default=float(os.getenv("AFFORDABLE_FREE_FRAC", "0.45")))
    single_coin_flexible: bool = Field(default=os.getenv("SINGLE_COIN_FLEXIBLE", "true").lower() in ("1", "true", "yes", "on"))

    # ========================================================================
    # P0: Quant Engine Parameters (env-overridable, default updated per P0)
    # ========================================================================
    # Gate 1 - Reclaim Strength: (price-EMA20)/EMA20 >= MIN_RECLAIM_PCT
    # P0: reclaim 0.0008 (0.08%)
    min_reclaim_pct: float = Field(default=float(os.getenv("MIN_RECLAIM_PCT", "0.0008")))
    # Gate 2 - Volume Confirmation: volRatio >= MIN_ENTRY_VOL_RATIO
    # P0: volRatio 0.8, vol_score_penalty_pct 8.0
    min_entry_vol_ratio: float = Field(default=float(os.getenv("MIN_ENTRY_VOL_RATIO", "0.7")))
    vol_score_penalty_pct: float = Field(default=float(os.getenv("VOL_SCORE_PENALTY_PCT", "8.0")))
    # Gate 3 - AI Failover: failover_quant_bump 10.0
    failover_quant_bump: float = Field(default=float(os.getenv("FAILOVER_QUANT_BUMP", "10.0")))
    # Score cutoff: P0 min_quant_score = 58
    min_quant_score: float = Field(default=float(os.getenv("MIN_QUANT_SCORE", "62.0")))
    # AI caution: P0 ai_caution_min_confidence = 0.2
    ai_caution_min_confidence: float = Field(default=float(os.getenv("AI_CAUTION_MIN_CONFIDENCE", "0.2")))
    # BUG 4 FIX: when quant says LONG but the AI only shrugs (HOLD below the
    # caution floor), allow the entry at this reduced sizing fraction instead of
    # dropping to CONSOLIDATION-HOLD. A weak AI non-vote must not veto a strong
    # measured quant signal.
    quant_override_sizing_mult: float = Field(default=float(os.getenv("QUANT_OVERRIDE_SIZING_MULT", "0.5")))
    # P0: min_vol_score_penalty (deprecated alias, kept for compatibility)
    min_vol_score_penalty: float = Field(default=float(os.getenv("MIN_VOL_SCORE_PENALTY", "8.0")))
    # P0.1 & P0.2 Guard toggles (env-overridable)
    hard_reject_consolidation: bool = Field(default=os.getenv("HARD_REJECT_CONSOLIDATION", "true").lower() in ("1", "true", "yes", "on"))
    hard_gate_volume: bool = Field(default=os.getenv("HARD_GATE_VOLUME", "true").lower() in ("1", "true", "yes", "on"))

    # ========================================================================
    # P1: Trading Strategy Parameters (env-overridable)
    # ========================================================================
    # Volume surge boost: +5 to score when volRatio >= 2.0
    volume_surge_boost: float = Field(default=float(os.getenv("VOLUME_SURGE_BOOST", "5.0")))
    volume_surge_threshold: float = Field(default=float(os.getenv("VOLUME_SURGE_THRESHOLD", "2.0")))
    # Trailing SL/TP config: P1 trailing_stop_trigger_pct=0.003, trailing_stop_delta_pct=0.0015
    trailing_stop_trigger_pct: float = Field(default=float(os.getenv("TRAILING_STOP_TRIGGER_PCT", "0.003")))
    trailing_stop_delta_pct: float = Field(default=float(os.getenv("TRAILING_STOP_DELTA_PCT", "0.0015")))
    # Pyramiding logic: P1 50% entry, 30% @0.5xATR, 20% @1.0xATR
    pyramid_entry_pct: float = Field(default=float(os.getenv("PYRAMID_ENTRY_PCT", "0.50")))
    pyramid_stage1_pct: float = Field(default=float(os.getenv("PYRAMID_STAGE1_PCT", "0.30")))
    pyramid_stage2_pct: float = Field(default=float(os.getenv("PYRAMID_STAGE2_PCT", "0.20")))
    pyramid_stage1_atr_mult: float = Field(default=float(os.getenv("PYRAMID_STAGE1_ATR_MULT", "0.5")))
    pyramid_stage2_atr_mult: float = Field(default=float(os.getenv("PYRAMID_STAGE2_ATR_MULT", "1.0")))
    # PYRAMID AUTO-PLACE KILL-SWITCH: OFF by default. Staged adds @0.5/1.0xATR only fire
    # when PYRAMIDING_AUTO=true. Deploy OFF so nothing auto-adds without explicit consent.
    pyramiding_auto: bool = Field(default=os.getenv("PYRAMIDING_AUTO", "false").lower() in ("1", "true", "yes", "on"))
    # TP ROTATION (opt-in): after a TP1 fill, lock the runner at adaptive BE+ and free
    # the slot/capital for a fresh pick without waiting for the runner to close.
    # OFF by default — explicit consent required. All env-overridable, never literals.
    tp_rotation_enabled: bool = Field(default=os.getenv("TP_ROTATION_ENABLED", "false").lower() in ("1", "true", "yes", "on"))
    tp_cooldown_sec: int = Field(default=int(os.getenv("TP_COOLDOWN_SEC", "300")))
    tp_rotation_retry_cooldown_sec: int = Field(default=int(os.getenv("TP_ROTATION_RETRY_COOLDOWN_SEC", "60")))
    tp_rotation_global_cooldown_sec: int = Field(default=int(os.getenv("TP_ROTATION_GLOBAL_COOLDOWN_SEC", "60")))
    max_tp_rotations_per_day: int = Field(default=int(os.getenv("MAX_TP_ROTATIONS_PER_DAY", "8")))
    # Adaptive BE+ buffer multiplier on live ATR for the rotation lock
    # (buffer = max(venue tick, k*ATR_live, round-trip fee); env-overridable).
    tp_rotation_be_atr_mult: float = Field(default=float(os.getenv("TP_ROTATION_BE_ATR_MULT", "0.15")))
    # Adaptive breakeven+ SL buffer for each staged add: buffer = max(k*ATR_live, venue tick/step).
    # Never a dead number — scales off live ATR, floored by exchangeInfo granularity.
    pyramid_sl_buffer_atr_mult: float = Field(default=float(os.getenv("SL_BUFFER_ATR_MULT", "0.15")))
    # Position scaling: P1 50% / 100%
    position_scaling_low: float = Field(default=float(os.getenv("POSITION_SCALING_LOW", "0.50")))
    position_scaling_high: float = Field(default=float(os.getenv("POSITION_SCALING_HIGH", "1.00")))

    # ========================================================================
    # P2: Risk Management Parameters (env-overridable)
    # ========================================================================
    # Dynamic R:R based on ATR (target R:R multiplier for TP distance)
    # Position scaling 2% risk fixed dollar (min_risk_per_trade = 0.02)
    min_risk_per_trade: float = Field(default=float(os.getenv("MIN_RISK_PER_TRADE", "0.02")))
    # Fixed dollar risk cap for scaling (e.g., $50 per trade)
    risk_cap_usd: float = Field(default=float(os.getenv("RISK_CAP_USD", "50.0")))
    # Dynamic fee adjustment based on volRatio (fee_multiplier = 1 + 0.2 * (volRatio - 1), max 1.4)
    fee_multiplier_base: float = Field(default=float(os.getenv("FEE_MULTIPLIER_BASE", "1.0")))
    fee_multiplier_vol_factor: float = Field(default=float(os.getenv("FEE_MULTIPLIER_VOL_FACTOR", "0.2")))
    fee_multiplier_max: float = Field(default=float(os.getenv("FEE_MULTIPLIER_MAX", "1.4")))


config = BotConfig()
