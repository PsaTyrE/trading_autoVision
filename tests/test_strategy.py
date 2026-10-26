"""Runnable self-check.  cd /home/pi/latihan && ./venv/bin/python tests/test_strategy.py"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
from src import strategy


def frame(n=400, breakout_at=None, direction="BUY", vol=400.0):
    """Random walk + 18-bar flat squeeze (constant closes) right before breakout_at."""
    rng = np.random.default_rng(7)
    px = 100 + np.cumsum(rng.normal(0, 0.05, n))
    rows = []
    for i in range(n):
        o = c = px[i]
        rows.append([i * 14400000, o, max(o, c) + 1.0, min(o, c) - 1.0, c, 100.0])
    d = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c", "v"])
    if breakout_at is not None:
        base = float(d.c.iloc[breakout_at - 20])
        for i in range(breakout_at - 18, breakout_at):
            d.loc[i, "o"] = base
            d.loc[i, "c"] = base          # constant closes -> BBW collapses
            d.loc[i, "h"] = base + 0.01
            d.loc[i, "l"] = base - 0.01
            d.loc[i, "v"] = 100.0
        i = breakout_at
        d.loc[i, "v"] = vol
        if direction == "BUY":
            hi = d.h.iloc[i - 20:i].max()
            d.loc[i, "o"] = hi - 0.5
            d.loc[i, "c"] = hi + 1.0
            d.loc[i, "h"] = hi + 1.2
            d.loc[i, "l"] = hi - 0.6
        else:
            lo = d.l.iloc[i - 20:i].min()
            d.loc[i, "o"] = lo + 0.5
            d.loc[i, "c"] = lo - 1.0
            d.loc[i, "l"] = lo - 1.2
            d.loc[i, "h"] = lo + 0.6
    return d


f = strategy.features(frame())
assert {"atr", "vma", "bbw", "bwq", "hh", "ll"} <= set(f.columns)
assert len(f) == 400

assert strategy.signal(strategy.features(frame(150))) is None, "too few bars must be None"

sig = strategy.signal(strategy.features(frame(breakout_at=300, direction="BUY")), 300)
assert sig is not None, "expected BUY signal"
side, meta = sig
assert side == "BUY", side
assert meta["vol_ratio"] > 1.0
assert meta["atr"] > 0

sig2 = strategy.signal(strategy.features(frame(breakout_at=300, direction="SELL")), 300)
assert sig2 is not None and sig2[0] == "SELL", sig2

d3 = strategy.features(frame(breakout_at=300, direction="BUY", vol=1.0))
assert strategy.signal(d3, 300) is None, "dead volume must be rejected"

d4 = strategy.features(frame(breakout_at=300, direction="BUY"))
d4.loc[299, "c"] = d4.c.iloc[299]
assert strategy.signal(d4, 300) is not None

sl, tp1, tp2, sd = strategy.levels(100.0, 1.0, "BUY")
assert sl < 100 < tp1 < tp2, (sl, tp1, tp2)
assert abs(sd - strategy.SL_ATR) < 1e-9
assert abs((tp1 - 100) / sd - strategy.TP1_R) < 1e-9
assert abs((tp2 - 100) / sd - strategy.TP2_R) < 1e-9

sl, tp1, tp2, sd = strategy.levels(100.0, 1.0, "SELL")
assert sl > 100 > tp1 > tp2, (sl, tp1, tp2)

print("OK: all assertions passed")
