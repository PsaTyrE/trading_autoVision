#!/usr/bin/env python3
"""One short Telegram digest per closed 4H bar: who is squeezed, who has volume.

Read-only. Never places an order, never writes state. Cron: 6x/day, 3 min after
bar close (03/07/11/15/19/23 WIB).

ponytail: reuses strategy.features/signal instead of a second copy of the gate
logic; if the strategy changes, this follows for free.
"""
import os
import sys

sys.path.insert(0, "/home/pi/latihan")
os.chdir("/home/pi/latihan")

from dotenv import load_dotenv

load_dotenv("/home/pi/latihan/.env", override=True)

import pandas as pd

import auto_pilot_trader as A
from src import strategy as S
from src.live_trader import BinanceFuturesTrader
from src.telegram_notifier import TelegramNotifier

COLS = ["t", "o", "h", "l", "c", "v"]


def main():
    t = BinanceFuturesTrader()
    rows, sigs, bar_ts = [], [], None

    for sym in A.WATCHLIST:
        try:
            raw = t.exchange.fetch_ohlcv(sym, S.TF, limit=320)
            f = S.features(pd.DataFrame(raw, columns=COLS))
            x = f.iloc[-2]
            bar_ts = x["t"]
            vr = x["v"] / x["vma"] if x["vma"] else 0.0
            sqz = bool(x["bwq"] >= x["bbw"])
            sig = S.signal(f, -2)
            name = sym.split("/")[0]
            if sig:
                sigs.append(f"{name} {sig['side'].upper()} volR {vr:.2f}")
            rows.append((name, vr, sqz))
        except Exception as e:  # one bad pair must not kill the digest
            rows.append((sym.split("/")[0], -1.0, False))
            print(f"[digest] {sym} {type(e).__name__}: {str(e)[:60]}", flush=True)

    sqz = sorted([r for r in rows if r[2]], key=lambda r: -r[1])
    lines = [f"4H tutup {pd.to_datetime(bar_ts, unit='ms')} UTC"]
    if sigs:
        lines.append("SINYAL: " + " | ".join(sigs))
    else:
        lines.append("SINYAL: nol")
    if sqz:
        lines.append("Kompresi: " + ", ".join(f"{n} {v:.2f}" for n, v, _ in sqz[:6]))
    else:
        lines.append("Kompresi: nol")
    best = max(rows, key=lambda r: r[1])
    lines.append(f"VolR tertinggi: {best[0]} {best[1]:.2f} (gate {S.MIN_VOL_RATIO:.1f})")
    lines.append("Bot flat, nunggu." if not sigs else "Bot entry sendiri kalau lolos gate.")

    TelegramNotifier().send_message("\n".join(lines))
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    main()
