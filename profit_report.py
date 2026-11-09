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


def _income_retry(ex, since, tries=3, pause=2.0):
    """-2015 di endpoint income masih intermiten: ulang dulu sebelum menyerah."""
    last = None
    for i in range(tries):
        try:
            return ex.fapiPrivateGetIncome({"startTime": since, "limit": 1000}) or []
        except Exception as e:
            last = e
            if i + 1 < tries:
                time.sleep(pause)
    raise last


def fetch_income(ex):
    rows = []
    since = 1730000000000
    while True:
        ch = _income_retry(ex, since)
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
    try:
        with contextlib.redirect_stdout(sys.stderr):
            rows = fetch_income(ex)
    except Exception as e:
        # Tanpa income, angka profit tidak bisa dipertanggungjawabkan. Kirim apa
        # adanya daripada diam: bot butuh izin baca riwayat (income/userTrades).
        msg = ("<b>LAPORAN PROFIT GAGAL</b> - %s WIB\n"
               "Endpoint income ditolak bursa: %s\n"
               "Artinya: izin baca riwayat (income/userTrades) di API key bermasalah.\n"
               "Trading tetap jalan (order/posisi/balance normal)."
               % (datetime.now().strftime("%d %b %H:%M"), str(e)[:120]))
        print(msg)
        if "--send" in sys.argv:
            token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
            if token and chat:
                import requests
                requests.post("https://api.telegram.org/bot%s/sendMessage" % token,
                              json={"chat_id": chat, "text": msg, "parse_mode": "HTML"}, timeout=20)
        return 1
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
    r_series, ts_series = [], []
    try:
        for r in csv.DictReader(open("trade_closed.csv", encoding="utf-8")):
            try:
                R = float(r["R"])
            except Exception:
                continue
            n_close += 1
            sum_r += R
            wins += 1 if R > 0 else 0
            r_series.append(R)
            try:
                _t = float(r.get("ts") or 0)
                ts_series.append(_t / 1000.0 if _t > 1e11 else _t)
            except Exception:
                ts_series.append(0.0)
    except Exception:
        pass
    exp_r = sum_r / n_close if n_close else 0.0
    wr = 100.0 * wins / n_close if n_close else 0.0

    # --- metrik risiko (Sharpe, max drawdown, expectancy per sesi WIB) ---
    sharpe_t = sharpe_d = mdd = None
    sess = []
    try:
        if len(r_series) >= 5:
            mu = sum(r_series) / len(r_series)
            sd = (sum((x - mu) ** 2 for x in r_series) / (len(r_series) - 1)) ** 0.5
            sharpe_t = mu / sd if sd else None
    except Exception:
        pass
    try:
        daily = {}
        for x in rows:
            if x["incomeType"] not in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                continue
            d = datetime.fromtimestamp(int(x["time"]) / 1000.0).strftime("%Y-%m-%d")
            daily[d] = daily.get(d, 0.0) + float(x["income"])
        if len(daily) >= 5 and deposit > 0:
            rets = [daily[k] / deposit for k in sorted(daily)]
            mu = sum(rets) / len(rets)
            sd = (sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
            sharpe_d = (mu / sd) * (365 ** 0.5) if sd else None
            eqc, peak, worst = deposit, deposit, 0.0
            for k in sorted(daily):
                eqc += daily[k]
                peak = max(peak, eqc)
                worst = min(worst, (eqc - peak) / peak * 100.0 if peak else 0.0)
            mdd = worst
    except Exception:
        pass
    try:
        buckets = {"Asia 07-14": [], "Eropa 14-20": [], "US 20-04": []}
        for ts, R in zip(ts_series, r_series):
            if not ts:
                continue
            h = datetime.fromtimestamp(ts).hour
            if 7 <= h < 14:
                buckets["Asia 07-14"].append(R)
            elif 14 <= h < 20:
                buckets["Eropa 14-20"].append(R)
            else:
                buckets["US 20-04"].append(R)
        for name, arr in buckets.items():
            if arr:
                sess.append("%s n=%d exp %+.2fR" % (name, len(arr), sum(arr) / len(arr)))
    except Exception:
        pass

    # --- CHECKPOINT: validasi filter volume bar 4h (pre-register 2026-11-08) ---
    # Hitung close yang terjadi SETELAH filter aktif, bandingkan dengan syarat FREEZE.md.
    FILTER_SINCE = time.mktime(time.strptime("2026-11-08 07:24", "%Y-%m-%d %H:%M"))
    f_n, f_w, f_r = 0, 0, 0.0
    try:
        for r in csv.DictReader(open("trade_closed.csv", encoding="utf-8")):
            try:
                R = float(r["R"])
            except Exception:
                continue
            try:
                ts = float(r.get("ts") or 0)
            except Exception:
                ts = 0.0
            if ts > 1e11:
                ts /= 1000.0
            if ts and ts >= FILTER_SINCE:
                f_n += 1
                f_r += R
                f_w += 1 if R > 0 else 0
    except Exception:
        pass

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
                side = (m.get("side") or p.get("side") or "").upper()
                mark = float(p.get("markPrice") or 0)
                if not mark or abs(mark - entry) < 1e-12:
                    qty = float(p.get("contracts") or 0)
                    upnl = float(p.get("unrealizedPnl") or 0)
                    if qty and entry:
                        mark = entry + (upnl / qty) * (1 if side == "LONG" else -1)
                    try:
                        with contextlib.redirect_stdout(sys.stderr):
                            tk = ex.fetch_ticker(sym)
                        _last = float(tk.get("last") or tk.get("close") or 0)
                        if _last:
                            mark = _last
                    except Exception:
                        pass
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
    if sharpe_t is not None or mdd is not None:
        lines.append("Risiko   : Sharpe/trade %s | Sharpe harian %s | Max DD %s"
                     % ("%.2f" % sharpe_t if sharpe_t is not None else "n/a",
                        "%.1f" % sharpe_d if sharpe_d is not None else "n/a",
                        "%.1f%%" % mdd if mdd is not None else "n/a"))
    if sess:
        lines.append("Per sesi : " + " | ".join(sess))
    if f_n == 0:
        lines.append("Checkpoint filter vol4h: 0/20 close (baru aktif 08 Nov 07:24, belum ada yang close)")
    else:
        f_exp = f_r / f_n
        f_wr = 100.0 * f_w / f_n
        if f_n >= 20:
            if f_wr >= 55 and f_exp >= 0.15:
                verdict = "LULUS -> lanjut, kandidat naik risk 3%"
            elif f_wr < 50 or f_exp <= 0:
                verdict = "GAGAL -> rollback MIN_VOLR_4H=0"
            else:
                verdict = "NETRAL -> perpanjang 10 close"
        else:
            verdict = "belum dinilai (butuh %d close lagi)" % (20 - f_n)
        lines.append("Checkpoint filter vol4h: %d/20 close | WR %.0f%% | exp %+.3fR | %s"
                     % (f_n, f_wr, f_exp, verdict))
    # --- FUNNEL hari ini: berapa kandidat muncul, ditolak di gerbang mana ---
    _today = datetime.now().strftime("%Y-%m-%d")
    _gate = {}
    try:
        for _ln in open("auto_pilot.log", encoding="utf-8", errors="ignore"):
            if _today not in _ln or "VETO" not in _ln:
                continue
            _sym = ""
            for _tok in _ln.split():
                if "/USDT" in _tok:
                    _sym = _tok.strip(":").split("/")[0]
                    break
            if not _sym:
                continue
            if "volr4h" in _ln:
                _k = "vol4h"
            elif "AI confidence" in _ln:
                _k = "AI"
            elif "PUCUK" in _ln or "jenuh" in _ln:
                _k = "pucuk"
            else:
                _k = "lain"
            _gate.setdefault(_k, set()).add(_sym)
    except Exception:
        pass
    _n_today = 0
    try:
        for _r in csv.DictReader(open("trade_log.csv", encoding="utf-8")):
            try:
                _ts = float(_r["ts"])
            except Exception:
                continue
            if _ts > 1e11:
                _ts /= 1000.0
            if datetime.fromtimestamp(_ts).strftime("%Y-%m-%d") == _today:
                _n_today += 1
    except Exception:
        pass
    if _gate or _n_today:
        lines.append("Funnel hari ini: entry %d | ditolak vol4h %d koin, AI %d, anti-pucuk %d, lain %d"
                     % (_n_today, len(_gate.get("vol4h", ())), len(_gate.get("AI", ())),
                        len(_gate.get("pucuk", ())), len(_gate.get("lain", ()))))
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
