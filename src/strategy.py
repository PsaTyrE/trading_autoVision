"""Entry: Trend-Following Pullback / Buy on Dip (Anti-Pucuk, Anti-Dasar).

Core Logic:
  - BUY (Dip in Uptrend):
      1. Macro trend: Close > EMA50 and EMA50 rising.
      2. Healthy Pullback: RSI between 38 and 56 (not overbought, healthy retrace).
      3. Value Zone: Price pulling back to / testing dynamic support (EMA50 or BB_MID or VWAP).
      4. Anti-Pucuk Ceiling: Price must be >= 1.5% below Upper Bollinger Band (c <= bb_up * 0.985).
      5. Price Action Confirmation: Bullish reversal candle (Close > Open) with lower wick rejection.
      6. Volume: Volume ratio >= MIN_VOL_RATIO (0.9x).
      7. SMC Guard: No bearish liquidity sweep rejection at highs.

  - SELL (Rally in Downtrend):
      1. Macro trend: Close < EMA50 and EMA50 falling.
      2. Healthy Retrace: RSI between 44 and 62 (not oversold).
      3. Value Zone: Price pulling back to / testing dynamic resistance (EMA50 or BB_MID).
      4. Anti-Dasar Floor: Price must be >= 1.5% above Lower Bollinger Band (c >= bb_lo * 1.015).
      5. Price Action Confirmation: Bearish reversal candle (Close < Open) with upper wick rejection.
      6. Volume: Volume ratio >= MIN_VOL_RATIO (0.9x).
      7. SMC Guard: No bullish liquidity sweep rejection at lows.
"""
import os
import pandas as pd

def _num(name, default):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[strategy] ignoring non-numeric {name}={raw!r}", flush=True)
        return default

TF = os.getenv("STRAT_TF", "4h")
SL_ATR = _num("STRAT_SL_ATR", 2.0)
TP1_R = _num("STRAT_TP1_R", 2.0)
TP2_R = _num("STRAT_TP2_R", 3.5)
BE_AT_R = _num("STRAT_BE_AT_R", 1.0)
MIN_BARS = int(_num("STRAT_MIN_BARS", 120))
MIN_VOL_RATIO = _num("STRAT_MIN_VOL_RATIO", 1.0)

def features(df):
    d = df.copy()
    tr = pd.concat(
        [d.h - d.l, (d.h - d.c.shift()).abs(), (d.l - d.c.shift()).abs()], axis=1
    ).max(axis=1)
    d["atr"] = tr.rolling(14).mean()
    d["vma"] = d.v.rolling(20).mean()
    
    # Trend & Support/Resistance Indicators
    d["ema50"] = d.c.ewm(span=50, adjust=False).mean()
    _up = d.c.diff().clip(lower=0).rolling(14).mean()
    _dn = (-d.c.diff().clip(upper=0)).rolling(14).mean()
    d["rsi"] = 100 - 100 / (1 + _up / _dn)
    
    # Bollinger Bands
    d["bb_mid"] = d.c.rolling(20).mean()
    _bs = d.c.rolling(20).std()
    d["bb_up"] = d.bb_mid + 2 * _bs
    d["bb_lo"] = d.bb_mid - 2 * _bs
    d["bbw"] = (d.bb_up - d.bb_lo) / d.c
    d["bwq"] = d.bbw.rolling(100).quantile(0.25)
    
    # Swing extremes for reference
    d["hh"] = d.h.rolling(20).max().shift(1)
    d["ll"] = d.l.rolling(20).min().shift(1)

    # MACD
    _e12 = d.c.ewm(span=12, adjust=False).mean()
    _e26 = d.c.ewm(span=26, adjust=False).mean()
    d["macd"] = _e12 - _e26
    d["macd_sig"] = d.macd.ewm(span=9, adjust=False).mean()
    d["macd_hist"] = d.macd - d.macd_sig

    # VWAP
    _tp = (d.h + d.l + d.c) / 3.0
    _pv = _tp * d.v
    d["vwap"] = _pv.rolling(20).sum() / d.v.rolling(20).sum().replace(0, 1e-9)

    # SMC: Liquidity Sweep detection
    _h = d.h.values
    _l = d.l.values
    _c = d.c.values
    _n = len(d)
    _fvg_bias = ["neutral"] * _n
    _sweep_bias = ["none"] * _n
    _fvg_gap_top = [float("nan")] * _n
    _fvg_gap_bot = [float("nan")] * _n

    for i in range(2, _n):
        if _l[i] > _h[i - 2]:
            _fvg_bias[i] = "bullish_fvg"
            _fvg_gap_top[i] = _l[i]
            _fvg_gap_bot[i] = _h[i - 2]
        elif _h[i] < _l[i - 2]:
            _fvg_bias[i] = "bearish_fvg"
            _fvg_gap_top[i] = _l[i - 2]
            _fvg_gap_bot[i] = _h[i]

        _win_h = _h[max(0, i - 10):i]
        _win_l = _l[max(0, i - 10):i]
        if len(_win_h) > 0 and len(_win_l) > 0:
            _prior_h = float(_win_h.max())
            _prior_l = float(_win_l.min())
            if _h[i] > _prior_h and _c[i] < _prior_h:
                _sweep_bias[i] = "bearish_liquidity_sweep"
            elif _l[i] < _prior_l and _c[i] > _prior_l:
                _sweep_bias[i] = "bullish_liquidity_sweep"

    d["smc_fvg"] = _fvg_bias
    d["smc_sweep"] = _sweep_bias
    d["fvg_top"] = _fvg_gap_top
    d["fvg_bot"] = _fvg_gap_bot
    return d

def signal(d, i=-1):
    """(side, meta) or None. Pullback / Buy on Dip signal generator."""
    if len(d) < MIN_BARS:
        return None
    if pd.isna(d.atr.iloc[i]) or pd.isna(d.vma.iloc[i]) or pd.isna(d.rsi.iloc[i]):
        return None
    if d.atr.iloc[i] <= 0:
        return None
    if d.v.iloc[i] < d.vma.iloc[i] * MIN_VOL_RATIO:
        return None

    c = float(d.c.iloc[i])
    o = float(d.o.iloc[i])
    h = float(d.h.iloc[i])
    l = float(d.l.iloc[i])
    atr = float(d.atr.iloc[i])
    rsi = float(d.rsi.iloc[i])
    ema50 = float(d.ema50.iloc[i])
    ema50_prev = float(d.ema50.iloc[i - 1])
    bb_up = float(d.bb_up.iloc[i])
    bb_lo = float(d.bb_lo.iloc[i])
    bb_mid = float(d.bb_mid.iloc[i])
    vwap = float(d.vwap.iloc[i])
    sweep = str(d.smc_sweep.iloc[i])
    vr = float(d.v.iloc[i] / d.vma.iloc[i])

    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    body = max(1e-9, abs(c - o))

    is_uptrend = (c > ema50) and (ema50 >= ema50_prev)
    is_downtrend = (c < ema50) and (ema50 <= ema50_prev)

    side = None
    dip_margin = 0.0

    # 1. BUY PULLBACK (DIP)
    if is_uptrend and (36.0 <= rsi <= 58.0) and (c <= bb_up * 0.985):
        if sweep == "bearish_liquidity_sweep":
            return None  # Sweep high rejection -> DILARANG BUY
        # Retest zone: price touches or is within 2.5% of EMA50, BB_MID, or VWAP
        near_ema = abs(c - ema50) / c <= 0.03 or (l <= ema50 and c >= ema50)
        near_bb_mid = abs(c - bb_mid) / c <= 0.025 or (l <= bb_mid and c >= bb_mid)
        near_vwap = abs(c - vwap) / c <= 0.025 or (l <= vwap and c >= vwap)
        
        # Bullish confirmation candle (green candle or strong lower wick rejection)
        bullish_confirm = (c > o) and (lower_wick >= body * 0.35 or c > float(d.c.iloc[i - 1]))
        if (near_ema or near_bb_mid or near_vwap) and bullish_confirm:
            side = "BUY"
            dip_margin = max(0.0, bb_up - c)

    # 2. SELL PULLBACK (RALLY TO RESISTANCE)
    elif is_downtrend and (42.0 <= rsi <= 64.0) and (c >= bb_lo * 1.015):
        if sweep == "bullish_liquidity_sweep":
            return None  # Sweep low rejection -> DILARANG SELL
        near_ema = abs(c - ema50) / c <= 0.03 or (h >= ema50 and c <= ema50)
        near_bb_mid = abs(c - bb_mid) / c <= 0.025 or (h >= bb_mid and c <= bb_mid)
        near_vwap = abs(c - vwap) / c <= 0.025 or (h >= vwap and c <= vwap)

        bearish_confirm = (c < o) and (upper_wick >= body * 0.35 or c < float(d.c.iloc[i - 1]))
        if (near_ema or near_bb_mid or near_vwap) and bearish_confirm:
            side = "SELL"
            dip_margin = max(0.0, c - bb_lo)

    if not side:
        return None

    # Score: based on healthy RSI position, bounce quality, and volume confirmation
    rsi_quality = (1.0 - abs(rsi - 48.0) / 20.0)  # best around 48
    score = 50.0 + 25.0 * min(2.0, max(0.0, vr - 0.9)) + 25.0 * max(0.0, min(1.0, rsi_quality))

    return side, {
        "atr": atr,
        "close": c,
        "vol_ratio": vr,
        "bar_ts": int(d.t.iloc[i]),
        "rsi": rsi,
        "ema50": ema50,
        "trend_up": bool(c > ema50),
        "breakout_margin": dip_margin,
        "score": round(min(100.0, max(0.0, score)), 1),
        "bb_up": bb_up,
        "bb_lo": bb_lo,
        "bb_mid": bb_mid,
        "macd_hist": float(d.macd_hist.iloc[i]),
        "macd_prev": float(d.macd_hist.iloc[i - 1]),
        "ema50_prev": ema50_prev,
        "vwap": vwap,
        "vwap_aligned": bool(c > vwap) if side == "BUY" else bool(c < vwap),
        "smc_fvg": str(d.smc_fvg.iloc[i]),
        "smc_sweep": sweep,
        "fvg_top": float(d.fvg_top.iloc[i]) if not pd.isna(d.fvg_top.iloc[i]) else None,
        "fvg_bot": float(d.fvg_bot.iloc[i]) if not pd.isna(d.fvg_bot.iloc[i]) else None,
    }

def levels(entry, atr, side):
    """(sl, tp1, tp2, sl_dist) raw floats, unrounded."""
    sd = SL_ATR * atr
    s = 1 if side == "BUY" else -1
    return entry - s * sd, entry + s * sd * TP1_R, entry + s * sd * TP2_R, sd
