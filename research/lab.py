#!/usr/bin/env python3
"""research/lab.py - hypothesis lab. READ ONLY: never places an order.

Tests structural edges on cached OHLCV against a random-entry baseline with
identical trade count, side mix and holding period. A hypothesis only counts if
it beats the baseline's own distribution, not zero.

Usage: lab.py pull | lab.py xsmom
"""
import os
import sys
import random
import statistics

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)
from dotenv import load_dotenv

load_dotenv(os.path.join(_ROOT, ".env"), override=True)

import ccxt
import numpy as np
import pandas as pd

import auto_pilot_trader as A

CACHE = "research/cache"
TF = "4h"
BARS = 1000            # ~167 days of 4h (exchange per-request cap)
FEE = 0.0004           # maker both sides (0.02% x 2)
FEE_TAKER = 0.0010     # taker both sides, stress case

EX = ccxt.binanceusdm({  # public endpoints only; no keys needed for OHLCV
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
})


def cache_path(sym, tf=TF):
    return f"{CACHE}/{sym.replace('/', '_').replace(':', '_')}_{tf}.csv"


def pull(sym, tf=TF, limit=BARS):
    os.makedirs(CACHE, exist_ok=True)
    p = cache_path(sym, tf)
    if os.path.exists(p):
        d = pd.read_csv(p)
        if len(d) >= limit * 0.95:
            return d
    raw = EX.fetch_ohlcv(sym, tf, limit=limit)
    d = pd.DataFrame(raw, columns=["t", "o", "h", "l", "c", "v"])
    d.to_csv(p, index=False)
    return d


def load_matrix(symbols):
    """Aligned close matrix: index=bar timestamp, columns=symbol."""
    series = {}
    for s in symbols:
        try:
            d = pull(s)
        except Exception as e:
            print(f"  skip {s}: {type(e).__name__}")
            continue
        if len(d) < BARS * 0.9:
            print(f"  skip {s}: only {len(d)} bars")
            continue
        series[s] = d.set_index("t")["c"]
    m = pd.DataFrame(series).dropna()
    return m


def xsmom(m, K, H, N, fee=FEE, shuffle=False, rng=None, reverse=False):
    """Cross-sectional momentum, non-overlapping windows.

    Each window: rank by past K-bar return, long top N, short bottom N, hold H
    bars. Returns list of per-window net returns (equal weight per leg).
    reverse=True flips legs (long losers / short winners).
    """
    px = m.values
    cols = list(m.columns)
    n_sym = len(cols)
    if n_sym < N * 2 + 2:
        return []
    out = []
    i = K
    while i + H < len(px):
        past = px[i] / px[i - K] - 1.0
        fut = px[i + H] / px[i] - 1.0
        order = past.argsort()
        if shuffle:
            idx = list(range(n_sym))
            rng.shuffle(idx)
            longs, shorts = idx[:N], idx[-N:]
        else:
            longs, shorts = list(order[-N:]), list(order[:N])
        if reverse:
            longs, shorts = shorts, longs
        r = (fut[longs].mean() - fut[shorts].mean()) - fee
        out.append(r)
        i += H
    return out


def stats(rets):
    if not rets:
        return None
    n = len(rets)
    wins = sum(1 for r in rets if r > 0)
    eq, peak, dd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1 + r)
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
    return {
        "n": n,
        "win%": 100.0 * wins / n,
        "avg%": 100.0 * sum(rets) / n,
        "tot%": 100.0 * (eq - 1),
        "dd%": 100.0 * dd,
    }


def show(tag, s):
    if not s:
        print(f"{tag:<28} no trades")
        return
    print(f"{tag:<28} n={s['n']:<4} win={s['win%']:5.1f}%  "
          f"avg={s['avg%']:+6.3f}%  tot={s['tot%']:+8.2f}%  dd={s['dd%']:6.2f}%")


def cmd_pull():
    syms = A.WATCHLIST
    print(f"pulling {len(syms)} symbols x {BARS} bars {TF}")
    ok = 0
    for s in syms:
        try:
            d = pull(s)
            ok += 1
            print(f"  {s:<22} {len(d)} bars  {pd.to_datetime(d['t'].iloc[0], unit='ms')} -> "
                  f"{pd.to_datetime(d['t'].iloc[-1], unit='ms')}")
        except Exception as e:
            print(f"  {s:<22} FAIL {type(e).__name__}: {e}")
    print(f"OK {ok}/{len(syms)}")


def cmd_xsmom():
    m = load_matrix(A.WATCHLIST)
    print(f"matrix {m.shape[0]} bars x {m.shape[1]} symbols "
          f"({pd.to_datetime(m.index[0], unit='ms').date()} -> "
          f"{pd.to_datetime(m.index[-1], unit='ms').date()})")
    print()
    rng = random.Random(42)
    rows = []
    for K in (6, 12, 30):
        for H in (6, 18):
            for N in (3, 5):
                real = xsmom(m, K, H, N)
                if not real:
                    continue
                s = stats(real)
                base = [statistics.mean(xsmom(m, K, H, N, shuffle=True, rng=rng) or [0])
                        for _ in range(200)]
                base.sort()
                p95 = base[int(0.95 * len(base))]
                beat = s["avg%"] / 100.0 > p95
                tag = f"K={K} H={H} N={N}"
                show(tag, s)
                print(f"{'':<28} baseline avg={100*statistics.mean(base):+.3f}%  "
                      f"p95={100*p95:+.3f}%  {'BEATS' if beat else 'no edge'}")
                rows.append((tag, s, statistics.mean(base), p95, beat))
    print()
    winners = [r for r in rows if r[4]]
    print(f"BEATS BASELINE: {len(winners)}/{len(rows)}")
    for tag, s, b, p95, _ in winners:
        print(f"  {tag}  avg={s['avg%']:+.3f}%  n={s['n']}  tot={s['tot%']:+.2f}%")
    if not winners:
        print("  verdict: cross-sectional momentum has no edge here either")
    # stress the best combo at taker fees
    if rows:
        best = max(rows, key=lambda r: r[1]["avg%"])
        tag, s, b, p95, _ = best
        print(f"\nbest {tag} at TAKER fee ({FEE_TAKER*100:.2f}% round trip):")
        K, H, N = [int(x.split("=")[1]) for x in tag.split()]
        show("  taker", stats(xsmom(m, K, H, N, fee=FEE_TAKER)))


def cmd_rev():
    """Reversal legs (long losers / short winners) + out-of-sample split.

    Runs the same grid with reverse=True, compares against a reversed random
    baseline, then re-checks the surviving combos on the last 40% of history.
    """
    m = load_matrix(A.WATCHLIST)
    n = len(m)
    split = int(n * 0.60)
    print(f"matrix {n} bars x {m.shape[1]} symbols  "
          f"IS={pd.to_datetime(m.index[0], unit='ms').date()}->"
          f"{pd.to_datetime(m.index[split - 1], unit='ms').date()}  "
          f"OOS={pd.to_datetime(m.index[split], unit='ms').date()}->"
          f"{pd.to_datetime(m.index[-1], unit='ms').date()}")
    print()
    rng = random.Random(7)
    survivors = []
    for K in (3, 6, 12):
        for H in (3, 6, 12):
            for N in (1, 2, 3):
                real = xsmom(m, K, H, N, reverse=True)
                if len(real) < 20:
                    continue
                s = stats(real)
                base = [statistics.mean(xsmom(m, K, H, N, shuffle=True, rng=rng,
                                              reverse=True) or [0])
                        for _ in range(300)]
                base.sort()
                p95 = base[int(0.95 * len(base))]
                beat = s["avg%"] / 100.0 > p95
                print(f"K={K:<3}H={H:<3}N={N}  n={s['n']:<4} win={s['win%']:5.1f}%  "
                      f"avg={s['avg%']:+6.3f}%  base={100*statistics.mean(base):+6.3f}%  "
                      f"p95={100*p95:+6.3f}%  {'BEATS' if beat else '-'}")
                if beat:
                    survivors.append((K, H, N, s))
    print()
    print(f"BEATS BASELINE (in-sample): {len(survivors)}")
    for K, H, N, s in survivors:
        sub = m.iloc[split:]
        oos = stats(xsmom(sub, K, H, N, reverse=True))
        print(f"  K={K} H={H} N={N}  IS avg={s['avg%']:+.3f}% n={s['n']}  ->  "
              f"OOS avg={oos['avg%']:+.3f}% n={oos['n']} win={oos['win%']:.1f}% "
              f"tot={oos['tot%']:+.2f}%")
    if not survivors:
        print("  verdict: reversal has no edge either")


def sanitize(m, jump=0.35, max_bad=5):
    """Drop glitched bars (|bar-to-bar move| > jump) and thin symbols.

    Bad prints on illiquid alts create fake -99% / +300% windows that dominate
    any mean. A column with more than max_bad glitches is dropped entirely.
    """
    clean = {}
    dropped = []
    for c in m.columns:
        s = m[c].astype(float)
        ret = s.pct_change()
        bad = ret.abs() > jump
        nbad = int(bad.sum())
        if nbad > max_bad:
            dropped.append((c, nbad))
            continue
        s = s.copy()
        s[bad] = float("nan")
        s = s.ffill().bfill()
        clean[c] = s
    out = pd.DataFrame(clean)
    if dropped:
        print(f"  dropped {len(dropped)} thin/glitchy symbols: "
              + ", ".join(f"{c}({n})" for c, n in dropped))
    return out


def xsmom_w(m, K, H, N, fee=FEE, cap=0.25):
    """Reversal windows with per-leg return winsorised at +/-cap."""
    px = m.values
    out = []
    i = K
    while i + H < len(px):
        past = px[i] / px[i - K] - 1.0
        fut = np.clip(px[i + H] / px[i] - 1.0, -cap, cap)
        order = past.argsort()
        longs, shorts = list(order[:N]), list(order[-N:])
        out.append((fut[longs].mean() - fut[shorts].mean()) - fee)
        i += H
    return out


def cmd_robust():
    m = load_matrix(A.WATCHLIST)
    print(f"raw matrix {m.shape[0]} x {m.shape[1]}")
    m = sanitize(m)
    print(f"clean matrix {m.shape[0]} x {m.shape[1]}")
    print()
    n = len(m)
    split = int(n * 0.60)
    rng = random.Random(7)
    print(f"{'combo':<14}{'IS n':>6}{'IS avg':>9}{'OOS n':>7}{'OOS avg':>9}"
          f"{'OOS win':>9}{'max win':>9}  verdict")
    rows = []
    for K in (6, 12):
        for H in (6, 12):
            for N in (1, 2, 3):
                real = xsmom_w(m, K, H, N)
                if len(real) < 20:
                    continue
                base = sorted(statistics.mean(
                    xsmom(m, K, H, N, reverse=True, shuffle=True, rng=rng) or [0])
                    for _ in range(200))
                p95 = base[int(0.95 * len(base))]
                cut = int(len(real) * 0.60)
                is_s = stats(real[:cut])
                oos = stats(real[cut:])
                if not oos:
                    continue
                mx = max(real) * 100
                beat = oos["avg%"] / 100.0 > p95
                print(f"K={K:<3}H={H:<3}N={N}  {is_s['n']:>6}{is_s['avg%']:>+9.3f}"
                      f"{oos['n']:>7}{oos['avg%']:>+9.3f}{oos['win%']:>8.1f}%"
                      f"{mx:>+9.2f}  {'BEATS' if beat else '-'}")
                rows.append((K, H, N, oos, p95, beat))
    print()
    win = [r for r in rows if r[5]]
    print(f"OOS beats baseline: {len(win)}/{len(rows)}")
    for K, H, N, oos, p95, _ in win:
        print(f"  K={K} H={H} N={N}  OOS avg={oos['avg%']:+.3f}%  "
              f"tot={oos['tot%']:+.2f}%  dd={oos['dd%']:.2f}%  n={oos['n']}")
    if not win:
        print("  verdict: reversal edge was data artefacts, not signal")


def xs_ret(m, K, H, N, fee=FEE, cap=0.05, agg="mean"):
    """Reversal window returns with tight winsorisation and median option."""
    px = m.values
    out = []
    i = K
    while i + H < len(px):
        past = px[i] / px[i - K] - 1.0
        fut = np.clip(px[i + H] / px[i] - 1.0, -cap, cap)
        order = past.argsort()
        longs, shorts = list(order[:N]), list(order[-N:])
        if agg == "median":
            r = float(np.median(fut[longs]) - np.median(fut[shorts]))
        else:
            r = float(fut[longs].mean() - fut[shorts].mean())
        out.append(r - fee)
        i += H
    return out


def cmd_check():
    """Decisive robustness: tight cap, median agg, liquid-only universe."""
    m = sanitize(load_matrix(A.WATCHLIST))
    cols = list(m.columns)
    print(f"universe {len(cols)} symbols, {len(m)} bars")
    print()
    variants = [
        ("cap5% mean",        dict(cap=0.05, agg="mean")),
        ("cap5% median",      dict(cap=0.05, agg="median")),
        ("cap10% mean",       dict(cap=0.10, agg="mean")),
        ("cap2% mean",        dict(cap=0.02, agg="mean")),
    ]
    for K, H, N in ((6, 6, 3), (12, 6, 3), (12, 12, 3), (12, 6, 2)):
        print(f"--- K={K} H={H} N={N}")
        for name, kw in variants:
            r = xs_ret(m, K, H, N, **kw)
            st = stats(r)
            cut = int(len(r) * 0.60)
            oos = stats(r[cut:])
            tot = sum(r)
            top5 = sum(sorted(r)[-5:])
            share = 100 * top5 / tot if tot else 0
            print(f"  {name:<14} n={st['n']:<4} avg={st['avg%']:+6.3f}% "
                  f"win={st['win%']:5.1f}%  OOS avg={oos['avg%']:+6.3f}% "
                  f"win={oos['win%']:5.1f}%  top5={share:5.1f}% of profit")
    print()
    print("--- liquid-only (top 20 by median quote volume)")
    vol = {}
    for c in cols:
        d = pull(c)
        vol[c] = float((d["c"] * d["v"]).median())
    top = sorted(vol, key=vol.get, reverse=True)[:20]
    ml = m[top]
    print(f"  {', '.join(t.replace('/USDT:USDT','') for t in top)}")
    for K, H, N in ((6, 6, 3), (12, 6, 3), (12, 12, 3)):
        r = xs_ret(ml, K, H, N, cap=0.05, agg="mean")
        cut = int(len(r) * 0.60)
        st, oos = stats(r), stats(r[cut:])
        print(f"  K={K:<3}H={H:<3}N={N}  avg={st['avg%']:+6.3f}%  "
              f"OOS avg={oos['avg%']:+6.3f}% win={oos['win%']:5.1f}% "
              f"tot={oos['tot%']:+7.2f}% dd={oos['dd%']:6.2f}%")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "xsmom"
    {"pull": cmd_pull, "xsmom": cmd_xsmom, "rev": cmd_rev,
     "robust": cmd_robust, "check": cmd_check}[cmd]()
