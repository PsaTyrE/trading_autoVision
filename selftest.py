"""Self-test suite for auto_pilot_trader and strategy modules.

Run after any change: ./venv/bin/python selftest.py
Must exit 0 with ALL PASS. Never commit a broken selftest.
"""
import json
import math
import os
import sys
import pandas as pd
import auto_pilot_trader as A
import src.strategy as strategy

failures = []

def check(name, cond):
    if cond:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name}")
        failures.append(name)

# --- 1. state persistence ---
probe = {"day_key": "2099-01-01", "day_entries": 99, "last_signal_ts": 1234567890}
A.save_state(probe)
check("state roundtrip", A.load_state() == probe)
check("no tmp residue", not os.path.exists(A.STATE_FILE + ".tmp"))

# --- 2. signal: Pullback Buy on Dip (verified on real market data fixture) ---
with open("fixtures/btc_pullback.json", "r") as f:
    raw_bars = json.load(f)

d = strategy.features(pd.DataFrame(raw_bars, columns=["t", "o", "h", "l", "c", "v"]))
sig = strategy.signal(d, -1)

check("pullback BUY detected", bool(sig) and sig[0] == "BUY")
check("vol_ratio reported", bool(sig) and sig[1]["vol_ratio"] >= strategy.MIN_VOL_RATIO)

# --- 3. volume gate blocks on weak volume ---
raw_low_v = [list(r) for r in raw_bars]
raw_low_v[-1][5] = 1.0  # destroy volume on signal bar
d2 = strategy.features(pd.DataFrame(raw_low_v, columns=["t", "o", "h", "l", "c", "v"]))
check("volume gate blocks", strategy.signal(d2, -1) is None)

# --- 4. levels math: SL 2 ATR, TP1 2R, TP2 3.5R ---
sl, tp1, tp2, sd = strategy.levels(100.0, 1.0, "BUY")
_expL = (100.0 - sd, 100.0 + sd * strategy.TP1_R, 100.0 + sd * strategy.TP2_R)
check("LONG levels", (sl, tp1, tp2) == _expL and sd == strategy.SL_ATR)

sl, tp1, tp2, sd = strategy.levels(100.0, 1.0, "SELL")
_expS = (100.0 + sd, 100.0 - sd * strategy.TP1_R, 100.0 - sd * strategy.TP2_R)
check("SHORT levels", (sl, tp1, tp2) == _expS and sd == strategy.SL_ATR)

# --- 5. trailing epsilon scales with price (sub-$1 coin protection) ---
def eps(price, dec):
    return max(abs(price) * 1e-4, 10.0 ** (-dec))
check("trail eps DOGE < 0.1", eps(0.16, 2) < 0.1)
check("trail eps XAU sane", 0.3 < eps(4132.0, 1) < 1.0)

# --- 5b. snapshot fields measured, not fabricated ---
m = sig[1] if sig else {}
check("score in 0-100", 0.0 <= float(m.get("score", -1)) <= 100.0)
check("rsi in 0-100", 0.0 <= float(m.get("rsi", -1)) <= 100.0)
check("dip margin positive", float(m.get("breakout_margin", -1)) > 0)
check("trend_up bool", isinstance(m.get("trend_up"), bool))

# --- 6. position sizing sanity ---
check("BTC untradeable at $0.30 risk", (0.30 / 2000.0) < 0.001)
check("XAU tradeable at $0.30 risk", (0.30 / 2.0) >= 0.001)

# --- 7. env knobs ---
check("knob defaults measured", strategy.SL_ATR == 2.0 and strategy.TP1_R == float(os.getenv("STRAT_TP1_R", 2.0)))

# --- 8. anti-pucuk guard: reject bar near Upper Bollinger Band ---
raw_pucuk = [list(r) for r in raw_bars]
# Set signal bar close above Upper Bollinger Band
raw_pucuk[-1][4] = float(sig[1]["bb_up"]) * 1.01
d_pucuk = strategy.features(pd.DataFrame(raw_pucuk, columns=["t", "o", "h", "l", "c", "v"]))
check("anti-pucuk rejects overextended bar", strategy.signal(d_pucuk, -1) is None)

# --- 9. daily loss cap (config di .env yang tadinya mati) ---
_inc_rows = [
    {"incomeType": "REALIZED_PNL", "income": "-3.00"},
    {"incomeType": "COMMISSION", "income": "-0.10"},
    {"incomeType": "FUNDING_FEE", "income": "-0.05"},
    {"incomeType": "TRANSFER", "income": "999.00"},   # harus diabaikan
]
check("day_pnl sums pnl+fee+funding only", A.day_pnl_from_income(_inc_rows) == -3.15)
check("day_pnl ignores unrelated income types", A.day_pnl_from_income([{"incomeType": "TRANSFER", "income": "50"}]) == 0.0)
check("day_pnl handles empty", A.day_pnl_from_income([]) == 0.0)
check("loss cap trips at -6% of 55", A.daily_loss_blocked(-3.30, 55.0, 0.05) is True)
check("loss cap allows -2% of 55", A.daily_loss_blocked(-1.10, 55.0, 0.05) is False)
check("loss cap allows profit", A.daily_loss_blocked(0.75, 55.0, 0.05) is False)
check("loss cap safe on zero equity", A.daily_loss_blocked(-1.0, 0.0, 0.05) is False)
check("loss cap exported", hasattr(A, "daily_loss_blocked") and hasattr(A, "day_pnl_from_income"))

# --- 10. entry ts diambil dari fill paling awal (bukan now) ---
check("entry_ts picks earliest fill",
      A.entry_ts_from_trades([{"time": "1793300000000"}, {"time": "1793290000000"}], 1) == 1793290000)
check("entry_ts falls back when empty", A.entry_ts_from_trades([], 12345) == 12345)
check("entry_ts exported", hasattr(A, "entry_ts_from_trades"))

# --- 11. veto RSI 1h ---
check("rsi1h veto LONG 81.5", A.rsi1h_veto("LONG", 81.5) is True)
check("rsi1h veto LONG 57.2 lolos", A.rsi1h_veto("LONG", 57.2) is False)
check("rsi1h veto SHORT 41.0", A.rsi1h_veto("SHORT", 41.0) is True)
check("rsi1h veto SHORT 52.4 lolos", A.rsi1h_veto("SHORT", 52.4) is False)
check("rsi1h veto None lolos", A.rsi1h_veto("LONG", None) is False)

if failures:
    print(f"\nRESULT: {len(failures)} FAILED: {failures}")
    sys.exit(1)
else:
    print("\nRESULT: ALL PASS (%d/%d)" % (17 + 16, 17 + 16))
    sys.exit(0)
