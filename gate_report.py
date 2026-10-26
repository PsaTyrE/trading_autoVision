#!/usr/bin/env python3
"""Gate report: the two conditions for adding capital. One file, stdlib csv only."""
import csv, os, sys
sys.path.insert(0, "/home/pi/latihan")

CLOSED = "/home/pi/latihan/trade_closed.csv"
NEED_N, NEED_R, NEED_WR = 30, 3.0, 30.0
ABORT_N = 20  # kill-switch: 20 closed + sumR<=0 = ladder hypothesis failed


def read():
    if not os.path.exists(CLOSED):
        return []
    with open(CLOSED) as f:
        return [r for r in csv.DictReader(f) if r.get("R")]


def report(rows):
    n = len(rows)
    R = [float(r["R"]) for r in rows]
    net = sum(float(r["net"]) for r in rows)
    wins = sum(1 for x in R if x > 0)
    tot = sum(R)
    wr = wins / n * 100 if n else 0.0
    ok_n = n >= NEED_N
    ok_r = tot >= NEED_R and wr >= NEED_WR
    abort = n >= ABORT_N and tot <= 0
    verdict = ("TAMBAH MODAL" if ok_n and ok_r else
               "MATIKAN SEKARANG (abort 20)" if abort else
               "MATIKAN (sumR<=0)" if ok_n and tot <= 0 else
               "TUNGGU")
    L = [f"<b>GATE REPORT</b> — {n}/{NEED_N} trade closed",
         f"ΣR <b>{tot:+.2f}</b> (butuh ≥ +{NEED_R})",
         f"Winrate <b>{wr:.1f}%</b> (butuh ≥ {NEED_WR:.0f}%)",
         f"Net <b>{net:+.4f} USDT</b>",
         f"Syarat1 sampel: {'LULUS' if ok_n else 'BELUM'} | "
         f"Syarat2 hasil: {'LULUS' if ok_r else 'BELUM'}",
         f"Abort {n}/{ABORT_N}: {'YA - set ENTRY_ENABLED=false' if abort else 'tidak'}",
         f"Putusan: <b>{verdict}</b>"]
    if rows:
        r = rows[-1]
        L.append(f"Terakhir: {r['symbol']} {r['side']} {float(r['net']):+.4f} ({float(r['R']):+.2f}R)")
    return "\n".join(L)


if __name__ == "__main__":
    txt = report(read())
    print(txt)
    if "--send" in sys.argv:
        from src.telegram_notifier import TelegramNotifier
        TelegramNotifier().send_message(txt)
        print("SENT")
