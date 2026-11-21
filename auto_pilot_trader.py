#!/usr/bin/env python3
import csv
"""
Autonomous Senior Trader Engine (Pi Daemon) - Multi-Position + Correlation Guard
Handles:
1. Multi-Position Trailing Stop & Profit Management:
   - Dynamic Trailing Stop on all winning trades (protect profit)
   - Auto Breakeven once 1R is hit
   - Runner trailing at 0.75R once 2R is hit
   - Re-arm ladder TP1/TP2 if missing
2. Correlation Filter & Cluster Guard:
   - Prevents opening multiple positions in the same asset correlation cluster (BTC-correlated / ETH-correlated / High Beta)
   - Caps concurrent positions at MAX_OPEN_POSITIONS
   - Independent asset groups (Crypto Majors, High Beta Alts, Commodities/XAU)
3. Confluence & AI Debate Gate:
   - Squeeze + Breakout + Volume + VWAP + SMC Anti-trap
   - Bull vs Bear AI debate verification (Confidence >= 0.85)
4. Multi-Position State Tracking:
   - Synchronized dictionary `active_trades` in state JSON
   - Safe per-symbol close reconciliation
"""
from src.telegram_notifier import TelegramNotifier
from src.ai_analyzer import AIAnalyzer
from src.multi_analyst import MultiAnalystEngine
from src.forex_factory_news import ForexFactoryNewsSentinel
import sys
import os
import time
import json
import math
import contextlib
import ccxt
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
from src.live_trader import BinanceFuturesTrader, err_detail
from src import strategy

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)

WATCHLIST = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "XAU/USDT:USDT", "ZEC/USDT:USDT", "SOL/USDT:USDT",
    "NEAR/USDT:USDT", "HYPE/USDT:USDT", "XRP/USDT:USDT", "WLD/USDT:USDT", 
    "DOGE/USDT:USDT", "ENA/USDT:USDT", "IOST/USDT:USDT", "UNI/USDT:USDT", "BNB/USDT:USDT",
    "PUMP/USDT:USDT", "SUI/USDT:USDT", "ARB/USDT:USDT", "XMR/USDT:USDT", "GTC/USDT:USDT",
    "INJ/USDT:USDT", "ADA/USDT:USDT", "TIA/USDT:USDT", "AVAX/USDT:USDT", "TAO/USDT:USDT",
    "FIL/USDT:USDT", "LINK/USDT:USDT", "COMP/USDT:USDT", "AAVE/USDT:USDT", "BCH/USDT:USDT",
    "STRK/USDT:USDT", "TRUMP/USDT:USDT", "ZRO/USDT:USDT", "LTC/USDT:USDT", "ONDO/USDT:USDT",
    "ALT/USDT:USDT", "APR/USDT:USDT", "DOOD/USDT:USDT", "AGT/USDT:USDT", "BR/USDT:USDT",
    "BTW/USDT:USDT", "JCT/USDT:USDT", "Q/USDT:USDT", "APT/USDT:USDT", "WIF/USDT:USDT"
]

# --- UNIVERSE: seluruh pasar USDT-M perpetual, bukan cuma 45 koin hardcode ---
# SCAN_MODE=market -> tiap scan, daftar simbol dibangun dari fetch_tickers():
#                     semua perpetual USDT likuid, di-pre-rank volume 24 jam.
# SCAN_MODE=static -> pakai WATCHLIST di atas apa adanya (perilaku lama / rollback).
# Parameter strategi (RSI/ADX/BB/SL/risk) TIDAK berubah oleh patch ini.
SCAN_MODE = (os.getenv("SCAN_MODE", "market") or "market").strip().lower()
SCAN_MIN_VOL_USDT = float(os.getenv("SCAN_MIN_VOL_USDT", "20000000"))
SCAN_PRE_RANK = int(os.getenv("SCAN_PRE_RANK", "50"))
# Stablecoin / fiat quote pair: bukan aset yang mau ditradingkan.
SCAN_EXCLUDE_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDD", "PYUSD", "EUR",
    "EURI", "AEUR", "GBP", "TRY", "BRL", "ARS", "BIDR", "IDRT", "RUB", "UAH",
    "NGN", "ZAR",
}
# Token leveraged (BTCUP, ETHDOWN, ...) bukan spot exposure.
SCAN_LEV_SUFFIX = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")

# --- RANKING KANDIDAT ---
# Data 29 trade: surge 1,2-1,6x expectancy +0,288R; surge >=1,6x cuma +0,024R.
# band = dahulukan surge yang dekat SCAN_VOL_BAND_CENTER (default 1,4x).
# surge = perilaku lama (vol_ratio terbesar didahulukan) untuk rollback.
SCAN_RANK_MODE = (os.getenv("SCAN_RANK_MODE", "band") or "band").strip().lower()
SCAN_VOL_BAND_CENTER = float(os.getenv("SCAN_VOL_BAND_CENTER", "1.4"))


MAX_LEVERAGE = 5
MAX_MARGIN_PCT = 0.85
STATE_FILE = os.getenv("STATE_FILE", os.path.join(BASE_DIR, "autonomous_trader_state.json"))
JOURNAL = os.getenv("JOURNAL_FILE", os.path.join(BASE_DIR, "trade_log.csv"))
CLOSED = os.getenv("CLOSED_FILE", os.path.join(BASE_DIR, "trade_closed.csv"))
DAILY_CAP_SEC = int(os.getenv("DAILY_CAP_SEC", "86400"))
DAILY_MAX = int(os.getenv("MAX_DAILY_ENTRIES", "3"))

REGIME_FILE = os.path.join(BASE_DIR, "regime_state.json")


def regime_go():
    """Entry hanya kalau exp strategi ini di 30 hari terakhir >= +0,10R.

    Diukur regime_monitor.py (cron harian). Fail-closed: file hilang, basi
    >3 hari, atau exp di bawah ambang -> entry diblokir.
    """
    try:
        d = json.load(open(REGIME_FILE, encoding="utf-8"))
        if time.time() - float(d.get("ts") or 0) > 3 * 86400:
            log("REGIME: state basi (>3 hari), entry diblokir")
            return False
        ok = bool(d.get("go")) and float(d.get("exp") or -9) >= 0.10
        if not ok:
            log(f"REGIME NOGO: exp {d.get('exp')}R n={d.get('n')} (syarat +0,10R)")
        return ok
    except Exception as e:
        log(f"REGIME: state tidak terbaca ({err_detail(e)}), entry diblokir")
        return False

RISK_USDT = float(os.getenv("RISK_USDT", "0"))  # 0 = auto compounding dari equity live
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
MIN_NOTIONAL = 5.0
RISK_MAX = float(os.getenv("RISK_MAX_USDT", "0"))  # 0 = auto (1.6x risk budget)
MIN_AI_CONF = float(os.getenv("MIN_AI_CONF", "0.85"))

# --- COMPOUNDING RISK LADDER ---
# Risk USDT dihitung dari equity live, dan persentasenya NAIK bertahap
# seiring saldo tumbuh. Ini yang bikin growth eksponensial, bukan linear.
RISK_TIERS = [
    (0.0,   0.020),   # < $75   -> 2.0% per trade
    (75.0,  0.024),   # $75+    -> 2.4%
    (150.0, 0.028),   # $150+   -> 2.8%
    (300.0, 0.032),   # $300+   -> 3.2%
    (600.0, 0.035),   # $600+   -> 3.5%
]
RISK_PCT_CAP = float(os.getenv("RISK_PCT_CAP", "0.035"))
# Kandidat #1 (FREEZE.md): tolak entry kalau lebar SL > 5% dari harga.
# Dievaluasi di checkpoint 29 close: SL>5% n=10 -> -1,32R | SL<=5% n=19 -> +6,67R.
# 0 = filter mati.
MAX_SL_PCT = float(os.getenv("MAX_SL_PCT", "5.0"))

# --- Ladder TP: SATU sumber kebenaran ---------------------------------------
# Dulu entry ladder dan re-arm (_tp_legs_for) masing-masing punya salinan angka
# sendiri. Kalau satu diubah, re-arm memulihkan leg di level yang berbeda dari
# yang benar-benar dipasang. Sekarang keduanya membaca konstanta ini.
LADDER_LEVELS = [1.0, 1.2, 1.4, 1.7]
LADDER_WEIGHTS = [0.26, 0.24, 0.22, 0.18]
LADDER_RUNNER_PCT = 0.10          # porsi qty yang tidak dipasangi TP
REARM_MAX_TRIES = 3               # batas percobaan re-arm per posisi
TP_DUP_TOL = 2e-4                 # toleransi harga saat cek "order TP ini sudah ada?"
NEWS_TAG_TITLE_WINDOW_MIN = 180   # judul berita cuma ditempel kalau |menit| <= ini
BREAKER_EQUITY_PCT = 0.10         # circuit breaker minimal = 10% equity
BB_CLEAR_TOL = 0.015              # anti-pucuk: harga wajib 1,5% di luar Bollinger
QTY_COVER_TOL = 0.999             # stop dianggap menutup posisi kalau qty >= 99,9%



_POSMETA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pos_meta.json")


def load_pos_meta() -> dict:
    try:
        with open(_POSMETA) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_pos_meta(m: dict) -> None:
    try:
        tmp = _POSMETA + ".tmp"
        with open(tmp, "w") as f:
            json.dump(m, f)
        os.replace(tmp, _POSMETA)
    except Exception as e:
        log(f"save_pos_meta error: {e}")


def backfill_pos_meta(symbol: str, entry: float, side: str, cur_sl: float, qty: float) -> dict:
    """Isi pos_meta untuk posisi yang TIDAK punya trade_meta (mis. hasil ADOPT).

    Tanpa ini r_dist=0 -> trailing mati total dan SL tidak pernah dikunci
    (kejadian nyata: ZEC/BR/Q tidak punya trade_meta, SL beku di SL awal).
    """
    m = {"symbol": symbol, "entry": float(entry or 0), "side": str(side or "").upper(),
         "sl": float(cur_sl or 0), "qty": float(qty or 0), "backfilled": True}
    sym_info = None
    try:
        # risk dihitung dari jarak SL awal * qty (bukan dari state yang hilang)
        m["risk"] = round(abs(m["entry"] - m["sl"]) * m["qty"], 6) if m["sl"] else 0.0
    except Exception:
        m["risk"] = 0.0
    return m


def trail_lock_pct(side, entry, pnl_pct, atr_pct):
    """Kunci profit berbasis PERSEN gerak harga, bukan cuma R.

    Kenapa perlu: R = 2xATR. Untuk koin liar (Q: ATR 17% -> 1R = 34%), ambang
    0.8R baru kena setelah harga gerak ~27%, jadi profit +22% dibiarkan tanpa
    kuncian sama sekali. Persen gerak itu ukuran yang sama di semua koin.

    atr_pct = 1R dalam persen. Tahapan:
      < 0.5R  : tidak kunci
      0.5R    : BE
      0.8R    : +0.25R
      1.2R    : +0.5R
      2.0R    : +1.0R
    """
    try:
        pnl_pct = abs(float(pnl_pct)); atr_pct = float(atr_pct)
    except Exception:
        return None
    if atr_pct <= 0:
        return None
    g = pnl_pct / atr_pct
    if g < 0.5:
        return None
    if g >= 2.0:
        lock = 1.0
    elif g >= 1.2:
        lock = 0.5
    elif g >= 0.8:
        lock = 0.25
    else:
        lock = 0.0
    return lock


def trail_lock_sl(side, entry, price, r_dist, dec=4):
    """SL dinamis: kunci profit progresif sesuai gainR yang SUDAH tercapai.

    <0.8R      -> None (biarkan SL awal)
    0.8R..1.0R -> break-even
    1.0R..1.5R -> 0.4R
    1.5R..2.0R -> 0.8R
    2.0R..3.0R -> 1.2R
    >=3.0R     -> 1.5R
    lalu di atas 2R ikut ratchet 0.75R dari harga terbaik (runner).

    Dipakai otomatis untuk SEMUA posisi tiap siklus, jadi tidak perlu debat
    per koin: begitu ada gerak yang layak dikunci, SL ikut naik sendiri.
    """
    try:
        entry = float(entry); price = float(price); r_dist = float(r_dist)
    except Exception:
        return None
    if r_dist <= 0 or entry <= 0:
        return None
    gain = (price - entry) if side == "LONG" else (entry - price)
    g = gain / r_dist
    if g < 0.8:
        return None
    if g >= 3.0:
        lock = 1.5
    elif g >= 2.0:
        lock = 1.2
    elif g >= 1.5:
        lock = 0.8
    elif g >= 1.0:
        lock = 0.4
    else:
        lock = 0.0
    lv = entry + r_dist * lock if side == "LONG" else entry - r_dist * lock
    if lock == 0.0:
        lv = entry * 1.001 if side == "LONG" else entry * QTY_COVER_TOL
    out = round(lv, dec)
    if g >= 2.0:
        rt = runner_trail_sl(side, price, r_dist, dec)
        if rt is not None:
            out = max(out, rt) if side == "LONG" else min(out, rt)
    return out


def runner_trail_sl(side, price, r_dist, dec=4):
    """SL runner: ikut harga dengan jarak 0.75R (sesuai docstring modul, dulu tidak diimplementasi)."""
    try:
        price = float(price); r_dist = float(r_dist)
    except Exception:
        return None
    if r_dist <= 0:
        return None
    off = r_dist * 0.75
    return round(price - off, dec) if side == "LONG" else round(price + off, dec)


def tp_leg_passed(side, tp, price) -> bool:
    """True kalau level TP sudah dilewati harga (pasang limit di sini = langsung eksekusi pasar)."""
    try:
        tp = float(tp); price = float(price)
    except Exception:
        return False
    return (tp >= price) if side == "SHORT" else (tp <= price)


def rsi1h_veto(side, rsi1h) -> bool:
    """True kalau RSI 1h sudah jenuh untuk arah tsb -> entry dilarang."""
    if rsi1h is None:
        return False
    try:
        v = float(rsi1h)
    except Exception:
        return False
    if side == "LONG":
        return v >= 58.0
    if side == "SHORT":
        return v <= 42.0
    return False


def entry_ts_from_trades(rows, fallback) -> int:
    """Ambil waktu fill paling awal (detik) sebagai ts entry sebenarnya."""
    try:
        ts = [int(r["time"]) for r in (rows or [])]
        return (min(ts) // 1000) if ts else int(fallback)
    except Exception:
        return int(fallback)


def day_pnl_from_income(rows) -> float:
    """Jumlahkan PnL realisasi hari ini dari income Binance (pnl + fee + funding)."""
    tot = 0.0
    for r in (rows or []):
        try:
            if str(r.get("incomeType")) in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                tot += float(r.get("income") or 0.0)
        except Exception:
            continue
    return round(tot, 6)


def daily_loss_blocked(realized: float, equity: float, pct: float = 0.05) -> bool:
    """True kalau rugi realisasi hari ini sudah menembus pct dari equity."""
    try:
        eq = float(equity)
        if eq <= 0:
            return False
        return float(realized) <= -abs(float(pct) * eq)
    except Exception:
        return False


def risk_budget(equity: float) -> float:
    """Besar risiko (USDT) per trade, dihitung proporsional dari equity live."""
    eq = float(equity or 0.0)
    pct = RISK_TIERS[0][1]
    for thr, p in RISK_TIERS:
        if eq >= thr:
            pct = p
    pct = min(pct, RISK_PCT_CAP)
    amt = round(eq * pct, 2)
    if RISK_USDT > 0:
        amt = min(amt, RISK_USDT)
    return max(0.30, amt)


def risk_max_budget(equity: float) -> float:
    """Plafon risk setelah bump notional minimum exchange."""
    if RISK_MAX > 0:
        return RISK_MAX
    return max(1.0, round(risk_budget(equity) * 1.6, 2))

# Correlation clustering: max 1 position per cluster to prevent systematic liquidation
CLUSTERS = {
    "COMMODITY": ["XAU/USDT:USDT"],
    "BTC_PROXY": ["BTC/USDT:USDT", "BCH/USDT:USDT", "LTC/USDT:USDT"],
    "ETH_DEFI": ["ETH/USDT:USDT", "UNI/USDT:USDT", "AAVE/USDT:USDT", "COMP/USDT:USDT", "ENA/USDT:USDT", "LINK/USDT:USDT"],
    "SOL_MEME": ["SOL/USDT:USDT", "WIF/USDT:USDT", "DOGE/USDT:USDT", "PUMP/USDT:USDT"],
    "L1_INFRA": ["BNB/USDT:USDT", "AVAX/USDT:USDT", "SUI/USDT:USDT", "APT/USDT:USDT", "NEAR/USDT:USDT", "ADA/USDT:USDT", "INJ/USDT:USDT", "TIA/USDT:USDT"],
    "L2_MODULAR": ["ARB/USDT:USDT", "STRK/USDT:USDT", "ALT/USDT:USDT", "ZRO/USDT:USDT"],
    "PRIVACY_OTHER": ["XMR/USDT:USDT", "ZEC/USDT:USDT", "TAO/USDT:USDT", "WLD/USDT:USDT", "FIL/USDT:USDT", "ONDO/USDT:USDT", "TRUMP/USDT:USDT"]
}

def get_symbol_cluster(sym: str) -> str:
    for cluster, symbols in CLUSTERS.items():
        if sym in symbols:
            return cluster
    return "MISC"

def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

_UNI_STATE = {"sig": None}

def build_universe(t):
    """Daftar simbol yang dipindai pada siklus ini.

    market : seluruh perpetual USDT-M yang aktif, volume 24 jam >= SCAN_MIN_VOL_USDT,
             tanpa stablecoin/fiat & token leveraged, di-pre-rank volume desc,
             dipotong SCAN_PRE_RANK. Tujuannya: bot tidak terkunci di 45 koin
             hardcode, dan koin tipis (biaya > edge) tersaring otomatis.
    static : WATCHLIST lama, apa adanya.
    Kalau API gagal, jatuh ke WATCHLIST supaya bot tidak berhenti trading.
    """
    if SCAN_MODE != "market":
        return list(WATCHLIST), "static"
    try:
        with contextlib.redirect_stdout(sys.stderr):
            tickers = t.exchange.fetch_tickers()
            markets = t.exchange.load_markets()
    except Exception as e:
        log(f"UNIVERSE: fetch_tickers gagal ({type(e).__name__}: {str(e)[:80]}) - fallback WATCHLIST")
        return list(WATCHLIST), "fallback"
    picks = []
    for sym, tk in (tickers or {}).items():
        try:
            m = markets.get(sym) or {}
            if not m or not m.get("active", True):
                continue
            if not (m.get("swap") and m.get("linear")):
                continue
            if (m.get("quote") or "").upper() != "USDT":
                continue
            base = (m.get("base") or "").upper()
            if not base or base in SCAN_EXCLUDE_BASES:
                continue
            if base.endswith(SCAN_LEV_SUFFIX):
                continue
            vol = float(tk.get("quoteVolume") or 0.0)
            if vol < SCAN_MIN_VOL_USDT:
                continue
            picks.append((sym, vol))
        except Exception:
            continue
    if not picks:
        log("UNIVERSE: nol simbol lolos filter likuiditas - fallback WATCHLIST")
        return list(WATCHLIST), "fallback"
    picks.sort(key=lambda x: -x[1])
    return [p[0] for p in picks[:SCAN_PRE_RANK]], "market(%d likuid)" % len(picks)

def rank_candidates(candidates):
    """Urutkan kandidat. band: |vol_ratio - center| terkecil dulu; surge: terbesar dulu."""
    if SCAN_RANK_MODE == "surge":
        return sorted(candidates, key=lambda x: x[3], reverse=True)
    return sorted(candidates, key=lambda x: abs(x[3] - SCAN_VOL_BAND_CENTER))



def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"active_trades": {}, "active_trade": None, "last_scan": 0}

def save_state(s):
    try:
        # MERGE anti-clobber: proses cron yang jalan lama bisa punya salinan state
        # basi dan menimpa active_trades yang baru ditulis proses lain.
        try:
            with open(STATE_FILE, "r") as _f:
                _disk = json.load(_f)
            _closed = set(s.get("closed_syms") or []) | set(_disk.get("closed_syms") or [])
            if _closed:
                s["closed_syms"] = sorted(_closed)

            _today_norm = time.strftime("%Y-%m-%d", time.gmtime())
            if str(s.get("day_key") or "") != _today_norm:
                s["day_key"] = _today_norm
                s["day_entries"] = 0
            elif str(_disk.get("day_key") or "") == _today_norm:
                s["day_entries"] = max(int(s.get("day_entries") or 0), int(_disk.get("day_entries") or 0))

            _d_at = _disk.get("active_trades") or {}
            _m_at = s.get("active_trades") or {}
            for _k, _v in _d_at.items():
                if _k not in _m_at and _k not in _closed:
                    _m_at[_k] = _v
            s["active_trades"] = _m_at

            # Simbol yang sedang TERBUKA tidak boleh nyangkut di closed_syms. Kalau
            # nyangkut, merge di atas menolak memulihkannya dari disk -> meta posisi
            # hidup hilang begitu ada proses yang menyimpan state basi (INJ kena ADOPT
            # 22:56 lalu hilang dari state tiap selftest jalan).
            if s.get("closed_syms"):
                _live = set(_m_at.keys())
                s["closed_syms"] = sorted(x for x in s["closed_syms"] if x not in _live)

            _d_ex = _disk.get("excursions") or {}
            _m_ex = s.get("excursions") or {}
            for _k, _v in _d_ex.items():
                if _k in _closed:
                    continue
                if _k not in _m_ex:
                    _m_ex[_k] = _v
                else:
                    _m_ex[_k]["mfe_pct"] = max(float(_m_ex[_k].get("mfe_pct") or 0), float(_v.get("mfe_pct") or 0))
                    _m_ex[_k]["mae_pct"] = min(float(_m_ex[_k].get("mae_pct") or 0), float(_v.get("mae_pct") or 0))
            s["excursions"] = _m_ex
        except Exception:
            pass
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"save_state error: {e}")

def get_open_positions(t):
    """Return list of open positions."""
    with contextlib.redirect_stdout(sys.stderr):
        return [p for p in t.exchange.fetch_positions() if float(p.get("contracts") or 0) != 0]

RECON_SIDECAR = os.getenv("RECON_SIDECAR", os.path.join(BASE_DIR, ".recon_ids.json"))
RECON_SIDECAR_MAX = 800


def _recon_ids_load():
    """Id fill yang sudah pernah dibukukan, dibaca dari sidecar.

    Kenapa sidecar: recon_ids tinggal di state, dan state bisa hilang (reset,
    selftest, restore backup). Begitu hilang, close berikutnya menjumlah ulang
    SEMUA fill sejak entry - Q terbukti dobel: PARTIAL +0.9383 lalu close final
    +1.8970 padahal fill ketiga cuma +0.9590. Sidecar tidak ikut save_state.
    """
    try:
        with open(RECON_SIDECAR, "r") as f:
            d = json.load(f) or {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _recon_ids_save(d):
    try:
        tmp = RECON_SIDECAR + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, RECON_SIDECAR)
    except Exception as e:
        log(f"RECON IDS SAVE FAILED: {e}")


def _recon_fills(t, raw, st, sym):
    """Ambil fill yang BELUM pernah dicatat, berdasarkan trade id.

    Kenapa bukan window waktu: dulu kode menjumlah SEMUA fill sejak entry ts - 600s.
    Untuk simbol yang pernah ditradingkan sebelumnya, fill lama ikut kejumlah
    (ZEC ditulis +0.5877 padahal realized aslinya -0.0101) dan fill baru bisa
    kelewat (BR ditulis -0.0853 padahal -0.4509). Trade id tidak bisa salah
    hitung seperti itu.
    """
    seen = set(st.get("recon_ids", {}).get(sym) or [])
    seen |= set(_recon_ids_load().get(sym) or [])
    with contextlib.redirect_stdout(sys.stderr):
        tr = t.exchange.fapiPrivateGetUserTrades({"symbol": raw, "limit": 500})
    fresh = [x for x in tr if str(x.get("id")) not in seen]
    fresh.sort(key=lambda x: int(x["time"]))
    pnl = sum(float(x["realizedPnl"]) for x in fresh)
    fee = sum(float(x["commission"]) for x in fresh)
    return pnl, fee, [str(x.get("id")) for x in fresh], (int(fresh[-1]["time"]) if fresh else 0)


def _recon_funding(t, raw, sym, st, since_ms):
    """Funding fee sejak posisi dibuka, sebagai BIAYA (positif).

    Kenapa perlu: log_close_if_flat cuma menjumlah COMMISSION dari userTrades.
    Funding dibayar per 8 jam dan tidak menempel di fill mana pun, jadi selama ini
    luput total. BTW ditulis net +0.0028 padahal setelah funding -0.0031 hasilnya
    -0.0003 (rugi). Dikembalikan sebagai delta supaya TP parsial dan close final
    tidak menghitung periode yang sama dua kali.
    """
    try:
        since_ms = int(since_ms or 0)
    except Exception:
        since_ms = 0
    if since_ms <= 0:
        return 0.0, 0.0
    try:
        with contextlib.redirect_stdout(sys.stderr):
            inc = t.exchange.fapiPrivateGetIncome({
                "symbol": raw, "incomeType": "FUNDING_FEE",
                "startTime": since_ms, "limit": 500,
            })
    except Exception:
        return 0.0, 0.0
    tot_cost = -sum(float(x.get("income") or 0) for x in inc)
    seen = float((st.get("recon_funding") or {}).get(sym) or 0.0)
    return tot_cost - seen, tot_cost


def _mark_funding(st, sym, total_cost):
    rf = st.get("recon_funding") or {}
    rf[sym] = round(float(total_cost), 8)
    st["recon_funding"] = rf


def _mark_recon(st, sym, ids):
    d = st.get("recon_ids") or {}
    cur = list(d.get(sym) or [])
    cur.extend(ids)
    d[sym] = cur[-400:]
    st["recon_ids"] = d
    side = _recon_ids_load()
    s = list(side.get(sym) or [])
    for i in ids:
        if i not in s:
            s.append(i)
    side[sym] = s[-RECON_SIDECAR_MAX:]
    _recon_ids_save(side)


SLIP = os.getenv("SLIP_FILE", os.path.join(BASE_DIR, "slippage.csv"))
MAKER_PROBE = os.getenv("MAKER_PROBE_FILE", os.path.join(BASE_DIR, "maker_probe.csv"))
MAKER_SHADOW = os.getenv("MAKER_SHADOW_FILE", os.path.join(BASE_DIR, "maker_shadow.csv"))


def _news_tag(ts_ms, window_min=45):
    """Label saja: apakah detik close ini berdekatan dengan rilis berita merah USD.

    Bukan untuk mengambil aksi apa pun. Tujuannya mengumpulkan bukti seberapa mahal
    slippage saat berita, supaya keputusan berikutnya punya dasar angka.
    """
    try:
        import datetime as _dt
        evs = json.load(open(os.path.join(BASE_DIR, "forex_factory_cache.json"), encoding="utf-8"))
    except Exception:
        return 0, "", 0.0
    try:
        t0 = _dt.datetime.fromtimestamp(int(ts_ms) / 1000.0, _dt.timezone.utc)
    except Exception:
        return 0, "", 0.0
    best_d = None
    best_t = ""
    for e in evs or []:
        if str(e.get("impact", "")).strip().lower() != "high":
            continue
        if str(e.get("country", "")).strip().upper() not in ("USD", "ALL"):
            continue
        try:
            ed = _dt.datetime.fromisoformat(str(e.get("date")))
        except Exception:
            continue
        if ed.tzinfo is None:
            ed = ed.replace(tzinfo=_dt.timezone.utc)
        d = (ed - t0).total_seconds() / 60.0
        if best_d is None or abs(d) < abs(best_d):
            best_d, best_t = d, str(e.get("title") or "")
    if best_d is None:
        return 0, "", 0.0
    # Judul hanya ditempel kalau close-nya benar-benar dekat event. Tanpa batas ini,
    # event yang sudah lewat 8 jam tetap jadi "berita terdekat" dan kolomnya menyesatkan.
    if abs(best_d) > NEWS_TAG_TITLE_WINDOW_MIN:
        best_t = ""
    return (1 if abs(best_d) <= window_min else 0), best_t, round(best_d, 1)


def _recon_slip(t, raw, ids, sl_price, side):
    """Harga SL yang dipasang vs harga fill sebenarnya.

    SL kita STOP_MARKET: begitu tersentuh jadi market order, jadi saat berita merah
    fill bisa jauh di belakang SL. Tanpa catatan ini, "rugi 1R" cuma asumsi.
    """
    try:
        sl_price = float(sl_price or 0)
        want = set(str(i) for i in (ids or []))
        if sl_price <= 0 or not want:
            return None
        with contextlib.redirect_stdout(sys.stderr):
            tr = t.exchange.fapiPrivateGetUserTrades({"symbol": raw, "limit": 500})
        f = [x for x in tr if str(x.get("id")) in want]
        qsum = sum(abs(float(x.get("qty") or 0)) for x in f)
        if qsum <= 0:
            return None
        avg = sum(abs(float(x.get("qty") or 0)) * float(x.get("price") or 0) for x in f) / qsum
        adverse = (sl_price - avg) if str(side).upper() == "LONG" else (avg - sl_price)
        return {"fill": avg, "sl": sl_price,
                "slip_pct": adverse / sl_price * 100.0, "slip_usd": adverse * qsum}
    except Exception:
        return None


def _tp_legs_for(tm, side, qty, cur_price=None):
    """Level TP yang BENAR-BENAR dipasang saat entry, bukan level strategi.

    Dulu re-arm memakai tm["tp1"]/tm["tp2"] (level strategi, tp2 ~ 2R) padahal
    ladder menaruh TP2 di 1,2R. Akibatnya kalau order TP hilang, leg yang
    dipulihkan duduk di harga yang tidak pernah dipakai strategi. Sekarang
    urutannya: tp_legs (hasil nyata) -> rekonstruksi dari entry/SL -> fallback lama.
    """
    out = []
    for l in (tm.get("tp_legs") or []):
        try:
            out.append((float(l[1]), float(l[2])))
        except Exception:
            continue
    if out:
        return out
    try:
        _en = float(tm.get("entry") or 0)
        _sl = float(tm.get("sl") or 0)
    except Exception:
        _en = _sl = 0.0
    if _en > 0 and _sl > 0 and abs(_en - _sl) > 0:
        _r = abs(_en - _sl)
        _sg = 1.0 if str(side).upper() == "LONG" else -1.0
        _lv = list(LADDER_LEVELS)
        _w = list(LADDER_WEIGHTS)
        _sw = sum(_w)
        for _m, _ww in zip(_lv, _w):
            out.append((max(0.0, float(qty) * (_ww / _sw) * (1.0 - LADDER_RUNNER_PCT)), _en + _sg * _r * _m))
        return out
    if tm.get("tp1") and tm.get("tp2"):
        return [(float(qty) * 0.60, float(tm["tp1"])), (float(qty) * 0.25, float(tm["tp2"]))]
    return []


def log_close_if_flat(t, st):
    """CLOSE_LOG: reconcile finished trades into trade_closed.csv with net USDT and R."""
    at_dict = st.get("active_trades") or {}
    legacy_at = st.get("active_trade")
    ats = list(at_dict.values()) if at_dict else ([legacy_at] if legacy_at else [])
    if not ats:
        return

    try:
        with contextlib.redirect_stdout(sys.stderr):
            live = [p for p in t.exchange.fetch_positions() if float(p.get("contracts") or 0) != 0]
    except Exception as e:
        log(f"close-log positions error: {e}")
        return

    live_symbols = {p.get("symbol") for p in live}
    state_changed = False

    # TP PARSIAL: qty menyusut tapi posisi belum flat -> realized PnL-nya tidak
    # pernah kecatat (Q: 112 -> 56 unit hilang dari akuntansi). Catat di sini,
    # lalu geser baseline ts supaya tidak dobel waktu posisi akhirnya ditutup.
    _live_qty = {p.get("symbol"): abs(float(p.get("contracts") or 0)) for p in live}
    for at in list(ats):
        sym = at.get("symbol")
        if not sym or sym not in _live_qty:
            continue
        at.setdefault("entry_ts", at.get("ts"))
        _q_now = _live_qty[sym]
        _q0 = float(at.get("qty") or 0)
        if _q0 <= 0 or _q_now >= _q0 * QTY_COVER_TOL:
            continue
        raw = str(sym).split(":")[0].replace("/", "")
        try:
            _g, _f, _pids, _last = _recon_fills(t, raw, st, sym)
        except Exception as e:
            log(f"partial TP fetch error {sym}: {e}")
            continue
        if not _pids:
            continue
        _fd, _ft = _recon_funding(t, raw, sym, st, at.get("entry_ts") or at.get("ts") or 0)
        _f += _fd
        _net = _g - _f
        _risk = float(at.get("risk") or 0.0)
        try:
            new = not os.path.exists(CLOSED)
            with open(CLOSED, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["ts", "symbol", "side", "entry", "qty", "gross", "fee", "net", "risk", "R"])
                w.writerow([int(time.time()), sym + "/PARTIAL", at.get("side"), at.get("entry"),
                            round(_q0 - _q_now, 8), round(_g, 4), round(_f, 4), round(_net, 4),
                            _risk, round(_net / _risk, 2) if _risk else 0.0])
        except Exception as e:
            log(f"partial TP write error {sym}: {e}")
            continue
        log(f"PARTIAL TP {sym}: {_q0 - _q_now} unit closed, net {_net:+.4f} R {(_net/_risk if _risk else 0):+.2f} | sisa {_q_now}")
        _mark_recon(st, sym, _pids)
        _mark_funding(st, sym, _ft)
        at["qty"] = _q_now
        at["ts"] = int(_last / 1000) if _last else int(at.get("ts") or 0)
        at_dict[sym] = at
        state_changed = True

    for at in list(ats):
        sym = at.get("symbol")
        if not sym or sym in live_symbols:
            continue

        raw = str(sym).split(":")[0].replace("/", "")
        pnl = fee = 0.0
        _ids = []
        n_tr = 0
        try:
            pnl, fee, _ids, _last_ts = _recon_fills(t, raw, st, sym)
            n_tr = len(_ids)
        except Exception as e:
            log(f"close-log fetch error {sym}: {e}")

        if n_tr == 0:
            log(f"close-log: no fills for {raw} since entry - retry next run")
            continue

        _fd, _ft = _recon_funding(t, raw, sym, st, at.get("entry_ts") or at.get("ts") or 0)
        if _fd:
            log(f"FUNDING {sym}: {_fd:+.4f} (kumulatif {_ft:+.4f})")
        fee += _fd
        net = pnl - fee
        risk = float(at.get("risk") or 0.0)
        r = net / risk if risk else 0.0
        try:
            new = not os.path.exists(CLOSED)
            with open(CLOSED, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["ts", "symbol", "side", "entry", "qty", "gross", "fee", "net", "risk", "R"])
                w.writerow([int(time.time()), sym, at["side"], at["entry"], at["qty"],
                            round(pnl, 4), round(fee, 4), round(net, 4), risk, round(r, 2)])
        except Exception as e:
            log(f"close-log write error {sym}: {e}")

        # --- catat MFE/MAE trade yang sudah selesai ---
        try:
            _e = (st.get("excursions") or {}).get(sym) or {}
            _excf = os.path.join(BASE_DIR, "trade_excursion.csv")
            _newx = not os.path.exists(_excf)
            with open(_excf, "a", newline="") as f:
                _w = csv.writer(f)
                if _newx:
                    _w.writerow(["ts", "symbol", "side", "entry", "net", "R", "mfe_pct", "mae_pct", "sl_pct"])
                _slp = abs(float(at.get("entry") or 0) - float(at.get("sl") or 0)) / float(at.get("entry") or 1) * 100
                _w.writerow([int(time.time()), sym, at["side"], at["entry"], round(net, 4), round(r, 2),
                             _e.get("mfe_pct", 0.0), _e.get("mae_pct", 0.0), round(_slp, 3)])
            if sym in (st.get("excursions") or {}):
                del st["excursions"][sym]
        except Exception as e:
            log(f"excursion write error {sym}: {e}")

        try:
            with contextlib.redirect_stdout(sys.stderr):
                t.cancel_all_orders(sym)
        except Exception as e:
            log(f"close-log cancel error {sym}: {e}")

        # --- SLIP-LOG: SL terpasang vs harga fill nyata ---
        # SL kita STOP_MARKET: begitu tersentuh jadi market order, jadi saat berita merah
        # fill bisa jauh di belakang SL. Tanpa catatan ini, "rugi 1R" cuma asumsi.
        try:
            _sl_ref = float(at.get("sl_live") or at.get("sl") or 0)
            _sp = _recon_slip(t, raw, _ids, _sl_ref, at.get("side"))
            if _sp:
                _nf, _nt, _nd = _news_tag(int(time.time() * 1000))
                _newsp = not os.path.exists(SLIP)
                with open(SLIP, "a", newline="") as f:
                    _w2 = csv.writer(f)
                    if _newsp:
                        _w2.writerow(["ts", "symbol", "side", "sl", "fill", "slip_pct",
                                      "slip_usd", "near_news", "news_title", "mins_to_news"])
                    _w2.writerow([int(time.time()), sym, at.get("side"), round(_sp["sl"], 8),
                                  round(_sp["fill"], 8), round(_sp["slip_pct"], 4),
                                  round(_sp["slip_usd"], 4), _nf, _nt, _nd])
                log(f"SLIP {sym}: SL {_sp['sl']:g} fill {_sp['fill']:g} "
                    f"{_sp['slip_pct']:+.3f}% (${_sp['slip_usd']:+.4f}) news={_nf}")
        except Exception as e:
            log(f"slip log error {sym}: {e}")

        _mark_recon(st, sym, _ids)
        _mark_funding(st, sym, _ft)
        if sym in at_dict:
            del at_dict[sym]
        _cl = st.get("closed_syms") or []
        if sym not in _cl:
            _cl.append(sym)
        st["closed_syms"] = _cl
        if st.get("active_trade") and st["active_trade"].get("symbol") == sym:
            st["active_trade"] = None
        state_changed = True

        log(f"CLOSED {sym} {at['side']} gross={pnl:+.4f} fee={fee:.4f} net={net:+.4f} R={r:+.2f}")
        try:
            TelegramNotifier().send_message(
                f"<b>[CLOSED]</b> {sym} {at['side']}\n"
                f"Entry ${at['entry']} qty {at['qty']}\n"
                f"Gross <b>{pnl:+.4f}</b> | Fee {fee:.4f} | Net <b>{net:+.4f} USDT</b> | <b>{r:+.2f}R</b>"
            )
        except Exception as e:
            log(f"Telegram notify error: {e}")

    if state_changed:
        st["active_trades"] = at_dict
        save_state(st)

def sweep_orphan_orders(t):
    """Cancel orders that outlived their position."""
    try:
        with contextlib.redirect_stdout(sys.stderr):
            held = [p for p in t.exchange.fetch_positions() if float(p.get("contracts") or 0) != 0]
    except Exception as e:
        log(f"orphan sweep: positions error {e}")
        return 0

    held_syms = {p.get("symbol") for p in held}
    n = 0
    try:
        with contextlib.redirect_stdout(sys.stderr):
            for sym in {o["symbol"] for o in t.exchange.fetch_open_orders()} - held_syms:
                try:
                    t.cancel_all_orders(sym)
                    n += 1
                except Exception:
                    pass
    except Exception as e:
        log(f"orphan sweep: cancel error {e}")
    if n:
        log(f"ORPHAN SWEEP: cancelled {n} order(s) with no position")
    return n

def _db_syms():
    """Simbol milik modul 1D daily_breakout — jangan disentuh bot 4h."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_breakout_state.json")
        with open(path, encoding="utf-8") as f:
            return set(json.load(f).keys())
    except Exception:
        return set()


def manage_open_positions(t):
    """Monitor ALL live positions, update trailing stop / break-even independently."""
    positions = get_open_positions(t)
    if not positions:
        return []

    st = load_state()
    at_dict = st.get("active_trades") or {}
    legacy_at = st.get("active_trade")

    managed_syms = []
    db_syms = _db_syms()
    for pos in positions:
        symbol = pos["symbol"]
        if symbol in db_syms:
            log(f"SKIP {symbol}: posisi modul 1D, bot 4h tidak ikut campur")
            continue
        qty = abs(float(pos["contracts"]))
        entry = float(pos.get("entryPrice") or 0)
        if entry <= 0:
            entry = float((load_pos_meta().get(symbol) or {}).get("entry") or 0)
        if entry <= 0:
            log(f"SKIP {symbol}: entry price kosong, trailing dilewati")
            continue
        side = "LONG" if str(pos.get("side", "")).lower() in ("long", "buy") else "SHORT"
        managed_syms.append(symbol)

        with contextlib.redirect_stdout(sys.stderr):
            tk = t.exchange.fetch_ticker(symbol)
            algos = t.get_conditional_orders(symbol)

        cur_price = float(tk["last"])
        pnl_pct = (cur_price - entry) / entry if side == "LONG" else (entry - cur_price) / entry

        dec = 2 if "XAU" not in symbol else 1
        try:
            mkt = t.exchange.market(symbol)
            pf = next((f for f in (mkt.get("info") or {}).get("filters", []) if f.get("filterType") == "PRICE_FILTER"), None)
            ts = float(pf.get("tickSize")) if pf else float(mkt.get("precision", {}).get("price") or 0.01)
            if ts > 0:
                dec = max(0, -int(math.floor(math.log10(ts) + 1e-9)))
        except Exception:
            pass

        stops = [float(a.get("triggerPrice") or a.get("stopPrice") or 0.0) for a in algos
                 if "STOP" in (a.get("orderType") or "").upper()]
        stops = [x for x in stops if x > 0]
        if side == "LONG":
            cand = [x for x in stops if x < cur_price] or stops
            cur_sl = max(cand) if cand else 0.0
        else:
            cand = [x for x in stops if x > cur_price] or stops
            cur_sl = min(cand) if cand else 0.0

        # Retrieve initial SL from active_trades dict or legacy
        trade_meta = at_dict.get(symbol) or (legacy_at if legacy_at and legacy_at.get("symbol") == symbol else {})
        # ADOPT: posisi live tanpa catatan di state (misal state ke-reset) -> rekonstruksi
        # biar trailing + pencatatan close tetap jalan.
        if not trade_meta:
            _adopt_sl = float(cur_sl or 0.0)
            _ats = int(time.time())
            try:
                _raw_ad = str(symbol).split(":")[0].replace("/", "")
                with contextlib.redirect_stdout(sys.stderr):
                    _tr_ad = t.exchange.fapiPrivateGetUserTrades({"symbol": _raw_ad, "limit": 200})
                _ats = entry_ts_from_trades(_tr_ad, _ats)
            except Exception:
                pass
            trade_meta = {"symbol": symbol, "side": side, "entry": entry, "qty": qty,
                          "sl": _adopt_sl, "tp1": None, "tp2": None, "tp_legs": [],
                          "risk": round(qty * abs(entry - _adopt_sl), 4) if _adopt_sl else 0.0,
                          "risk_est": True,
                          "ts": _ats, "adopted": True}
            # risk di sini dihitung dari SL yang SEDANG terpasang (bisa sudah trailing),
            # bukan SL awal -> R-nya taksiran. Ditandai biar tidak dihitung sebagai sampel.
            at_dict[symbol] = trade_meta
            st["active_trades"] = at_dict
            log(f"ADOPT {symbol} {side} qty {qty} entry {entry} sl {_adopt_sl} (tidak ada di state)")

        # trade_meta bisa HILANG (ADOPT / state clobber) -> pakai pos_meta, terakhir backfill
        if not trade_meta:
            _pm = load_pos_meta()
            trade_meta = _pm.get(symbol) or {}
            if not trade_meta:
                # entry dari posisi exchange TIDAK boleh 0: kalau 0, gainR jadi 0
                # dan trailing/SL-lock tidak pernah jalan. Ambil dari pos['entry'].
                _en = float(entry or p.get("entry") or p.get("entryPrice") or 0)
                if _en <= 0:
                    log(f"POS META skip {symbol}: entry price tidak tersedia - trailing dilewati siklus ini")
                else:
                    entry = _en
                    trade_meta = backfill_pos_meta(symbol, entry, side, cur_sl, qty)
                if trade_meta:
                    _pm[symbol] = trade_meta
                    save_pos_meta(_pm)
                    log(f"POS META backfill {symbol}: entry {entry} side {side} sl {cur_sl} qty {qty} risk {trade_meta.get('risk')}")
        _sl0 = float(trade_meta.get("sl") or 0.0)
        # FALLBACK: kalau state nggak nyimpen SL, pakai SL yang benar-benar
        # aktif di exchange (algo/conditional order). Tanpa ini r_dist=0
        # dan trailing stop TIDAK PERNAH jalan (profit dibiarkan balik ke SL awal).
        if _sl0 <= 0:
            _sl0 = float(cur_sl or 0.0)
        r_dist = abs(entry - _sl0) if _sl0 else 0.0
        gain = (cur_price - entry) if side == "LONG" else (entry - cur_price)

        # SLAH SATU sumber logika (trail_lock_sl) supaya BE/ratchet tidak bisa
        # lupa dipasang di satu cabang seperti sebelumnya. Semua posisi, tiap siklus.
        # Kalau posisi sedang RUGI, tidak ada profit untuk dikunci. Tanpa guard ini,
        # lock_sl dihitung di sisi yang salah dan SL diminta di bawah harga
        # (BR: -2021 "Order would immediately trigger").
        _in_profit = gain > 0
        lock_sl = trail_lock_sl(side, entry, cur_price, r_dist, dec) if (r_dist > 0 and _in_profit) else None
        # Lapis kedua: kalau gerak harga sudah besar tapi R masih kecil (koin
        # volatil / SL lebar), kunci berdasarkan persen gerak, jangan tunggu 0.8R.
        if entry > 0:
            _atr_pct = abs(r_dist / entry) * 100 if r_dist else 0.0
            _lk = trail_lock_pct(side, entry, abs(pnl_pct) * 100, _atr_pct) if (_atr_pct and _in_profit) else None
            if _lk is not None:
                _lv = entry + r_dist * _lk if side == "LONG" else entry - r_dist * _lk
                _lv = round(_lv, dec) if _lk else round(entry * (1.001 if side == "LONG" else QTY_COVER_TOL), dec)
                if lock_sl is None:
                    lock_sl = _lv
                else:
                    lock_sl = max(lock_sl, _lv) if side == "LONG" else min(lock_sl, _lv)

        # --- MFE / MAE TRACKING ---
        # MFE = puncak profit tertinggi (persen), MAE = drawdown terdalam.
        # Ini SATU-SATUNYA cara tau apakah TP kita kepagian atau kekecengan.
        try:
            _exc = st.get("excursions") or {}
            _e = _exc.get(symbol) or {"mfe_pct": 0.0, "mae_pct": 0.0}
            if pnl_pct * 100 > float(_e.get("mfe_pct") or 0.0):
                _e["mfe_pct"] = round(pnl_pct * 100, 3)
            if pnl_pct * 100 < float(_e.get("mae_pct") or 0.0):
                _e["mae_pct"] = round(pnl_pct * 100, 3)
            _exc[symbol] = _e
            st["excursions"] = _exc
        except Exception:
            pass

        # Simpan SL yang BENAR-BENAR terpasang di exchange. at["sl"] adalah SL awal dari
        # sinyal; setelah trailing angka itu basi, dan pengukuran slippage jadi salah.
        try:
            if cur_sl:
                trade_meta["sl_live"] = float(cur_sl)
                at_dict[symbol] = trade_meta
                st["active_trades"] = at_dict
        except Exception as _sle:
            # Dulu `pass`: kalau ini gagal, sl_live basi dan pengukuran R/slip salah
            # tanpa satu baris pun di log.
            log(f"SL LIVE SAVE FAILED {symbol}: {err_detail(_sle)}")

        log(f"HB {symbol} {side} qty {qty} px {cur_price} pnl {pnl_pct*100:+.2f}% gainR {gain/r_dist if r_dist else 0:.2f} sl {cur_sl or 0}")

        # TP re-arm check
        try:
            with contextlib.redirect_stdout(sys.stderr):
                _oo = t.exchange.fetch_open_orders(symbol)
        except Exception:
            _oo = None

        if _oo is not None:
            _cs = "sell" if side == "LONG" else "buy"
            _rf = int(trade_meta.get("rearm_fail") or 0)
            _open_close = [o for o in _oo if str(o.get("side") or "").lower() == _cs]
            _have_px, _have_q = [], 0.0
            for o in _open_close:
                try:
                    _have_px.append(float(o.get("price") or 0))
                    _have_q += float(o.get("amount") or 0)
                except Exception:
                    continue
            # BUG FIX: posisi yang dibuka sebelum tp_legs ada di state tidak punya
            # catatan leg sama sekali. Kalau order TP-nya hilang semua, re-arm membangun
            # ulang ladder dari entry/SL dan bisa memasang LAGI level yang sudah kena TP
            # (Q: 1,2R sudah fill 50%). Jadi catat dulu leg yang masih hidup; re-arm
            # sesudah ini selalu punya sumber kebenaran dari exchange.
            if not (trade_meta.get("tp_legs") or []) and _open_close:
                try:
                    _ent = float(trade_meta.get("entry") or 0.0)
                    _slm = float(trade_meta.get("sl") or 0.0)
                    _rd = abs(_ent - _slm)
                    _bf = []
                    for _o in _open_close:
                        _op = float(_o.get("price") or 0.0)
                        _oq = float(_o.get("amount") or 0.0)
                        if _op <= 0 or _oq <= 0:
                            continue
                        _mm = (abs(_op - _ent) / _rd) if _rd > 0 else 0.0
                        _bf.append([round(_mm, 2), _oq, _op])
                    if _bf:
                        _bf.sort(key=lambda x: x[0])
                        trade_meta["tp_legs"] = _bf
                        at_dict[symbol] = trade_meta
                        st["active_trades"] = at_dict
                        log("TP LEGS BACKFILL %s: %s" % (
                            symbol, " | ".join("%s@%s(%sR)" % (q, p, m) for m, q, p in _bf)))
                except Exception as _be:
                    log(f"TP LEGS BACKFILL FAILED {symbol}: {err_detail(_be)}")

            # Kandidat re-arm:
            #  (a) leg yang GAGAL dipasang saat entry -> dijamin belum pernah terisi
            #  (b) tidak ada order TP sama sekali -> bangun ulang seluruh ladder
            _fail_legs = [l for l in (trade_meta.get("tp_failed") or [])
                          if isinstance(l, (list, tuple)) and len(l) >= 3]
            _cand = [(float(_l[1]), float(_l[2]), _i) for _i, _l in enumerate(_fail_legs)]
            if not _cand and not _open_close:
                _cand = [(float(_q), float(_p), None) for _q, _p in _tp_legs_for(trade_meta, side, qty)]
            if _cand and _rf < REARM_MAX_TRIES:
                try:
                    _missed = set(trade_meta.get("missed") or [])
                    _rem = float(qty) - _have_q
                    _plan = []
                    for _lq, _tpp, _idx in _cand:
                        _dup = False
                        for _hp in _have_px:
                            if _hp > 0 and abs(_tpp - _hp) <= max(abs(_tpp) * TP_DUP_TOL, 1e-12):
                                _dup = True
                                break
                        if _dup:
                            continue
                        if tp_leg_passed(side, _tpp, cur_price):
                            # log sekali saja per level, kalau tidak tiap 2 menit nulis ulang
                            if _tpp not in _missed:
                                log(f"TP MISSED {symbol}: level {_tpp} sudah dilewati harga {cur_price} - sisa ikut trailing")
                                _missed.add(_tpp)
                            continue
                        if _rem <= 0:
                            break
                        try:
                            _qq = float(t.exchange.amount_to_precision(symbol, min(_lq, _rem)))
                        except Exception as _qe:
                            # FIX v10: leg terlalu kecil -> pasang satu TP penuh untuk sisa qty.
                            _fq2 = 0.0
                            try:
                                _fq2 = float(t.exchange.amount_to_precision(symbol, _rem))
                            except Exception:
                                _fq2 = 0.0
                            if _fq2 > 0:
                                _plan.append((_fq2, _tpp, _idx))
                                _rem = 0.0
                                log(f"TP REARM SINGLE {symbol}: qty {_fq2} @ {_tpp} - leg terlalu kecil untuk dipecah")
                                break
                            log(f"TP re-arm amount error {symbol}: {err_detail(_qe)}")
                            break
                        if _qq <= 0:
                            continue
                        _rem -= _qq
                        _plan.append((_qq, _tpp, _idx))
                    _re, _bad = [], []
                    for _qq, _tpp, _idx in _plan:
                        with contextlib.redirect_stdout(sys.stderr):
                            _ok = t.place_limit_order(symbol, _cs, _qq, _tpp, reduce_only=True)
                        if _ok:
                            _re.append((_qq, _tpp, _idx))
                        else:
                            _bad.append((_qq, _tpp, _idx))
                    _fix = [_i for _q, _p, _i in _re if _i is not None]
                    if _fix:
                        _tl = list(trade_meta.get("tp_legs") or [])
                        for _i in _fix:
                            _tl.append(_fail_legs[_i])
                        trade_meta["tp_legs"] = _tl
                        trade_meta["tp_failed"] = [l for _i, l in enumerate(_fail_legs) if _i not in _fix]
                    if _missed != set(trade_meta.get("missed") or []):
                        trade_meta["missed"] = sorted(_missed)
                    if _fix or _missed:
                        at_dict[symbol] = trade_meta
                        st["active_trades"] = at_dict
                    if _re:
                        log(f"TP RE-ARM {symbol}: " + " | ".join(f"{q}@{p}" for q, p, _ in _re) + " | runner trailing")
                    if _bad:
                        trade_meta["rearm_fail"] = _rf + 1
                        at_dict[symbol] = trade_meta
                        st["active_trades"] = at_dict
                        log(f"TP RE-ARM GAGAL {symbol}: " + " | ".join(f"{q}@{p}" for q, p, _ in _bad) +
                            f" ({getattr(t, 'last_error', '') or 'place_limit_order balik falsy'}) percobaan {_rf + 1}/3")
                except Exception as te:
                    log(f"TP re-arm failed {symbol}: {err_detail(te)}")

        if lock_sl:
            eps = max(abs(cur_price) * 1e-4, 10.0 ** -dec)
            should_update = (side == "LONG" and lock_sl > cur_sl + eps) or \
                            (side == "SHORT" and (cur_sl == 0 or lock_sl < cur_sl - eps))
            if should_update:
                # set_protective_stop MENELAN exception dan balik falsy. Dulu hasilnya
                # diabaikan -> log nulis "placed OK" padahal Binance menolak -2021
                # (BR: SL diminta di bawah harga). Sekarang cek return-nya.
                _sl_ok = False
                try:
                    with contextlib.redirect_stdout(sys.stderr):
                        _res = t.set_protective_stop(symbol, lock_sl, amount=qty, position_side=side)
                    _sl_ok = bool(_res)
                    if _sl_ok:
                        log(f"Trailing: SL {symbol} {cur_sl or 0} -> {lock_sl} (placed OK)")
                    else:
                        log(f"Trailing FAILED {symbol} -> {lock_sl}: {getattr(t, 'last_error', '') or 'set_protective_stop balik falsy'}")
                except Exception as e:
                    log(f"Trailing FAILED {symbol} -> {lock_sl}: {err_detail(e)}")

                if _sl_ok:
                    try:
                        msg = f"\U0001f512 <b>[PROFIT LOCKED / TRAILING SL]</b>\nSymbol: <b>{symbol}</b> ({side})\nPrice: <b>${cur_price}</b> | SL Rapat: <b>${lock_sl}</b>\nStatus: Profit terkunci, sisa posisi anti-minus!"
                        TelegramNotifier().send_message(msg)
                    except Exception as e:
                        log(f"Telegram notify error: {e}")

    try:
        save_state(st)
    except Exception as e:
        # Dulu ditelan: kalau tulis state gagal, sl_live & akuntansi parsial hilang
        # tanpa satu baris pun di log.
        log(f"STATE SAVE FAILED (manage): {err_detail(e)}")
    return managed_syms

MAKER_TIMEOUT_SEC = 90
MAKER_POLL_SEC = 5
MAKER_MAX_SLIP_ATR = 0.25

def maker_entry(t, sym, side, qty, ref_price, atr):
    """Post-only at the touch. Never chases: abandons if price runs off."""
    oid = None
    try:
        book = t.exchange.fetch_order_book(sym, limit=5)
        side_book = book["bids"] if side.lower() == "buy" else book["asks"]
        if not side_book:
            return None
        px = float(t.exchange.price_to_precision(sym, side_book[0][0]))
        o = t._submit_order(symbol=sym, type="limit", side=side.lower(), amount=qty,
                            price=px, params={"postOnly": True, "timeInForce": "GTX"})
        if not o:
            return None
        oid = o.get("id")
        deadline = time.time() + MAKER_TIMEOUT_SEC
        while time.time() < deadline:
            time.sleep(MAKER_POLL_SEC)
            try:
                o = t.exchange.fetch_order(oid, sym)
            except Exception:
                continue
            status = o.get("status")
            if status == "closed":
                return o
            if status in ("canceled", "rejected", "expired"):
                return None
            try:
                last_px = float(t.exchange.fetch_ticker(sym)["last"])
                slip = abs(last_px - ref_price)
                if slip > MAKER_MAX_SLIP_ATR * atr:
                    log(f"Maker abandon {sym}: slip {slip:.4f} > {MAKER_MAX_SLIP_ATR}*ATR ({MAKER_MAX_SLIP_ATR*atr:.4f})")
                    try:
                        t.cancel_all_orders(sym)
                    except Exception:
                        pass
                    return None
            except Exception:
                pass
        try:
            t.cancel_all_orders(sym)
        except Exception:
            pass
        return None
    except Exception as e:
        log(f"Maker entry error {sym}: {e}")
        return None

def sl_width_pct(price, sl_dist):
    """Lebar SL dalam persen harga. None kalau data tidak valid."""
    try:
        price = float(price)
        sl_dist = abs(float(sl_dist))
    except Exception:
        return None
    if price <= 0 or sl_dist <= 0:
        return None
    return sl_dist / price * 100.0


def sl_width_ok(price, sl_dist):
    """False = entry ditolak karena SL kelewat lebar (Kandidat #1).

    Fail-open: kalau lebar SL tidak bisa dihitung, entry TIDAK diblokir.
    Filter ini cuma menolak SL yang jelas kelebaran, bukan jadi sumber
    entry gagal gara-gara bug hitung.
    """
    if MAX_SL_PCT <= 0:
        return True
    p = sl_width_pct(price, sl_dist)
    if p is None:
        return True
    return p <= MAX_SL_PCT


def scan_and_enter(t):
    """COMPRESSION BREAKOUT v2 - Multi-position with Correlation Guard."""
    # --- NEWS GUARD ---
    try:
        res = ForexFactoryNewsSentinel().check_news_guard()
        if res.get("is_danger", False):
            ev = res.get("imminent_event")
            log(f"NEWS GUARD: {ev.get('title') if ev else 'high-impact event'}. Entries paused.")
            return
    except Exception as e:
        log(f"News guard error: {e}")

    # --- BALANCE ---
    try:
        with contextlib.redirect_stdout(sys.stderr):
            usdt = t.exchange.fetch_balance()["USDT"]
        free = float(usdt.get("free") or 0.0)
        equity = float(usdt.get("total") or 0.0)
    except Exception as e:
        log(f"Balance fetch error: {e}")
        return
    if free < 10.0:
        log(f"Capital too low (free ${free:.2f}) - no entry")
        return

    # --- CIRCUIT BREAKER ---
    try:
        bp = os.path.join(BASE_DIR, ".equity_baseline")
        if not os.path.exists(bp):
            with open(bp, "w") as _f:
                _f.write(f"{equity:.6f}")
        with open(bp) as _f:
            base = float(_f.read().strip())
        breaker_threshold = max(float(os.getenv("CIRCUIT_BREAKER_MAX_LOSS", "5.00")), round(equity * BREAKER_EQUITY_PCT, 2))
        if equity < base - breaker_threshold:
            log(f"CIRCUIT BREAKER: equity ${equity:.2f} < ${base:.2f}-{breaker_threshold:.2f}. Entries halted.")
            return
    except Exception as e:
        log(f"Breaker error: {e}")

    # --- DAILY LOSS CAP (MAX_DAILY_LOSS_PCT, sebelumnya cuma ada di .env tanpa dipakai) ---
    try:
        _dlp = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))
        _utc_mid = (int(time.time()) // 86400) * 86400
        _inc = None
        for _try in range(3):
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    _inc = t.exchange.fapiPrivateGetIncome({"startTime": _utc_mid * 1000, "limit": 1000})
                break
            except Exception:
                if _try == 2:
                    raise
                time.sleep(2)
        _day_pnl = day_pnl_from_income(_inc)
        if daily_loss_blocked(_day_pnl, equity, _dlp):
            log(f"DAILY LOSS CAP: hari ini {_day_pnl:+.2f} USDT <= -{_dlp*100:.0f}% x ${equity:.2f} = {-abs(_dlp*equity):+.2f}. Entry dihentikan sampai 00:00 UTC.")
            return
    except Exception as e:
        # Fallback: endpoint income bisa ditolak -2015 (permission). Breaker harian
        # tidak boleh mati diam-diam, jadi hitung dari close CSV hari UTC ini.
        try:
            _utc_mid = (int(time.time()) // 86400) * 86400
            _day_pnl = 0.0
            with open("trade_closed.csv", encoding="utf-8") as _f:
                for _r in csv.DictReader(_f):
                    try:
                        _ts = float(_r.get("ts") or 0)
                        if _ts > 1e11:
                            _ts /= 1000.0
                        if _ts >= _utc_mid:
                            _day_pnl += float(_r.get("net") or 0)
                    except Exception:
                        continue
            _dlp = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))
            if daily_loss_blocked(_day_pnl, equity, _dlp):
                log(f"DAILY LOSS CAP (fallback CSV): hari ini {_day_pnl:+.2f} USDT. Entry dihentikan sampai 00:00 UTC.")
                return
            log(f"Daily loss cap fallback CSV: {_day_pnl:+.2f} USDT (income ditolak -2015)")
        except Exception as e2:
            log(f"Daily loss cap error: {e} | fallback gagal: {e2}")

    # --- MULTI-POSITION CAPACITY CHECK ---
    open_pos = get_open_positions(t)
    slots_available = MAX_OPEN_POSITIONS - len(open_pos)
    if slots_available <= 0:
        log(f"Max positions ({MAX_OPEN_POSITIONS}) reached ({len(open_pos)} open) - skip scan")
        return

    # Extract clusters already occupied by live positions
    occupied_clusters = {get_symbol_cluster(p["symbol"]) for p in open_pos}
    occupied_symbols = {p["symbol"] for p in open_pos}

    # --- DAILY CAP ---
    st = load_state() or {}
    _today = time.strftime("%Y-%m-%d", time.gmtime())
    _prev = int(st.get("day_entries") or 0) if st.get("day_key") == _today else 0
    if _prev >= DAILY_MAX:
        log(f"Daily cap reached ({_prev}/{DAILY_MAX} untuk hari UTC {_today}; reset 00:00 UTC = 07:00 WIB). Waiting.")
        return

    # --- REGIME GATE ---
    if not regime_go():
        return

    # --- SCAN CLOSED 4h BARS ---
    def _tradeable(sym, meta):
        sd = strategy.SL_ATR * meta["atr"]
        if sd <= 0:
            return False
        _live_risk = round(risk_budget(equity) * float(meta.get("risk_mult", 1.0)), 2)
        raw_q = _live_risk / sd
        m = t.exchange.market(sym)
        _lim = m.get("limits") or {}
        min_q = float((_lim.get("amount") or {}).get("min") or 0)
        min_c = float((_lim.get("cost") or {}).get("min") or 0)
        floor_notional = max(MIN_NOTIONAL, min_c)
        if raw_q * meta["close"] < floor_notional:
            raw_q = max(raw_q, floor_notional / meta["close"])
            if raw_q * sd > risk_max_budget(equity):
                return False
        if raw_q < min_q:
            return False
        return True

    # --- MULTI-TIMEFRAME SCAN: 4h arah utama + 1h tambahan peluang ---
    # 4h = full size. 1h = 60% size, HANYA kalau tren 4h searah (biar nggak lawan arus).
    TF_PLAN = (("4h", 1.0), ("1h", 0.6))

    _universe, _umode = build_universe(t)
    _sig = (_umode, tuple(_universe))
    if _sig != _UNI_STATE["sig"]:
        log("UNIVERSE %s: scan %d simbol" % (_umode, len(_universe)))
        _UNI_STATE["sig"] = _sig

    candidates = []
    for sym in _universe:
        if sym in occupied_symbols:
            continue
        try:
            _ctx = {}
            for _tf, _mult in TF_PLAN:
                with contextlib.redirect_stdout(sys.stderr):
                    raw = t.exchange.fetch_ohlcv(sym, timeframe=_tf, limit=320)
                if len(raw) < strategy.MIN_BARS:
                    continue
                d = strategy.features(pd.DataFrame(raw, columns=["t", "o", "h", "l", "c", "v"]))
                _ctx[_tf] = d
                sig = strategy.signal(d, -2)
                if not sig:
                    continue
                side, meta = sig

                # Setup 1h wajib searah tren 4h
                if _tf != "4h":
                    d4 = _ctx.get("4h")
                    if d4 is None or len(d4) < 3:
                        continue
                    _c4 = float(d4.c.iloc[-2])
                    _e4 = float(d4.ema50.iloc[-2])
                    _e4p = float(d4.ema50.iloc[-3])
                    _up4 = (_c4 > _e4) and (_e4 >= _e4p)
                    _dn4 = (_c4 < _e4) and (_e4 <= _e4p)
                    if side == "BUY" and not _up4:
                        continue
                    if side == "SELL" and not _dn4:
                        continue

                meta["tf"] = _tf
                meta["risk_mult"] = _mult

                # --- KONFIRMASI VOLUME BAR 4h (pre-register 2026-11-08) ---
                # Data 37 trade: volume bar 4h < 0,6x rata-rata 20 bar -> WR 31%, exp -0,10R.
                # Rollback: set MIN_VOLR_4H=0 di .env. Tidak menyentuh RSI/ADX/BB/ladder/risk.
                _vr4 = None
                try:
                    _d4v = _ctx.get("4h")
                    if _d4v is not None and len(_d4v) >= 22:
                        _v4 = float(_d4v.v.iloc[-2])
                        _vm4 = float(_d4v.vma.iloc[-2])
                        _vr4 = (_v4 / _vm4) if _vm4 > 0 else None
                except Exception:
                    _vr4 = None
                meta["volr4h"] = _vr4
                try:
                    _min_vr4 = float(os.getenv("MIN_VOLR_4H", "0.6") or 0)
                except Exception:
                    _min_vr4 = 0.6
                if _vr4 is not None and _min_vr4 > 0 and _vr4 < _min_vr4:
                    log("VETO volr4h %s: volume bar 4h %.2fx < %.2fx (setup %s)" % (sym, _vr4, _min_vr4, _tf))
                    continue
                if st.get("last_signal_ts") == meta["bar_ts"]:
                    continue
                if not _tradeable(sym, meta):
                    log(f"Skip {sym}: qty/notional below exchange minimum")
                    continue
                candidates.append((sym, side, meta, meta["vol_ratio"]))
                break   # sudah dapat setup di TF tertinggi untuk koin ini
        except Exception as e:
            log(f"scan {sym}: {type(e).__name__}: {str(e)[:120]}")
            continue

    if not candidates:
        log("No pullback signal on last closed 4h/1h bar.")
        return

    # Rank candidates: pita volume surge (band) atau surge terbesar (surge)
    candidates = rank_candidates(candidates)

    # Process candidates up to available slots with Correlation Guard
    entered_count = 0
    for sym, side, meta, vol_ratio in candidates:
        if entered_count >= slots_available:
            break

        cluster = get_symbol_cluster(sym)
        if cluster != "MISC" and cluster in occupied_clusters:
            log(f"CORRELATION GUARD: Skip {sym} (cluster {cluster} already active)")
            continue

        action = "buy" if side == "BUY" else "sell"
        side_str = "LONG" if side == "BUY" else "SHORT"
        resolved = t.resolve_symbol(sym)
        with contextlib.redirect_stdout(sys.stderr):
            price = float(t.exchange.fetch_ticker(resolved)["last"])

        sl_dist = strategy.SL_ATR * meta["atr"]
        if sl_dist <= 0 or price <= 0:
            continue

        try:
            dyn_risk = round(risk_budget(equity) * float(meta.get("risk_mult", 1.0)), 2)
            qty = float(t.exchange.amount_to_precision(resolved, dyn_risk / sl_dist))
        except Exception as e:
            log(f"Skip {sym}: qty precision ({e})")
            continue

        if qty <= 0:
            continue

        notional = qty * price
        if notional < MIN_NOTIONAL:
            _m = t.exchange.market(resolved)
            _step = float((_m.get("limits", {}).get("amount") or {}).get("step") or 0)
            if _step <= 0:
                _pa = float((_m.get("precision") or {}).get("amount") or 0)
                _step = _pa if _pa >= 1 else 10 ** (-_pa)
            _raw = MIN_NOTIONAL / price
            if _step > 0:
                _raw = math.ceil(_raw / _step) * _step
            bumped = float(t.exchange.amount_to_precision(resolved, _raw))
            bumped_risk = bumped * sl_dist
            _rmax = risk_max_budget(equity)
            if bumped <= 0 or bumped_risk > _rmax:
                log(f"Skip {sym}: notional ${notional:.2f} < ${MIN_NOTIONAL}, bump risk ${bumped_risk:.2f} > ${_rmax:.2f}")
                continue
            qty = bumped
            notional = qty * price

        max_notional = free * MAX_MARGIN_PCT * MAX_LEVERAGE
        if notional > max_notional:
            qty = float(t.exchange.amount_to_precision(resolved, max_notional / price))
            notional = qty * price
            if qty <= 0:
                continue

        # --- AI DEBATE VETO ---
        ai_reason = "n/a"
        try:
            _rsi = float(meta.get("rsi", 50.0))
            _rsic = ("OVERBOUGHT_DANGER" if _rsi >= 78 else "OVERSOLD_DANGER" if _rsi <= 22
                     else "OVERBOUGHT" if _rsi >= 70 else "OVERSOLD" if _rsi <= 30 else "NEUTRAL")
            _bbu, _bbl = meta.get("bb_up"), meta.get("bb_lo")
            _bbcond = ("ABOVE_UPPER_BAND" if _bbu and price > _bbu else
                       "BELOW_LOWER_BAND" if _bbl and price < _bbl else "INSIDE_BANDS")
            _mh, _mp = float(meta.get("macd_hist", 0.0)), float(meta.get("macd_prev", 0.0))
            _dir = 1 if side_str == "LONG" else -1
            _vwap_val = float(meta.get("vwap") or price)
            _vwap_ok = bool(meta.get("vwap_aligned", True))
            _smc_sweep = str(meta.get("smc_sweep", "none"))
            _smc_fvg = str(meta.get("smc_fvg", "neutral"))
            _smc_trap = (_smc_sweep == "bearish_liquidity_sweep" if _dir > 0 else _smc_sweep == "bullish_liquidity_sweep")
            _chk = {
                "compression": True,
                "breakout": meta.get("breakout_margin", 0.0) > 0,
                "volume": meta["vol_ratio"] > strategy.MIN_VOL_RATIO,
                "trend_macd": bool(meta.get("trend_up") == (_dir > 0)) and (_mh > 0) == (_dir > 0),
                "rsi_not_extreme": not (_rsi >= 78 if _dir > 0 else _rsi <= 22),
                "vwap_institutional": _vwap_ok,
                "smc_no_trap": not _smc_trap,
            }
            _k = sum(1 for v in _chk.values() if v)
            _sd = strategy.SL_ATR * meta["atr"]
            _sl_floor = price - _sd if side_str == "LONG" else price + _sd

            # --- HARD ORCHESTRATOR VALIDATION (ANTI-PUCUK & ANTI-TRAP MUTLAK) ---
            if _smc_trap:
                log(f"ORCHESTRATOR HARD VETO: {sym} rejected due to SMC trap ({_smc_sweep})")
                continue
            if side_str == "LONG" and _smc_sweep == "bearish_liquidity_sweep":
                log(f"ORCHESTRATOR HARD VETO: {sym} LONG rejected - sweep high rejection detected")
                continue
            if side_str == "SHORT" and _smc_sweep == "bullish_liquidity_sweep":
                log(f"ORCHESTRATOR HARD VETO: {sym} SHORT rejected - sweep low rejection detected")
                continue

            # HARD FILTER LINTAS TF: RSI 1h wajib tidak jenuh.
            # Veto RSI lama cuma pakai TF sinyal -> sinyal 4h lolos walau 1h vertikal
            # (INJ: 4h RSI 57.2 lolos < 58, padahal 1h RSI 81.5 -> rugi -1.01R).
            _rsi1h = None
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    _o1 = t.exchange.fetch_ohlcv(sym, timeframe="1h", limit=320)
                _d1 = pd.DataFrame(_o1, columns=["t", "o", "h", "l", "c", "v"])
                _d1 = _d1[_d1.t + 3600000 <= int(time.time() * 1000)]
                if len(_d1) >= strategy.MIN_BARS:
                    _rsi1h = float(strategy.features(_d1).iloc[-1]["rsi"])
            except Exception as _e1:
                log(f"RSI 1h check skip {sym}: {_e1}")
            if rsi1h_veto(side_str, _rsi1h):
                log(f"ORCHESTRATOR HARD VETO: {sym} {side_str} rejected - RSI 1h jenuh ({float(_rsi1h):.1f}, TF sinyal {_tf}) DILARANG {('BELI DI PUCUK' if side_str == 'LONG' else 'SELL DI DASAR')}")
                continue

            # HARD FILTER: DILARANG BELI DI PUCUK (RSI Overbought / Di Luar Bollinger Bands / Zone Filter)
            if side_str == "LONG":
                # 1. RSI batas ketat 58.0 (di atas 58 sudah zona panas)
                if _rsi >= 58.0:
                    log(f"ORCHESTRATOR HARD VETO: {sym} LONG rejected - RSI overextended ({_rsi:.1f} >= 58.0) DILARANG BELI DI PUCUK")
                    continue
                # 2. Bollinger Band ceiling: wajib minimal 1.5% di bawah BB_UP (dilarang mepet upper band)
                if _bbu and price >= float(_bbu) * (1.0 - BB_CLEAR_TOL):
                    log(f"ORCHESTRATOR HARD VETO: {sym} LONG rejected - Price too close to Upper Bollinger Band (${price} >= {float(_bbu)*(1.0 - BB_CLEAR_TOL):.4f}) DILARANG BELI DI PUCUK")
                    continue
                # 3. Bar close juga tidak boleh di atas BB_UP * (1.0 - BB_CLEAR_TOL)
                _bar_close = float(meta.get("close", 0.0))
                if _bbu and _bar_close >= float(_bbu) * (1.0 - BB_CLEAR_TOL):
                    log(f"ORCHESTRATOR HARD VETO: {sym} LONG rejected - Bar close too high near Upper Bollinger Band (${_bar_close} >= {float(_bbu)*(1.0 - BB_CLEAR_TOL):.4f}) DILARANG BELI DI PUCUK")
                    continue
            elif side_str == "SHORT":
                if _rsi <= 42.0:
                    log(f"ORCHESTRATOR HARD VETO: {sym} SHORT rejected - RSI oversold ({_rsi:.1f} <= 42.0) DILARANG SELL DI DASAR")
                    continue
                if _bbl and price <= float(_bbl) * (1.0 + BB_CLEAR_TOL):
                    log(f"ORCHESTRATOR HARD VETO: {sym} SHORT rejected - Price too close to Lower Bollinger Band (${price} <= {float(_bbl)*(1.0 + BB_CLEAR_TOL):.4f}) DILARANG SELL DI DASAR")
                    continue
                _bar_close = float(meta.get("close", 0.0))
                if _bbl and _bar_close <= float(_bbl) * (1.0 + BB_CLEAR_TOL):
                    log(f"ORCHESTRATOR HARD VETO: {sym} SHORT rejected - Bar close too low near Lower Bollinger Band (${_bar_close} <= {float(_bbl)*(1.0 + BB_CLEAR_TOL):.4f}) DILARANG SELL DI DASAR")
                    continue

            snap = {
                "symbol": sym, "current_price": price, "action": side_str,
                "trend": "UPTREND" if meta.get("trend_up") else "DOWNTREND",
                "trend_bias": "BULLISH" if meta.get("trend_up") else "BEARISH",
                "volume_ratio": round(meta["vol_ratio"], 2),
                "volume_condition": "high" if meta["vol_ratio"] >= 1.5 else "normal",
                "score": meta.get("score", 50),
                "breakout_margin_pct": round(meta.get("breakout_margin", 0.0) / price * 100, 3),
                "rsi_condition": _rsic,
                "confluence": {"count": _k, "checks": _chk},
                "bollinger_bands": {"condition": _bbcond,
                                    "upper": round(_bbu, 8) if _bbu else None,
                                    "lower": round(_bbl, 8) if _bbl else None,
                                    "middle": round(meta.get("bb_mid") or 0.0, 8)},
                "macd": {"status": "BULLISH" if _mh > 0 else "BEARISH",
                         "histogram": round(_mh, 8),
                         "momentum": "RISING" if _mh > _mp else "FALLING"},
                "indicators": {"rsi_14": round(_rsi, 1),
                               "atr_14": round(meta["atr"], 6),
                               "ema50": round(float(meta.get("ema50") or 0.0), 8),
                               "vwap": round(_vwap_val, 8),
                               "vwap_status": "BULLISH_ABOVE_VWAP" if price > _vwap_val else "BEARISH_BELOW_VWAP",
                               "smc_fvg": _smc_fvg,
                               "smc_liquidity_sweep": _smc_sweep,
                               "volume_vs_average": round(meta["vol_ratio"], 2)},
                "fees": {"taker_fee_rate": 0.0005, "maker_fee_rate": 0.0002},
                "quant_math_evaluation": {
                    "score": meta.get("score", 50),
                    "action": side_str,
                    "volatility_pct": round(meta["atr"] / price * 100, 3),
                    "recommended_stop_loss": round(_sl_floor, 8),
                    "recommended_grid_count": 1,
                    "profit_multiplier": round(strategy.TP2_R, 2),
                    "reasons": [
                        f"trend-following pullback retest near EMA50 / BB_MID with RSI {meta.get('rsi', 0.0):.1f}",
                        f"price safely cleared >=1.5% below Upper Bollinger Band (clearance: {meta.get('breakout_margin', 0.0):.4f})",
                        f"volume {meta['vol_ratio']:.2f}x the 20-bar mean (gate {strategy.MIN_VOL_RATIO})",
                        f"structural stop at {strategy.SL_ATR}xATR = {_sd:.6g}",
                    ]
                }
            }
            # Multi-analyst debate (Technical + Macro + Sentiment)
            _debate = MultiAnalystEngine(base_engine=AIAnalyzer()).run_debate(snap)
            ai_res = _debate
            ai_reason = _debate.primary_reason
            if _debate.dissenting_opinion:
                ai_reason += f" | Beda pendapat: {_debate.dissenting_opinion[:50]}"
            if _debate.decision_log_file:
                log(f"Debate log: {_debate.decision_log_file}")
            log(f"DEBATE {sym}: {ai_res.action} conf {ai_res.confidence:.2f} | {ai_reason}")

            if ai_res.confidence < MIN_AI_CONF:
                log(f"VETO: AI confidence {ai_res.confidence:.2f} < {MIN_AI_CONF} on {sym} | {ai_reason}")
                continue

            if ai_res.action == "HOLD":
                log(f"VETO: AI HOLD on {sym} | {ai_reason}")
                continue

            if ai_res.action in ("LONG", "SHORT") and ai_res.action != side_str:
                log(f"VETO: AI disagrees ({ai_res.action} vs {side_str}) on {sym} | {ai_reason}")
                continue

        except Exception as ae:
            log(f"VETO: AI unavailable ({ae}) - skip {sym}")
            continue

        # --- FILTER LEBAR SL (Kandidat #1 FREEZE.md) ---
        _sl_d = strategy.SL_ATR * meta["atr"]
        if not sl_width_ok(price, _sl_d):
            log(f"SL WIDTH REJECT {sym}: SL {sl_width_pct(price, _sl_d):.2f}% > {MAX_SL_PCT:g}% "
                f"(jarak {_sl_d:.6g} @ {price}) - checkpoint 29: SL>5% n=10 -1,32R")
            continue

        # --- EXECUTE MAKER ENTRY ---
        with contextlib.redirect_stdout(sys.stderr):
            t.exchange.set_leverage(MAX_LEVERAGE, resolved)
            order = maker_entry(t, resolved, action, qty, price, meta["atr"])
        if not order:
            log(f"Entry skipped on {sym}: maker unfilled")
            continue

        time.sleep(1)
        fill = float(order.get("average") or order.get("price") or price)
        try:
            with contextlib.redirect_stdout(sys.stderr):
                _lp = [p for p in t.exchange.fetch_positions() if p.get("symbol") == sym and float(p.get("contracts") or 0) != 0]
            if _lp:
                _lq = abs(float(_lp[0]["contracts"]))
                if _lq > 0 and abs(_lq - qty) > 1e-9:
                    qty = _lq
                _le = float(_lp[0].get("entryPrice") or 0)
                if _le > 0:
                    fill = _le
        except Exception as pe:
            log(f"live qty read failed {sym}: {pe}")

        sl, tp1, tp2, _ = strategy.levels(fill, meta["atr"], side)
        px = lambda x: float(t.exchange.price_to_precision(resolved, x))
        sl_price, tp1_price, tp2_price = px(sl), px(tp1), px(tp2)
        close_side = "sell" if side == "BUY" else "buy"

        stop_ok = False
        try:
            with contextlib.redirect_stdout(sys.stderr):
                stop_ok = bool(t.set_protective_stop(sym, sl_price, amount=qty, position_side=side_str))
        except Exception as se:
            log(f"STOP PLACEMENT ERROR {sym}: {se}")

        if not stop_ok:
            log(f"NO STOP on {sym} - emergency flatten")
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    t.close_position(sym)
            except Exception as ce:
                log(f"Emergency close failed {sym}: {ce}")
            try:
                TelegramNotifier().send_message(f"⚠️ <b>ENTRY DIBATALKAN</b> {sym} {side_str}\nStop gagal dipasang - posisi ditutup darurat.")
            except Exception:
                pass
            continue

        # === LADDER TP MULTI-LEVEL (adaptif, jumlah level menyesuaikan qty) ===
        # Level RAPAT berbasis R dari SL: 1.0R / 1.2R / 1.4R / 1.7R (hasil uji Monte Carlo:
        # ladder rapat +0.48R/trade vs ladder lebar +0.38R/trade di vol normal).
        # Sisanya runner -> dipegang trailing stop biar bisa ikut tren panjang.
        # Kalau qty terlalu kecil (step minimum exchange), level otomatis dikurangi
        # dan minimal 1 unit tetap disisakan sebagai runner.
        try:
            with contextlib.redirect_stdout(sys.stderr):
                _mk = t.exchange.market(resolved)
                _minq = float(((_mk.get("limits") or {}).get("amount") or {}).get("min") or 0)
                _praw = (_mk.get("precision") or {}).get("amount")
                if isinstance(_praw, (int, float)) and float(_praw) >= 1:
                    _step = float(_praw)
                else:
                    _step = 10 ** (-float(_praw if _praw is not None else 8))
                if _step <= 0:
                    _step = 1e-8

                _units = int(math.floor(float(qty) / _step + 1e-9))
                _r = abs(float(fill) - float(sl_price))
                _sign = 1.0 if side == "BUY" else -1.0

                _placed = []
                _failed = []
                _tried = 0
                _runner_units = _units
                if _units >= 2 and _r > 0:
                    _runner_units = 1 if _units <= 4 else max(1, int(round(_units * LADDER_RUNNER_PCT)))
                    _tp_units = _units - _runner_units
                    _lvls = list(LADDER_LEVELS)[:max(1, min(4, _tp_units))]
                    _w = list(LADDER_WEIGHTS)[:len(_lvls)]
                    _sw = sum(_w)
                    _w = [x / _sw for x in _w]
                    _alloc = [max(1, int(round(_tp_units * x))) for x in _w]
                    while sum(_alloc) > _tp_units and max(_alloc) > 1:
                        _alloc[_alloc.index(max(_alloc))] -= 1
                    while sum(_alloc) < _tp_units:
                        _alloc[_alloc.index(max(_alloc))] += 1

                    for _k, (_mult, _n) in enumerate(zip(_lvls, _alloc)):
                        if _n <= 0:
                            continue
                        _q = float(t.exchange.amount_to_precision(resolved, _n * _step))
                        if _q <= 0 or _q < _minq:
                            continue
                        _p = px(float(fill) + _sign * _r * _mult)
                        _tried += 1
                        # place_limit_order menelan error dan balik falsy; dulu tetap
                        # di-append -> log bilang LADDER PLACED padahal leg-nya bolong
                        if t.place_limit_order(sym, close_side, _q, _p, reduce_only=True):
                            _placed.append((_mult, _q, _p))
                        else:
                            _failed.append((_mult, _q, _p))
                            log(f"LADDER LEG FAILED {sym}: {_q}@{_p} ({_mult:g}R) - {getattr(t, 'last_error', '')}")

                # FIX v10: qty terlalu kecil untuk dipecah -> satu TP penuh di level 1R.
                # Tanpa ini ladder gagal total dan posisi hanya dijaga SL (bug ANTHROPIC 2026-11-08).
                if not _placed and _r > 0:
                    try:
                        _fq = float(t.exchange.amount_to_precision(resolved, float(qty)))
                    except Exception:
                        _fq = 0.0
                    if _fq > 0 and _fq >= _minq:
                        _m1 = float(LADDER_LEVELS[0])
                        _p1 = px(float(fill) + _sign * _r * _m1)
                        _tried = max(1, _tried)
                        if t.place_limit_order(sym, close_side, _fq, _p1, reduce_only=True):
                            _placed.append((_m1, _fq, _p1))
                            log(f"LADDER SINGLE {sym}: qty {_fq} @ {_p1} ({_m1:g}R) - qty {qty} tidak bisa dipecah, TP penuh")
                        else:
                            _failed.append((_m1, _fq, _p1))
                            log(f"LADDER SINGLE FAILED {sym}: {_fq}@{_p1} - {getattr(t, 'last_error', '')}")

                if _placed:
                    _txt = " | ".join(f"TP{i+1} {q}@{p} ({m:g}R)" for i, (m, q, p) in enumerate(_placed))
                    log(f"LADDER PLACED {sym}: {_txt} | Runner {round(_runner_units * _step, 8)}")
                    if len(_placed) < _tried:
                        log(f"LADDER PARTIAL {sym}: cuma {len(_placed)}/{_tried} leg terpasang - sisanya dijaga SL + trailing")
                else:
                    log(f"TP ladder SKIP {sym}: qty {qty} < step minimum {_minq} - dijaga SL + trailing")
        except Exception as te:
            log(f"TP placement error {sym}: {te}")

        risk_real = qty * sl_dist
        # Leg TP yang benar-benar terpasang (mult, qty, harga) - sumber re-arm.
        try:
            _legs_rec = [[round(float(m), 2), float(q), float(p)] for (m, q, p) in _placed]
        except Exception:
            _legs_rec = []
        try:
            _legs_fail_rec = [[round(float(m), 2), float(q), float(p)] for (m, q, p) in _failed]
        except Exception:
            _legs_fail_rec = []
        trade_record = {
            "symbol": sym, "side": side_str, "entry": fill, "qty": qty,
            "sl": sl_price, "tp1": tp1_price, "tp2": tp2_price,
            "tp_legs": _legs_rec, "tp_failed": _legs_fail_rec,
            "cluster": cluster, "ts": int(time.time()), "risk": round(risk_real, 4)
        }

        at_dict = st.get("active_trades") or {}
        at_dict[sym] = trade_record
        st["active_trades"] = at_dict
        st["active_trade"] = trade_record  # backward compatibility
        st["last_entry_ts"] = time.time()
        st["last_signal_ts"] = meta["bar_ts"]
        st["day_key"] = _today
        _prev += 1
        st["day_entries"] = _prev
        save_state(st)

        occupied_clusters.add(cluster)
        occupied_symbols.add(sym)
        entered_count += 1

        fee_rt = notional * 0.001
        try:
            new = not os.path.exists(JOURNAL)
            with open(JOURNAL, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["ts", "symbol", "side", "entry", "qty", "notional", "sl", "tp1",
                                "tp2", "sl_pct", "risk_usdt", "fee_rt", "atr", "vol_ratio", "reason"])
                w.writerow([int(time.time()), sym, side_str, fill, qty, round(notional, 2),
                            sl_price, tp1_price, tp2_price, round(sl_dist / price * 100, 2),
                            round(risk_real, 4), round(fee_rt, 4), round(meta["atr"], 6),
                            round(meta["vol_ratio"], 2), ai_reason])
        except Exception as je:
            log(f"Journal error: {je}")

        log(f"ENTRY {sym} {side_str} qty={qty} @ {fill} notional=${notional:.2f} SL={sl_price} TP1={tp1_price} TP2={tp2_price} risk=${risk_real:.2f} fee_rt=${fee_rt:.4f}")

        # MAKER_PROBE: ukur spread + kedalaman top-of-book tiap entry. Dasar
        # keputusan pindah ke limit/maker (fee sekarang 13% gross edge).
        # Read-only: tidak mengubah order apa pun.
        try:
            _ob = t.exchange.fetch_order_book(resolved, limit=5)
            _bb = float(_ob["bids"][0][0])
            _ba = float(_ob["asks"][0][0])
            _mid = (_bb + _ba) / 2.0
            _spr = ((_ba - _bb) / _mid * 100.0) if _mid else 0.0
            _dep = float(_ob["bids"][0][1]) * _bb + float(_ob["asks"][0][1]) * _ba
            _newp = not os.path.exists(MAKER_PROBE)
            with open(MAKER_PROBE, "a", newline="") as _pf:
                _pw = csv.writer(_pf)
                if _newp:
                    _pw.writerow(["ts", "symbol", "side", "bid", "ask", "spread_pct",
                                  "top_depth_usdt", "notional", "fee_rt"])
                _pw.writerow([int(time.time()), sym, side_str, _bb, _ba,
                              round(_spr, 5), round(_dep, 2), round(notional, 2), round(fee_rt, 4)])
            # ENTRY_FEAT: fitur pra-entry untuk uji variabel pasca-checkpoint.
            _fp = os.path.join(BASE_DIR, "entry_feat.csv")
            _newf = not os.path.exists(_fp)
            _hr = (time.gmtime().tm_hour + 7) % 24
            _ses = "ASIA" if 7 <= _hr < 14 else ("EU" if 14 <= _hr < 20 else "US")
            with open(_fp, "a", newline="") as _ff:
                _fw = csv.writer(_ff)
                if _newf:
                    _fw.writerow(["ts", "symbol", "side", "ai_conf", "vol_ratio", "rsi",
                                  "adx", "atr_pct", "sl_pct", "spread_pct", "depth_usdt",
                                  "score", "hour_wib", "session", "notional", "risk_usd"])
                _fw.writerow([int(time.time()), sym, side_str, round(float(ai_res.confidence), 3),
                              round(float(meta["vol_ratio"]), 3), round(float(meta.get("rsi") or 0.0), 1),
                              round(float(meta.get("adx") or 0.0), 1),
                              round(meta["atr"] / price * 100, 3),
                              round(sl_dist / price * 100, 3), round(_spr, 5), round(_dep, 2),
                              meta.get("score", 50), _hr, _ses,
                              round(notional, 2), round(risk_real, 3)])
            log(f"MAKER PROBE {sym}: spread {_spr:.3f}% top depth ${_dep:.0f} vs fee taker ${fee_rt:.4f}")
        except Exception as _mpe:
            log(f"MAKER PROBE skip {sym}: {err_detail(_mpe)}")

        try:
            TelegramNotifier().notify_trade_opened(sym, {
                "action": side_str, "entry_price": fill, "stop_loss": sl_price,
                "take_profit_1": (" | ".join(f"{p} ({m:g}R)" for m, q, p in _placed) if _placed else f"{tp1_price} / {tp2_price}"),
                "leverage": MAX_LEVERAGE,
                "required_margin_usdt": round(notional / MAX_LEVERAGE, 2),
                "risk_amount_usdt": round(risk_real, 2),
                "risk_pct": f"{sl_dist / price * 100:.2f}%",
            }, f"Compression breakout 4h | vol {meta['vol_ratio']:.2f}x | cluster: {cluster} | {ai_reason}", mode="live")
        except Exception as tge:
            log(f"Telegram notify error: {tge}")

def news_heads_up(t):
    """Info saja: ada posisi kebuka dan berita merah USD dekat -> kasih tau.

    Tidak menggeser SL, tidak menutup posisi. Perlakuan posisi saat berita sengaja
    DIBEKUKAN sampai ada >=5 sampel slippage nyata (lihat FREEZE.md Kandidat #3).
    Dipanggil dari run(), bukan dari scan_and_enter(), supaya tetap jalan walau
    entry sedang dimatikan atau slot penuh.
    """
    try:
        res = ForexFactoryNewsSentinel().check_news_guard()
        if not res.get("is_danger"):
            return
        ev = res.get("imminent_event") or {}
        title = str(ev.get("title") or "high-impact")
        pos = get_open_positions(t)
        if not pos:
            return
        st = load_state() or {}
        seen = st.get("news_alerted") or {}
        now = int(time.time())
        if now - int(seen.get(title) or 0) < 4 * 3600:
            return
        lines = []
        for p in pos:
            sym = p.get("symbol")
            meta = (st.get("active_trades") or {}).get(sym) or {}
            sl = float(meta.get("sl_live") or meta.get("sl") or 0)
            mark = float(p.get("mark_price") or p.get("markPrice") or 0)
            dist = abs(mark - sl) / mark * 100 if (mark and sl) else 0.0
            lines.append(f"\u2022 {sym} {p.get('side')} SL {sl:g} ({dist:.1f}% dari harga)")
        TelegramNotifier().send_message(
            "\U0001f4f0 <b>BERITA MERAH DEKAT</b>\n"
            f"{res.get('caution_reason', '')}\n\n"
            "Posisi kebuka (SL TIDAK digeser, posisi TIDAK ditutup):\n"
            + "\n".join(lines)
            + "\n\nEntry baru ditahan otomatis. Perlakuan posisi dibekukan sampai ada data slippage."
        )
        seen[title] = now
        st["news_alerted"] = seen
        save_state(st)
    except Exception as e:
        log(f"news_heads_up error: {e}")


def maker_shadow(t):
    """Uji bayangan: limit post-only di harga sentuh kena atau tidak.

    Read-only, nol order. Sampel diambil per siklus (2 menit), jadi kalau harga
    menyentuh lalu balik di antara siklus, dihitung tidak kena -> fill rate
    cenderung UNDER-estimate. Kalau hasilnya tetap tinggi, maker layak dipakai.
    """
    try:
        if not os.path.exists(MAKER_PROBE):
            return
        with open(MAKER_PROBE, newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return
    if not rows:
        return
    done = set()
    if os.path.exists(MAKER_SHADOW):
        try:
            with open(MAKER_SHADOW, newline="") as f:
                done = {r["ts"] + r["symbol"] for r in csv.DictReader(f)}
        except Exception:
            pass
    now = time.time()
    fresh = not os.path.exists(MAKER_SHADOW)
    out = []
    for r in rows[-40:]:
        if r["ts"] + r["symbol"] in done:
            continue
        try:
            age = now - float(r["ts"])
        except Exception:
            continue
        if age < 90:
            continue
        is_long = str(r["side"]).upper().startswith("L")
        try:
            with contextlib.redirect_stdout(sys.stderr):
                last = float(t.exchange.fetch_ticker(r["symbol"])["last"])
            touch = float(r["bid"]) if is_long else float(r["ask"])
        except Exception:
            continue
        hit = last <= touch if is_long else last >= touch
        if not hit and age < 600:
            continue
        out.append([r["ts"], r["symbol"], r["side"], touch, last, 1 if hit else 0, int(age)])
    if not out:
        return
    with open(MAKER_SHADOW, "a", newline="") as f:
        w = csv.writer(f)
        if fresh:
            w.writerow(["ts", "symbol", "side", "touch", "last", "filled", "waited_s"])
        w.writerows(out)
    hit_n = sum(1 for x in out if x[5] == 1)
    log(f"MAKER SHADOW: {hit_n}/{len(out)} limit sentuh kena (kumulatif {len(done) + len(out)})")


def run():
    with contextlib.redirect_stdout(sys.stderr):
        t = BinanceFuturesTrader(is_testnet=False)

    st = load_state()
    log_close_if_flat(t, st)
    sweep_orphan_orders(t)

    managed_syms = manage_open_positions(t)
    maker_shadow(t)
    news_heads_up(t)
    # Check open positions count vs limit
    open_pos = get_open_positions(t)
    if len(open_pos) < MAX_OPEN_POSITIONS:
        if os.getenv("ENTRY_ENABLED", "false").lower() in ("1", "true", "yes", "on"):
            scan_and_enter(t)
        else:
            log("ENTRY PAUSED (ENTRY_ENABLED=false) - manage-only mode")
    else:
        log(f"Max positions reached ({len(open_pos)}/{MAX_OPEN_POSITIONS}) - manage-only mode")

def main():
    try:
        run()
    except Exception as e:
        import traceback
        log(f"FATAL {type(e).__name__}: {e}")
        print(traceback.format_exc()[-800:], file=sys.stderr, flush=True)
        try:
            TelegramNotifier().send_message(
                f"❌ <b>BOT ERROR</b>\n<code>{type(e).__name__}: {str(e)[:200]}</code>\n"
                f"Run berikutnya otomatis coba lagi."
            )
        except Exception:
            pass
        sys.exit(1)

if __name__ == "__main__":
    main()
