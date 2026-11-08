#!/usr/bin/env python3
"""Laporan PROFIT harian ke Telegram - satu pesan, satu angka.

Jalankan: ./venv/bin/python profit_report.py [--send]
Tanpa --send cuma print ke stdout (buat tes).
Angka dari Binance langsung (fapiPrivateGetIncome + fetch_balance), bukan CSV,
supaya tidak ada beda cakupan. Komposisi profit selalu ditampilkan: kalau
trading rugi tapi akun hijau karena kompensasi Binance, itu harus kelihatan.
"""
import contextlib
import csv
import json
import math
import os
import sys
import time
from datetime import datetime

BASE = "/home/pi/latihan"
os.chdir(BASE)
sys.path.insert(0, BASE)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(BASE, ".env"), override=True)

from src.live_trader import BinanceFuturesTrader  # noqa: E402

TARGET_PROFIT = float(os.getenv("TARGET_PROFIT_USD", "100"))
TRADES_PER_DAY = float(os.getenv("ASSUMED_TRADES_PER_DAY", "1.5"))
RISK_PCT = float(os.getenv("RISK_PCT", "0.02"))


def fetch_income(ex):
    rows = []
    since = 1730000000000
    while True:
        ch = ex.fapiPrivateGetIncome({"startTime": since, "limit": 1000}) or []
        if not ch:
            break
        rows += ch
        mx = max(int(x["time"]) for x in ch)
        if len(ch) < 1000 or mx <= since:
            break
        since = mx + 1
    return rows


def main():
    t = BinanceFuturesTrader(is_testnet=False)
    ex = t.exchange
    with contextlib.redirect_stdout(sys.stderr):
        rows = fetch_income(ex)
        bal = ex.fetch_balance()
        positions = t.get_open_positions() or []

    tot = {}
    for x in rows:
        tot[x["incomeType"]] = tot.get(x["incomeType"], 0.0) + float(x["income"])
    deposit = tot.get("TRANSFER", 0.0)
    realized = tot.get("REALIZED_PNL", 0.0)
    commission = tot.get("COMMISSION", 0.0)
    funding = tot.get("FUNDING_FEE", 0.0)
    claim = tot.get("POSITION_CLAIM_TRANSFER", 0.0)
    net_trading = realized + commission + funding
    equity = float(bal["USDT"]["total"])
    profit = equity - deposit
    pct = 100.0 * profit / deposit if deposit else 0.0
    target_equity = deposit + TARGET_PROFIT

    first_ts = min(int(x["time"]) for x in rows) / 1000.0
    days = max(1.0, (time.time() - first_ts) / 86400.0)

    # statistik dari CSV
    n_close, sum_r, wins = 0, 0.0, 0
    try:
        for r in csv.DictReader(open("trade_closed.csv", encoding="utf-8")):
            try:
                R = float(r["R"])
                n_close += 1
                sum_r += R
                wins += 1 if R > 0 else 0
            except Exception:
                continue
    except Exception:
        pass
    exp_r = sum_r / n_close if n_close else 0.0
    wr = 100.0 * wins / n_close if n_close else 0.0

    # proyeksi struktural: expectancy x risk x frekuensi (bukan ekstrapolasi 14 hari)
    per_day = exp_r * RISK_PCT * TRADES_PER_DAY
    if per_day > 0 and equity > 0 and target_equity > equity:
        eta_days = math.log(target_equity / equity) / math.log(1.0 + per_day)
        eta = "~%d hari (~%s) @ %.2f%%/hari" % (
            eta_days, datetime.fromtimestamp(time.time() + eta_days * 86400).strftime("%b %Y"), 100 * per_day)
    else:
        eta = "belum bisa diproyeksikan (expectancy %+.3fR)" % exp_r

    # pace nyata 14 hari
    cut = (time.time() - 14 * 86400) * 1000
    pace14 = sum(float(x["income"]) for x in rows if int(x["time"]) >= cut
                 and x["incomeType"] in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"))

    # R per posisi terbuka: pakai SL dari state (posisi exchange tidak bawa SL)
    try:
        stt = json.load(open("autonomous_trader_state.json", encoding="utf-8"))
        meta = stt.get("active_trades") or {}
    except Exception:
        meta = {}

    lines = []
    lines.append("<b>LAPORAN PROFIT</b> - %s WIB" % datetime.now().strftime("%d %b %H:%M"))
    lines.append("")
    lines.append("Equity   : $%.2f  (modal $%.2f, hari ke-%d)" % (equity, deposit, int(days)))
    lines.append("<b>PROFIT   : %+.2f USDT (%+.2f%%)</b>" % (profit, pct))
    lines.append("Komposisi: trading %+.2f | kompensasi %+.2f | floating %+.2f"
                 % (net_trading, claim, equity - (deposit + net_trading + claim)))
    lines.append("Target   : $%.2f profit -> equity $%.2f (sisa $%.2f)"
                 % (TARGET_PROFIT, target_equity, target_equity - equity))
    lines.append("Proyeksi : %s" % eta)
    lines.append("Pace 14h : %+.2f USDT" % pace14)
    lines.append("")
    if positions:
        lines.append("Posisi terbuka (%d):" % len(positions))
        def _meta_for(sym):
            if sym in meta:
                return meta[sym]
            base = sym.split("/")[0].split(":")[0]
            for k, v in meta.items():
                if k.split("/")[0].split(":")[0] == base:
                    return v
            return {}

        for p in positions:
            sym = p.get("symbol", "?")
            m = _meta_for(sym)
            try:
                entry = float(m.get("entry") or p.get("entryPrice") or 0)
                sl = float(m.get("sl") or 0)
                mark = float(p.get("markPrice") or 0)
                side = (m.get("side") or p.get("side") or "").upper()
                if entry and sl and sl != entry and mark:
                    risk = abs(entry - sl)
                    r = (mark - entry) / risk
                    if side == "SHORT":
                        r = -r
                    lines.append("  %s %s: %+.2fR (mark %.6g, SL %.6g)" % (sym.split("/")[0], side, r, mark, sl))
                else:
                    lines.append("  %s %s: SL tidak terbaca" % (sym.split("/")[0], side))
            except Exception:
                lines.append("  %s: gagal hitung R" % sym.split("/")[0])
    else:
        lines.append("Posisi terbuka: tidak ada (semua margin bebas)")
    lines.append("")
    lines.append("Track record: %d close | exp %+.3fR | WR %.0f%%" % (n_close, exp_r, wr))
    msg = "\n".join(lines)
    print(msg)

    if "--send" in sys.argv:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat:
            print("SEND_SKIP: TELEGRAM_BOT_TOKEN/CHAT_ID kosong")
            return 0
        try:
            import requests
            r = requests.post("https://api.telegram.org/bot%s/sendMessage" % token,
                              json={"chat_id": chat, "text": msg, "parse_mode": "HTML",
                                    "disable_web_page_preview": True}, timeout=20)
            print("SEND: %s %s" % (r.status_code, (r.text or "")[:120]))
            return 0 if r.status_code == 200 else 1
        except Exception as e:
            print("SEND_ERR: %s: %s" % (type(e).__name__, str(e)[:150]))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
