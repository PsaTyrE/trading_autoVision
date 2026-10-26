import pandas as pd
import numpy as np
from typing import Dict, Any
from src.config import config

def _sig_round(x: float) -> float:
    """
    Magnitude-agnostic rounding for price-scale values (price, EMA, MACD, BB, ATR).

    Fixed `round(x, 4)` is only correct near $1: for a sub-$0.10 asset it quantises the ATR
    (e.g. GMT 3.9e-05, JASMY 2.6e-05) straight to 0.0 — the entry guard then reads that as
    "ATR unavailable" and skips a perfectly tradable coin, and it distorts the reference price
    by up to ~0.5%. Significant-figure rounding is correct at every magnitude. Digits come from
    config (env-overridable), never a literal.
    """
    x = float(x)
    if not np.isfinite(x) or x == 0.0:
        return 0.0
    return round(x, int(getattr(config, "indicator_price_sig_digits", 8)) - 1 - int(np.floor(np.log10(abs(x)))))

def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Computes core technical indicators on OHLCV DataFrame using Pandas & NumPy.
    Expected columns: ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    """
    df = df.copy()

    # 1. Exponential Moving Averages (EMA)
    df['ema_20'] = df['close'].ewm(span=int(getattr(config, "ema_fast", 20)), adjust=False).mean()
    df['ema_50'] = df['close'].ewm(span=int(getattr(config, "ema_slow", 50)), adjust=False).mean()
    df['ema_200'] = df['close'].ewm(span=int(getattr(config, "ema_trend", 200)), adjust=False).mean()

    # 2. Relative Strength Index (RSI - Wilder smoothing)
    rsi_p = int(getattr(config, "rsi_period", 14))
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/rsi_p, adjust=False, min_periods=rsi_p).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/rsi_p, adjust=False, min_periods=rsi_p).mean()
    rs = gain / (loss + 1e-10)
    df['rsi'] = 100 - (100 / (1 + rs))

    # 3. MACD (12, 26, 9)
    macd_fast = int(getattr(config, "macd_fast", 12))
    macd_slow = int(getattr(config, "macd_slow", 26))
    macd_sig = int(getattr(config, "macd_signal", 9))
    ema_12 = df['close'].ewm(span=macd_fast, adjust=False).mean()
    ema_26 = df['close'].ewm(span=macd_slow, adjust=False).mean()
    df['macd_line'] = ema_12 - ema_26
    df['macd_signal'] = df['macd_line'].ewm(span=macd_sig, adjust=False).mean()
    df['macd_hist'] = df['macd_line'] - df['macd_signal']

    # 4. Average True Range (ATR) for dynamic SL/TP & volatility
    atr_p = int(getattr(config, "atr_period", 14))
    high_low = df['high'] - df['low']
    high_close_prev = (df['high'] - df['close'].shift(1)).abs()
    low_close_prev = (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([high_low, high_close_prev, low_close_prev], axis=1).max(axis=1)
    df['atr'] = tr.ewm(alpha=1/atr_p, adjust=False, min_periods=atr_p).mean()
    vol_win = int(getattr(config, "volatility_window", 20))
    df['atr_avg_20'] = df['atr'].rolling(window=vol_win).mean()
    df['atr_ratio'] = df['atr'] / (df['atr_avg_20'] + 1e-10)

    # 5. Bollinger Bands
    bb_p = int(getattr(config, "bb_period", 20))
    bb_std_mult = float(getattr(config, "bb_std_mult", 2.0))
    df['bb_mid'] = df['close'].rolling(window=bb_p).mean()
    bb_std = df['close'].rolling(window=bb_p).std()
    df['bb_upper'] = df['bb_mid'] + (bb_std * bb_std_mult)
    df['bb_lower'] = df['bb_mid'] - (bb_std * bb_std_mult)
    df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / (df['bb_mid'] + 1e-10)

    # 6. Volume Moving Average
    df['volume_sma_20'] = df['volume'].rolling(window=vol_win).mean()
    df['volume_ratio'] = df['volume'] / (df['volume_sma_20'] + 1e-10)

    return df

def extract_market_snapshot(df: pd.DataFrame, symbol: str, timeframe: str) -> Dict[str, Any]:
    """
    Extracts the latest market metrics and recent price action into a clean summary
    ready to be consumed by the AI reasoning model.

    P3: Signals are computed from the LAST CLOSED candle only. The final row of a live
    Binance OHLCV pull is the still-forming candle, whose RSI/ATR/EMA (and thus
    is_falling_micro / MACD crosses) mutate intra-candle — a classic 'repaint' source
    that triggers entries that would never have existed at candle close.
    """
    if len(df) < 2:
        return {}

    # Drop the in-progress candle unless explicitly disabled.
    if getattr(config, "use_closed_candle_only", True):
        df = df.iloc[:-1]

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    # Recent 10 candles high and low for local support/resistance
    recent_10 = df.iloc[-10:]
    recent_high = float(recent_10['high'].max())
    recent_low = float(recent_10['low'].min())

    # Trend determination
    ema_20 = float(latest['ema_20'])
    ema_50 = float(latest['ema_50'])
    ema_200 = float(latest['ema_200']) if not np.isnan(latest['ema_200']) else None
    current_price = float(latest['close'])

    trend = "BULLISH" if current_price > ema_20 > ema_50 else (
        "BEARISH" if current_price < ema_20 < ema_50 else "NEUTRAL / CONSOLIDATION"
    )

    # Real Price Action Confirmation
    is_candle_green = bool(latest['close'] >= latest['open'])
    is_falling_micro = bool(latest['close'] < prev['close'] and latest['low'] < prev['low'])

    rsi_val = round(float(latest['rsi']), 2) if not np.isnan(latest['rsi']) else 50.0
    rsi_condition = "OVERBOUGHT_DANGER" if rsi_val >= 72.0 else ("OVERSOLD_BOUNCE" if rsi_val <= 32.0 else "HEALTHY_NORMAL")

    bb_upper = _sig_round(float(latest['bb_upper'])) if not np.isnan(latest['bb_upper']) else _sig_round(current_price * 1.02)
    bb_mid = _sig_round(float(latest['bb_mid'])) if not np.isnan(latest['bb_mid']) else _sig_round(current_price)
    bb_lower = _sig_round(float(latest['bb_lower'])) if not np.isnan(latest['bb_lower']) else _sig_round(current_price * 0.98)
    bb_condition = "ABOVE_UPPER_BAND" if current_price >= bb_upper else ("BELOW_LOWER_BAND" if current_price <= bb_lower else "INSIDE_BANDS")

    vol_ratio = round(float(latest['volume_ratio']), 2) if not np.isnan(latest['volume_ratio']) else 1.0
    vol_condition = "HIGH_SURGE" if vol_ratio >= 1.2 else ("LOW_VOLUME_DANGER" if vol_ratio < 0.7 else "NORMAL")

    # Volatility regime: ATR(now) vs ATR(20-avg). >threshold = market still expanding
    # (post-news wick/chop) -> risky to enter; <=threshold = settled.
    # Guard with isfinite (not just isnan): a divide-by-(near)zero ATR baseline can yield inf,
    # which passed isnan and then crashed FastAPI's JSON encoder ("Out of range float: inf").
    _ar = latest['atr_ratio'] if 'atr_ratio' in latest else float('nan')
    try:
        _ar = float(_ar)
    except Exception:
        _ar = float('nan')
    _ar_known = bool(np.isfinite(_ar) and _ar > 0)
    atr_ratio = round(_ar, 3) if _ar_known else 1.0
    atr_expand_threshold = float(getattr(config, "volatility_gate_atr_ratio", 1.5))
    # Unknown/degenerate ATR is NOT evidence the tape settled. Report the regime CONSERVATIVE
    # (expanding=True) so the post-news gate HOLDS new entries instead of allowing them on a
    # numeric default. Only a genuine finite measurement may report "calm".
    volatility_expanding = bool(atr_ratio > atr_expand_threshold) if _ar_known else True

    prev_close = float(prev.get('close', 0.0) or 0.0)
    pct_change = round(float((latest['close'] - prev_close) / prev_close * 100), 2) if prev_close > 0 else 0.0

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "current_price": _sig_round(current_price),
        "price_change_last_candle_pct": pct_change,
        "trend_bias": trend,
        "price_action": {
            "is_candle_green": is_candle_green,
            "is_falling_micro": is_falling_micro,
            "open": float(latest['open']),
            "close": float(latest['close']),
            "high": float(latest['high']),
            "low": float(latest['low']),
            "prev_close": float(prev['close']),
            "prev_low": float(prev['low'])
        },
        "indicators": {
            "rsi_14": rsi_val,
            "rsi_condition": rsi_condition,
            "ema_20": _sig_round(ema_20),
            "ema_50": _sig_round(ema_50),
            "ema_200": _sig_round(ema_200) if ema_200 else "Insufficient data (<200 candles)",
            "macd": {
                "line": _sig_round(float(latest['macd_line'])),
                "signal": _sig_round(float(latest['macd_signal'])),
                "histogram": _sig_round(float(latest['macd_hist'])),
                "status": "BULLISH_CROSS" if latest['macd_line'] > latest['macd_signal'] and prev['macd_line'] <= prev['macd_signal'] else (
                    "BEARISH_CROSS" if latest['macd_line'] < latest['macd_signal'] and prev['macd_line'] >= prev['macd_signal'] else "NORMAL"
                )
            },
            # AUDIT FIX: never fabricate an ATR when the indicator is unavailable. The old
            # `current_price * 0.015` produced a synthetic volatility that silently drove grid
            # geometry and stop distance. Report 0.0 so the entry guard treats the snapshot as
            # unusable instead of sizing real orders off invented volatility.
            "atr_14": _sig_round(float(latest['atr'])) if np.isfinite(latest['atr']) else 0.0,
            "atr_ratio": atr_ratio,
            "atr_expanding": volatility_expanding,
            "bollinger_bands": {
                "upper": bb_upper,
                "mid": bb_mid,
                "lower": bb_lower,
                "condition": bb_condition,
                "bandwidth_pct": round(float(latest['bb_width'] * 100), 2) if not np.isnan(latest['bb_width']) else 0.0
            },
            "volume_vs_average": vol_ratio,
            "volume_condition": vol_condition
        },
        "key_levels": {
            "recent_swing_high": recent_high,
            "recent_swing_low": recent_low
        }
    }

