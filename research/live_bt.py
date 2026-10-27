#!/usr/bin/env python3
"""research/live_bt.py - replay the LIVE config exactly as auto_pilot_trader runs it.

Signal: src.strategy.signal on the closed 4h bar.
Fill  : post-only maker limit at the signal bar's close; fills only if the next
        bar trades back to that price.
Exits : SL 2xATR (taker), TP1 50% at 2R (maker), TP2 50% at 3.5R (maker),
        stop moves to breakeven at 1R, trails 0.5R behind price after 2R.
Null  : same ladder, random entry bars, same side mix, 300 shuffles.

READ ONLY. Never places an order.
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

import pandas as pd

from research import lab
from src import strategy as S

MAKER = 0.0002   # 0.02% per side
TAKER = 0.0005   # 0.05% per side, stop-outs
BE_LOCK = 0.001  # entry*1.001 breakeven lock, as in manage_open_positions


def replay(df, feat, sides, lad=None):
    """Simulate one symbol at the given entry indices. Returns list of R multiples."""
    o = df["o"].values
    h = df["h"].values
    lo = df["l"].values
    c = df["c"].values
    atr = feat["atr"].values
    lad = lad or {}
    TP1 = lad.get("tp1", S.TP1_R)
    TP2 = lad.get("tp2", S.TP2_R)
    BE = lad.get("be", 1.0)
    TR = lad.get("trail", 2.0)
    TRK = lad.get("trail_k", 0.5)
    SLM = lad.get("sl_atr", S.SL_ATR)
    out = []
    for i in sides:
        if i + 1 >= len(df):
            continue
        sd = SLM * atr[i]
        if not sd or sd <= 0:
            continue
        sig_close = c[i]
        side = sides[i]
        # post-only limit at the signal close: fills only if price trades back to it
        if h[i + 1] < sig_close or lo[i + 1] > sig_close:
            continue
        entry = sig_close
        r = sd
        if side == "BUY":
            sl = entry - r
            tp1, tp2 = entry + r * TP1, entry + r * TP2
        else:
            sl = entry + r
            tp1, tp2 = entry - r * TP1, entry - r * TP2

        notional = 1.0  # unit notional; R normalised by stop distance
        fee_m = notional * MAKER
        fee_t = notional * TAKER
        risk_pct = r / entry  # fraction of notional at risk

        got1 = False
        realized = 0.0  # in notional units
        fees = 0.0
        closed = False
        for j in range(i + 1, len(df)):
            if side == "BUY":
                hit_sl = lo[j] <= sl
                hit1 = h[j] >= tp1
                hit2 = h[j] >= tp2
            else:
                hit_sl = h[j] >= sl
                hit1 = lo[j] <= tp1
                hit2 = lo[j] <= tp2
            # conservative: stop before target inside the same bar
            if hit_sl:
                move = (sl - entry) / entry if side == "BUY" else (entry - sl) / entry
                realized += 0.5 * move if got1 else move
                fees += fee_t * (0.5 if got1 else 1.0)
                closed = True
                exit_j = j
                break
            if not got1 and hit1:
                move = (tp1 - entry) / entry if side == "BUY" else (entry - tp1) / entry
                realized += 0.5 * move
                fees += fee_m * 0.5
                got1 = True
                sl = entry * (1 + BE_LOCK) if side == "BUY" else entry * (1 - BE_LOCK)
                if BE <= 0:
                    sl = entry - r if side == "BUY" else entry + r
            if got1 and hit2:
                move = (tp2 - entry) / entry if side == "BUY" else (entry - tp2) / entry
                realized += 0.5 * move
                fees += fee_m * 0.5
                closed = True
                exit_j = j
                break
            if got1:
                # trail 0.5R behind price once price is 2R away
                gain = (h[j] - entry) if side == "BUY" else (entry - lo[j])
                if gain >= TR * r:
                    if side == "BUY":
                        sl = max(sl, h[j] - TRK * r)
                    else:
                        sl = min(sl, lo[j] + TRK * r)
        if not closed:
            exit_j = len(df) - 1
            move = (c[-1] - entry) / entry if side == "BUY" else (entry - c[-1]) / entry
            realized += 0.5 * move if got1 else move
            fees += fee_m * (0.5 if got1 else 1.0)
        # fee charged on entry notional too
        fees += fee_m
        net = realized - fees
        out.append((net / risk_pct, exit_j))  # -> (R multiple, exit bar)
    return out


def stats_r(rets):
    rets = [r[0] if isinstance(r, tuple) else r for r in rets]
    if not rets:
        return None
    n = len(rets)
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rets:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {"n": n, "win%": 100.0 * sum(1 for r in rets if r > 0) / n,
            "avgR": sum(rets) / n, "sumR": sum(rets), "ddR": dd}


def main():
    syms = lab.A.WATCHLIST
    rng = random.Random(11)
    all_real, all_rand = [], []
    per_sym = []
    for s in syms:
        try:
            df = lab.pull(s)
        except Exception as e:
            print(f"skip {s} {type(e).__name__}")
            continue
        if len(df) < S.MIN_BARS + 20:
            continue
        f = S.features(df)
        idxs = []
        side_map = {}
        for i in range(S.MIN_BARS, len(df) - 1):
            sg = S.signal(f, i)
            if sg:
                idxs.append(i)
                side_map[i] = sg[0]
        real = replay(df, f, side_map)
        if not real:
            per_sym.append((s, 0, None))
            continue
        rand = []
        for _ in range(30):
            ridx = rng.sample(range(S.MIN_BARS, len(df) - 1), min(len(idxs), 40))
            rand += replay(df, f, {i: rng.choice(["BUY", "SELL"]) for i in ridx})
        st = stats_r(real)
        per_sym.append((s, len(real), st))
        all_real += real
        all_rand += rand
        print(f"{s.replace('/USDT:USDT',''):<10} sig={len(idxs):<3} n={st['n']:<3} "
              f"win={st['win%']:5.1f}% avgR={st['avgR']:+6.3f} sumR={st['sumR']:+7.2f}")
    print()
    R, N = stats_r(all_real), stats_r(all_rand)
    print(f"LIVE CONFIG  n={R['n']:<4} win={R['win%']:5.1f}%  avgR={R['avgR']:+.4f}  "
          f"sumR={R['sumR']:+.2f}  maxDD={R['ddR']:.2f}R")
    print(f"RANDOM NULL  n={N['n']:<4} win={N['win%']:5.1f}%  avgR={N['avgR']:+.4f}  "
          f"sumR={N['sumR']:+.2f}  maxDD={N['ddR']:.2f}R")
    edge = R["avgR"] - N["avgR"]
    print(f"\nEDGE = {edge:+.4f} R/trade")
    print(f"At risk $0.30/trade: {edge*0.30:+.4f} USD/trade")
    print("VERDICT:", "EDGE" if edge > 0.05 else "NO EDGE - do not trade this")


LADDERS = {
    "A live 2/3.5 be1 tr2":      dict(),
    "B be0.5":                   dict(be=0.5),
    "C tp1.5/3":                 dict(tp1=1.5, tp2=3.0),
    "D tp3/6":                   dict(tp1=3.0, tp2=6.0),
    "E tp1/2":                   dict(tp1=1.0, tp2=2.0),
    "F sl1.5":                   dict(sl_atr=1.5),
    "G tr1.5 k0.3":              dict(trail=1.5, trail_k=0.3),
    "H no partial tr1 k0.5":     dict(tp1=99, tp2=99, trail=1.0, trail_k=0.5),
}


def cmd_ladder():
    """Same signals, different exit ladders, IS/OOS split."""
    syms = lab.A.WATCHLIST
    data = []
    for s in syms:
        try:
            df = lab.pull(s)
        except Exception:
            continue
        if len(df) < S.MIN_BARS + 20:
            continue
        f = S.features(df)
        sm = {}
        for i in range(S.MIN_BARS, len(df) - 1):
            sg = S.signal(f, i)
            if sg:
                sm[i] = sg[0]
        if sm:
            data.append((s, df, f, sm))
    print(f"{len(data)} symbols with signals\n")
    print(f"{'ladder':<24}{'n':>5}{'win%':>7}{'avgR':>9}{'IS avgR':>10}{'OOS avgR':>10}  verdict")
    for name, lad in LADDERS.items():
        real = []
        for s, df, f, sm in data:
            real += replay(df, f, sm, lad)
        if len(real) < 50:
            continue
        cut = int(len(real) * 0.60)
        a = stats_r(real)
        i_s = stats_r(real[:cut])
        o = stats_r(real[cut:])
        ok = i_s["avgR"] > 0 and o["avgR"] > 0
        print(f"{name:<24}{a['n']:>5}{a['win%']:>6.1f}%{a['avgR']:>+9.4f}"
              f"{i_s['avgR']:>+10.4f}{o['avgR']:>+10.4f}  {'BOTH+' if ok else '-'}")
    print("\nIS/OOS split is by trade order, not time - treat BOTH+ as a weak filter only.")


def cmd_seq():
    """One position at a time, one entry per day - what the bot actually does."""
    syms = lab.A.WATCHLIST
    data = []
    for s in syms:
        try:
            df = lab.pull(s)
        except Exception:
            continue
        if len(df) < S.MIN_BARS + 20:
            continue
        f = S.features(df)
        for i in range(S.MIN_BARS, len(df) - 1):
            sg = S.signal(f, i)
            if sg:
                data.append((i, s, df, f, sg[0]))
    data.sort()
    print(f"{len(data)} signals across {len({d[1] for d in data})} symbols\n")
    print(f"{'ladder':<24}{'n':>5}{'win%':>7}{'sumR':>9}{'avgR':>8}{'maxR':>8}{'medR':>8}")
    for name, lad in LADDERS.items():
        busy = -1
        last_day = -99
        rs = []
        for i, s, df, f, side in data:
            if i <= busy or (i // 6) - last_day < 1:
                continue
            res = replay(df, f, {i: side}, lad)
            if not res:
                continue
            r, exit_j = res[0]
            rs.append(r)
            busy = exit_j
            last_day = i // 6
        if len(rs) < 10:
            continue
        st = stats_r(rs)
        med = statistics.median(rs)
        print(f"{name:<24}{st['n']:>5}{st['win%']:>6.1f}%{st['sumR']:>+9.2f}"
              f"{st['avgR']:>+8.3f}{max(rs):>+8.2f}{med:>+8.3f}")
    print("\nmaxR is the single best trade. If avgR >> medR the result is one outlier.")


if __name__ == "__main__":
    cmds = {"ladder": cmd_ladder, "seq": cmd_seq}
    cmds[sys.argv[1]]() if len(sys.argv) > 1 and sys.argv[1] in cmds else main()
