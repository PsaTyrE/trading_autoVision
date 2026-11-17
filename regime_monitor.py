#!/usr/bin/env python3
"""Ukur expectancy strategi pullback di 30 hari terakhir -> regime_state.json.

Dipakai auto_pilot_trader.regime_go(). Fail-closed: kalau file tidak ada,
basi (>3 hari), atau exp < 0,10R -> entry diblokir.
"""
import json
import os
import time

import ccxt

BASE = "/home/pi/latihan"
OUT = os.path.join(BASE, "regime_state.json")
SYMS = """BTC ETH SOL BNB XRP DOGE ADA AVAX LINK SUI TON TRX DOT LTC BCH NEAR APT ARB OP
ATOM FIL INJ TIA SEI PEPE WIF BONK FLOKI ENA ONDO JUP RENDER FET AO AAVE UNI
CRV ALGO SAND MANA GALA IMX STX THETA EGLD AXS CHZ ENJ GMT APE 1000PEPE 1000SHIB""".split()
FEE, SLIP_R, RUNNER = 0.00039, 0.075, 0.10
LEGS = [(1.0, .26), (1.2, .24), (1.4, .22), (1.7, .18)]
WIN_DAYS, MIN_N, MIN_EXP = 30, 15, 0.10


def ema(v, n):
    k = 2 / (n + 1); e = v[0]; out = [e]
    for x in v[1:]:
        e = x * k + e * (1 - k); out.append(e)
    return out


def rsi(cl, n=14):
    g = l = 0.0
    for i in range(1, n + 1):
        d = cl[i] - cl[i - 1]
        g += max(d, 0); l += max(-d, 0)
    ag, al = g / n, l / n
    for i in range(n + 1, len(cl)):
        d = cl[i] - cl[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr14(h, l, c):
    tr = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    a = sum(tr[:14]) / 14
    for x in tr[14:]:
        a = (a * 13 + x) / 14
    return a


def sim(entry, sl, hi, lo, cl):
    r = abs(entry - sl)
    if r <= 0:
        return None
    fees = FEE * entry / r
    remain, got, best = 1.0 - RUNNER, 0.0, entry
    for i in range(len(hi)):
        if lo[i] <= sl:
            got += remain * ((sl - entry) / r - SLIP_R); return got - fees
        best = max(best, hi[i])
        for m, w in LEGS:
            if remain > 1e-9 and hi[i] >= entry + r * m:
                take = min(remain, w); got += take * m; remain -= take
        if remain > 1e-9 and best >= entry + 2 * r:
            ts = best - 0.75 * r
            if lo[i] <= ts:
                got += remain * ((ts - entry) / r); return got - fees
        if remain <= 1e-9:
            return got - fees
        if i >= 40:
            got += remain * ((cl[i] - entry) / r); return got - fees
    got += remain * ((cl[-1] - entry) / r)
    return got - fees


ex = ccxt.binanceusdm({"enableRateLimit": True})
ex.load_markets()
cut = int((time.time() - WIN_DAYS * 86400) * 1000)
Rs = []
for s in SYMS:
    sym = s + "/USDT:USDT"
    if sym not in ex.markets:
        continue
    try:
        oh = ex.fetch_ohlcv(sym, "4h", limit=400)
    except Exception:
        continue
    time.sleep(0.1)
    if len(oh) < 130:
        continue
    o = [b[1] for b in oh]; h = [b[2] for b in oh]; l = [b[3] for b in oh]
    c = [b[4] for b in oh]; v = [b[5] for b in oh]
    e50 = ema(c, 50)
    for i in range(60, len(c) - 1):
        if oh[i][0] < cut:
            continue
        if c[i] <= e50[i]:
            continue
        rr = rsi(c[max(0, i - 60):i + 1])
        if not (36 <= rr <= 58):
            continue
        if abs(l[i] - e50[i]) / e50[i] > 0.025:
            continue
        av = sum(v[i - 20:i]) / 20
        if av <= 0 or v[i] < av:
            continue
        a = atr14(h[:i + 1], l[:i + 1], c[:i + 1])
        if a <= 0:
            continue
        px = o[i + 1]
        R = sim(px, px - 2 * a, h[i + 1:], l[i + 1:], c[i + 1:])
        if R is not None:
            Rs.append(R)

exp = sum(Rs) / len(Rs) if Rs else -9.0
wr = 100 * sum(1 for x in Rs if x > 0) / len(Rs) if Rs else 0.0
go = len(Rs) >= MIN_N and exp >= MIN_EXP
state = {"ts": int(time.time()), "window_days": WIN_DAYS, "n": len(Rs),
         "exp": round(exp, 4), "wr": round(wr, 1), "min_n": MIN_N,
         "min_exp": MIN_EXP, "go": go}
json.dump(state, open(OUT, "w"), indent=1)
print(json.dumps(state))
