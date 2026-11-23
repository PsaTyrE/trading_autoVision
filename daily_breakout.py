#!/usr/bin/env python3
"""Entry 1D breakout (8 koin besar) + manajemen posisi.

Backtest 2,7 tahun (2024-02..2026-11), n=198: exp +0,129R, WR 56%, PF 1,31,
positif di 3 dari 4 kuartal. Risk 1% (di bawah ambang +0,15R, jadi setengah size).

Pakai:
  ./venv/bin/python daily_breakout.py            # scan + entry kalau ada sinyal
  ./venv/bin/python daily_breakout.py --manage   # trail / time-exit posisi
  ./venv/bin/python daily_breakout.py --dry      # lihat sinyal tanpa order
"""
import json
import os
import sys
import time
from decimal import Decimal, ROUND_DOWN

import ccxt
import requests
from dotenv import load_dotenv

BASE = "/home/pi/latihan"
load_dotenv(os.path.join(BASE, ".env"), override=True)
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "src"))

from src.live_trader import BinanceFuturesTrader  # noqa: E402

SYMS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "LINK"]   # fallback
UNIVERSE_N = 22
MIN_BARS = 400
STATE = os.path.join(BASE, "daily_breakout_state.json")
LOG = os.path.join(BASE, "daily_breakout.log")
RISK_PCT = 0.02          # 2% equity per trade
MAX_OPEN = 8             # heat maks 16%
SL_ATR = 2.0
LEGS = [(1.0, .26), (1.2, .24), (1.4, .22), (1.7, .18)]
RUNNER = 0.10
TRAIL_AFTER = 2.0        # R
TRAIL_DIST = 0.75        # R
HOLD_DAYS = 20
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def tg(msg):
    if not TOKEN or not CHAT:
        return
    try:
        requests.post("https://api.telegram.org/bot%s/sendMessage" % TOKEN,
                      data={"chat_id": CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log("telegram gagal: %s" % str(e)[:80])


def build_universe(ex):
    """22 perp USDT volume terbesar yang punya >= MIN_BARS candle 1D."""
    tk = ex.fetch_tickers()
    cand = [(s, t.get("quoteVolume") or 0) for s, t in tk.items()
            if s.endswith("/USDT:USDT") and t.get("quoteVolume")]
    cand.sort(key=lambda x: -x[1])
    out = []
    for s, _ in cand[:70]:
        try:
            if len(ex.fetch_ohlcv(s, "1d", limit=MIN_BARS + 5)) >= MIN_BARS:
                out.append(s)
        except Exception:
            continue
        if len(out) >= UNIVERSE_N:
            break
    return out or [s + "/USDT:USDT" for s in SYMS]


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {}


def save_state(st):
    json.dump(st, open(STATE, "w", encoding="utf-8"), indent=1)


def ema(vals, n):
    k = 2 / (n + 1); e = vals[0]; out = [e]
    for x in vals[1:]:
        e = x * k + e * (1 - k); out.append(e)
    return out


def atr14(h, l, c):
    tr = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    a = sum(tr[:14]) / 14
    for x in tr[14:]:
        a = (a * 13 + x) / 14
    return a


def signal(ex, sym):
    """Sinyal di candle 1D yang sudah TUTUP (bar terakhir dibuang)."""
    oh = ex.fetch_ohlcv(sym, "1d", limit=120)
    if len(oh) < 60:
        return None
    oh = oh[:-1]                      # buang candle berjalan
    o = [b[1] for b in oh]; h = [b[2] for b in oh]; l = [b[3] for b in oh]
    c = [b[4] for b in oh]; v = [b[5] for b in oh]
    i = len(c) - 1
    e50 = ema(c, 50)[i]
    av = sum(v[i - 20:i]) / 20
    if av <= 0 or v[i] < 1.5 * av or c[i] <= max(h[i - 20:i]) or c[i] <= e50:
        return None
    a = atr14(h, l, c)
    if a <= 0:
        return None
    return {"sym": sym, "close": c[i], "atr": a, "bar_ts": oh[i][0],
            "volr": v[i] / av, "ema50": e50}


def floor_qty(ex, sym, qty):
    step = Decimal(str(ex.markets[sym]["precision"]["amount"] or "0.001"))
    d = Decimal(str(qty))
    return float((d / step).to_integral_value(rounding=ROUND_DOWN) * step)


def open_trade(t, ex, sig, dry):
    sym = sig["sym"]
    px = t.get_ticker(sym)["last"]
    r = SL_ATR * sig["atr"]
    sl = px - r
    equity = t.get_balance()["total"]
    risk_usd = equity * RISK_PCT
    qty = floor_qty(ex, sym, risk_usd / r)
    if qty <= 0:
        log("%s: qty 0 (risk $%.2f / R $%.2f) - lewat" % (sym, risk_usd, r))
        return None
    msg = ("<b>ENTRY 1D BREAKOUT</b>\n%s\nHarga %.6g | SL %.6g (%.2f%%)\n"
           "Qty %s | Risk $%.2f (%.0f%%) | R %.6g" % (sym, px, sl, 100 * r / px, qty, risk_usd, 100 * RISK_PCT, r))
    if dry:
        log("DRY " + msg.replace("\n", " | "))
        return None
    try:
        t.setup_symbol_settings(sym, leverage=3, margin_type="ISOLATED")
    except Exception as e:
        log("%s: setup margin dilewat (%s)" % (sym, str(e)[:60]))
    od = t.place_market_order(sym, "buy", qty)
    if not od:
        log("%s: market order GAGAL" % sym)
        return None
    fill = t.get_order_fill(sym, od.get("id")) or {}
    entry = float(fill.get("average") or px)
    r = entry - sl
    ok = t.set_protective_stop(sym, sl, amount=qty, position_side="LONG")
    if not ok:
        log("%s: STOP GAGAL dipasang - tutup posisi" % sym)
        t.close_position(sym)
        return None
    for m, wt in LEGS:
        q = floor_qty(ex, sym, qty * wt)
        if q > 0:
            t.place_limit_order(sym, "sell", q, entry + r * m, reduce_only=True)
    log("ENTRY %s @ %.6g SL %.6g qty %s" % (sym, entry, sl, qty))
    tg(msg)
    return {"entry": entry, "sl": sl, "r": r, "qty": qty, "opened": int(time.time()),
            "best": entry, "ts": time.time()}


def record_close(t, sym, rec):
    """Posisi sudah tutup: hitung realized dari income, tulis daily_trades.csv."""
    base = sym.split("/")[0]
    try:
        rows = t.exchange.fapiPrivateGetIncome({"symbol": base + "USDT",
                                                "startTime": int(rec["opened"]) * 1000,
                                                "limit": 1000})
    except Exception as e:
        log("%s: income gagal (%s)" % (base, str(e)[:60]))
        return
    pnl = sum(float(r["income"]) for r in rows
              if r.get("incomeType") in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"))
    risk_usd = rec["qty"] * rec["r"]
    R = pnl / risk_usd if risk_usd else 0.0
    path = os.path.join(BASE, "daily_trades.csv")
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write("ts,symbol,entry,sl,qty,pnl_usd,R,hold_h\n")
        f.write("%d,%s,%.8g,%.8g,%s,%.4f,%.3f,%.1f\n" % (
            int(time.time()), base, rec["entry"], rec["sl"], rec["qty"], pnl, R,
            (time.time() - rec["opened"]) / 3600))
    log("%s: TUTUP pnl $%.4f (%.2fR) dicatat" % (base, pnl, R))
    tg("<b>CLOSE</b> %s | PnL $%.4f (%.2fR)" % (base, pnl, R))


def manage(t, ex, st):
    pos = {p["symbol"].split("/")[0]: p for p in t.get_open_positions()}
    for sym in list(st.keys()):
        rec = st[sym]
        if not isinstance(rec, dict) or "entry" not in rec:
            continue
        base = sym.split("/")[0]
        p = pos.get(base)
        if not p:
            record_close(t, sym, rec)
            st.pop(sym, None)
            continue
        r = rec["r"]
        age_d = (time.time() - rec["opened"]) / 86400
        if age_d >= HOLD_DAYS:
            log("%s: time exit %.0f hari" % (base, age_d))
            t.close_position(sym)
            tg("<b>TIME EXIT</b> %s %.0f hari" % (base, age_d))
            time.sleep(2)
            record_close(t, sym, rec)
            st.pop(sym, None)
            continue
        px = t.get_ticker(sym)["last"]
        rec["best"] = max(rec.get("best", rec["entry"]), px)
        if rec["best"] >= rec["entry"] + TRAIL_AFTER * r:
            new_sl = rec["best"] - TRAIL_DIST * r
            if new_sl > rec["sl"] + 1e-9:
                if t.set_protective_stop(sym, new_sl, position_side="LONG"):
                    log("%s: trail SL %.6g -> %.6g" % (base, rec["sl"], new_sl))
                    rec["sl"] = new_sl
        st[sym] = rec
    save_state(st)


def main():
    dry = "--dry" in sys.argv
    do_manage = "--manage" in sys.argv
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    ex.load_markets()
    t = BinanceFuturesTrader(is_testnet=False)
    st = load_state()

    if do_manage:
        manage(t, ex, st)
        return

    open_now = t.get_open_positions()
    st = {k: v for k, v in st.items() if isinstance(v, dict) and "entry" in v}
    if len(open_now) >= MAX_OPEN:
        log("posisi terbuka %d >= %d, skip scan" % (len(open_now), MAX_OPEN))
        return

    held = {p["symbol"].split("/")[0] for p in open_now}
    uni = build_universe(ex)
    log("universe %d koin, posisi terbuka %d/%d" % (len(uni), len(open_now), MAX_OPEN))
    for sym in uni:
        s = sym.split("/")[0]
        if s in held or sym in st:
            continue
        try:
            sig = signal(ex, sym)
        except Exception as e:
            log("%s: fetch gagal %s" % (s, str(e)[:60]))
            continue
        if not sig:
            continue
        log("%s: SINYAL close %.6g vol %.2fx ATR %.6g" % (s, sig["close"], sig["volr"], sig["atr"]))
        rec = open_trade(t, ex, sig, dry)
        if rec:
            st[sym] = rec
            save_state(st)
            if len(st) + len(held) >= MAX_OPEN:
                break


if __name__ == "__main__":
    main()
