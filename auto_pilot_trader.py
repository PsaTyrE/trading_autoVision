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
from src.live_trader import BinanceFuturesTrader
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

MAX_LEVERAGE = 5
MAX_MARGIN_PCT = 0.85
STATE_FILE = os.getenv("STATE_FILE", os.path.join(BASE_DIR, "autonomous_trader_state.json"))
JOURNAL = os.getenv("JOURNAL_FILE", os.path.join(BASE_DIR, "trade_log.csv"))
CLOSED = os.getenv("CLOSED_FILE", os.path.join(BASE_DIR, "trade_closed.csv"))
DAILY_CAP_SEC = int(os.getenv("DAILY_CAP_SEC", "86400"))
DAILY_MAX = int(os.getenv("MAX_DAILY_ENTRIES", "3"))
RISK_USDT = float(os.getenv("RISK_USDT", "0.85"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
MIN_NOTIONAL = 5.0
RISK_MAX = float(os.getenv("RISK_MAX_USDT", str(RISK_USDT * 1.5)))
MIN_AI_CONF = float(os.getenv("MIN_AI_CONF", "0.85"))

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

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"active_trades": {}, "active_trade": None, "last_scan": 0}

def save_state(s):
    try:
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

    for at in list(ats):
        sym = at.get("symbol")
        if not sym or sym in live_symbols:
            continue

        raw = str(sym).split(":")[0].replace("/", "")
        pnl = fee = 0.0
        n_tr = 0
        try:
            with contextlib.redirect_stdout(sys.stderr):
                trades = t.exchange.fapiPrivateGetUserTrades({"symbol": raw, "limit": 100})
                for x in trades:
                    if int(x["time"]) >= (int(at.get("ts", 0)) - 600) * 1000:
                        pnl += float(x["realizedPnl"])
                        fee += float(x["commission"])
                        n_tr += 1
        except Exception as e:
            log(f"close-log fetch error {sym}: {e}")

        if n_tr == 0:
            log(f"close-log: no fills for {raw} since entry - retry next run")
            continue

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

        try:
            with contextlib.redirect_stdout(sys.stderr):
                t.cancel_all_orders(sym)
        except Exception as e:
            log(f"close-log cancel error {sym}: {e}")

        if sym in at_dict:
            del at_dict[sym]
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

def manage_open_positions(t):
    """Monitor ALL live positions, update trailing stop / break-even independently."""
    positions = get_open_positions(t)
    if not positions:
        return []

    st = load_state()
    at_dict = st.get("active_trades") or {}
    legacy_at = st.get("active_trade")

    managed_syms = []
    for pos in positions:
        symbol = pos["symbol"]
        qty = abs(float(pos["contracts"]))
        entry = float(pos["entryPrice"])
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
        _sl0 = float(trade_meta.get("sl") or 0.0)
        r_dist = abs(entry - _sl0) if _sl0 else 0.0
        gain = (cur_price - entry) if side == "LONG" else (entry - cur_price)

        lock_sl = None
        if r_dist > 0:
            if gain >= r_dist * 2.0:
                # Lock at least +1.25R profit when hitting +2.0R
                lock_sl = round(entry + r_dist * 1.25, dec) if side == "LONG" else round(entry - r_dist * 1.25, dec)
            elif gain >= r_dist * 1.2:
                # Lock +0.5R profit once gain crosses +1.2R
                lock_sl = round(entry + r_dist * 0.5, dec) if side == "LONG" else round(entry - r_dist * 0.5, dec)
            elif gain >= r_dist * 0.8:
                # Protect capital: Move to Break-Even (entry) once gain reaches +0.8R
                lock_sl = round(entry * 1.001, dec) if side == "LONG" else round(entry * 0.999, dec)

        log(f"HB {symbol} {side} qty {qty} px {cur_price} pnl {pnl_pct*100:+.2f}% gainR {gain/r_dist if r_dist else 0:.2f} sl {cur_sl or 0}")

        # TP re-arm check
        try:
            with contextlib.redirect_stdout(sys.stderr):
                _oo = t.exchange.fetch_open_orders(symbol)
        except Exception:
            _oo = None

        if _oo is not None and trade_meta.get("tp1") and trade_meta.get("tp2"):
            _cs = "sell" if side == "LONG" else "buy"
            if not [o for o in _oo if str(o.get("side") or "").lower() == _cs]:
                if abs(qty - float(trade_meta.get("qty") or 0)) < 1e-9:
                    try:
                        _q1 = float(t.exchange.amount_to_precision(symbol, qty * 0.60))
                        _q2 = float(t.exchange.amount_to_precision(symbol, qty * 0.25))
                        with contextlib.redirect_stdout(sys.stderr):
                            if _q1 > 0:
                                t.place_limit_order(symbol, _cs, _q1, float(trade_meta["tp1"]), reduce_only=True)
                            if _q2 > 0:
                                t.place_limit_order(symbol, _cs, _q2, float(trade_meta["tp2"]), reduce_only=True)
                        log(f"TP RE-ARM {symbol}: {_q1} (60%) @ {trade_meta['tp1']} | {_q2} (25%) @ {trade_meta['tp2']} | runner 15% trailing")
                    except Exception as te:
                        log(f"TP re-arm failed {symbol}: {te}")

        if lock_sl:
            eps = max(abs(cur_price) * 1e-4, 10.0 ** -dec)
            should_update = (side == "LONG" and lock_sl > cur_sl + eps) or \
                            (side == "SHORT" and (cur_sl == 0 or lock_sl < cur_sl - eps))
            if should_update:
                log(f"Trailing: Locking profit on {symbol} -> Moving SL to {lock_sl}")
                with contextlib.redirect_stdout(sys.stderr):
                    t.set_protective_stop(symbol, lock_sl, amount=qty, position_side=side)
                try:
                    msg = f"🔒 <b>[PROFIT LOCKED / TRAILING SL]</b>\nSymbol: <b>{symbol}</b> ({side})\nPrice: <b>${cur_price}</b> | SL Rapat: <b>${lock_sl}</b>\nStatus: Profit terkunci, sisa posisi anti-minus!"
                    TelegramNotifier().send_message(msg)
                except Exception as e:
                    log(f"Telegram notify error: {e}")

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
        breaker_threshold = float(os.getenv("CIRCUIT_BREAKER_MAX_LOSS", "5.00"))
        if equity < base - breaker_threshold:
            log(f"CIRCUIT BREAKER: equity ${equity:.2f} < ${base:.2f}-{breaker_threshold:.2f}. Entries halted.")
            return
    except Exception as e:
        log(f"Breaker error: {e}")

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
        log(f"Daily cap reached ({_prev}/{DAILY_MAX} UTC today). Waiting.")
        return

    # --- SCAN CLOSED 4h BARS ---
    def _tradeable(sym, meta):
        sd = strategy.SL_ATR * meta["atr"]
        if sd <= 0:
            return False
        _live_risk = min(RISK_USDT, round(equity * 0.02, 2))
        raw_q = _live_risk / sd
        m = t.exchange.market(sym)
        _lim = m.get("limits") or {}
        min_q = float((_lim.get("amount") or {}).get("min") or 0)
        min_c = float((_lim.get("cost") or {}).get("min") or 0)
        floor_notional = max(MIN_NOTIONAL, min_c)
        if raw_q * meta["close"] < floor_notional:
            raw_q = max(raw_q, floor_notional / meta["close"])
            if raw_q * sd > RISK_MAX:
                return False
        if raw_q < min_q:
            return False
        return True

    candidates = []
    for sym in WATCHLIST:
        if sym in occupied_symbols:
            continue
        try:
            with contextlib.redirect_stdout(sys.stderr):
                raw = t.exchange.fetch_ohlcv(sym, timeframe=strategy.TF, limit=320)
            if len(raw) < strategy.MIN_BARS:
                continue
            d = strategy.features(pd.DataFrame(raw, columns=["t", "o", "h", "l", "c", "v"]))
            sig = strategy.signal(d, -2)
            if not sig:
                continue
            side, meta = sig
            if st.get("last_signal_ts") == meta["bar_ts"]:
                continue
            if not _tradeable(sym, meta):
                log(f"Skip {sym}: qty/notional below exchange minimum")
                continue
            candidates.append((sym, side, meta, meta["vol_ratio"]))
        except Exception as e:
            log(f"scan {sym}: {type(e).__name__}: {str(e)[:120]}")
            continue

    if not candidates:
        log("No compression-breakout signal on last closed 4h bar.")
        return

    # Rank candidates by volume surge
    candidates.sort(key=lambda x: x[3], reverse=True)

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
            dyn_risk = min(RISK_USDT, round(equity * 0.02, 2))
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
            if bumped <= 0 or bumped_risk > RISK_MAX:
                log(f"Skip {sym}: notional ${notional:.2f} < ${MIN_NOTIONAL}, bump risk ${bumped_risk:.2f} > ${RISK_MAX:.2f}")
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

            # HARD FILTER: DILARANG BELI DI PUCUK (RSI Overbought / Di Luar Bollinger Bands)
            if side_str == "LONG":
                if _rsi >= 66.0:
                    log(f"ORCHESTRATOR HARD VETO: {sym} LONG rejected - RSI overbought ({_rsi:.1f} >= 66.0) DILARANG BELI DI PUCUK")
                    continue
                if _bbu and price >= float(_bbu) * 0.998:
                    log(f"ORCHESTRATOR HARD VETO: {sym} LONG rejected - Price near/above Upper Bollinger Band (${price} >= ${_bbu}) DILARANG BELI DI PUCUK")
                    continue
            elif side_str == "SHORT":
                if _rsi <= 34.0:
                    log(f"ORCHESTRATOR HARD VETO: {sym} SHORT rejected - RSI oversold ({_rsi:.1f} <= 34.0) DILARANG SELL DI DASAR")
                    continue
                if _bbl and price <= float(_bbl) * 1.002:
                    log(f"ORCHESTRATOR HARD VETO: {sym} SHORT rejected - Price near/below Lower Bollinger Band (${price} <= ${_bbl}) DILARANG SELL DI DASAR")
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
                        f"compression bbw<={strategy.COMPRESSION_Q} on previous bar, expansion now",
                        f"close broke {strategy.BREAK_LOOKBACK}-bar extreme by {meta.get('breakout_margin', 0.0):.6g}",
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
                ai_reason += f" | 异议: {_debate.dissenting_opinion[:50]}"
            if _debate.decision_log_file:
                log(f"Debate log: {_debate.decision_log_file}")
            log(f"DEBATE {sym}: {ai_res.action} conf {ai_res.confidence:.2f} | {ai_reason}")

            if ai_res.confidence < MIN_AI_CONF:
                log(f"VETO: AI confidence {ai_res.confidence:.2f} < {MIN_AI_CONF} on {sym}")
                try:
                    TelegramNotifier().send_message(f"[AI DEBATE VETO: LOW CONF] {sym}\nSetup: {side_str} | Conf: {ai_res.confidence:.2f} < {MIN_AI_CONF}\nDebat: {ai_reason}")
                except Exception:
                    pass
                continue

            if ai_res.action == "HOLD":
                log(f"VETO: AI HOLD, no confirmation on {sym} | {ai_reason}")
                try:
                    TelegramNotifier().send_message(f"[AI DEBATE VETO: HOLD] {sym}\nBear/Risk Officer memenangkan debat.\nAlasan: {ai_reason}")
                except Exception:
                    pass
                continue

            if ai_res.action in ("LONG", "SHORT") and ai_res.action != side_str:
                log(f"VETO: AI disagrees ({ai_res.action} vs {side_str})")
                try:
                    TelegramNotifier().send_message(f"[AI DEBATE VETO: CONFLICT] {sym}\nTeknikal: {side_str} vs AI: {ai_res.action}\nDetail: {ai_reason}")
                except Exception:
                    pass
                continue

        except Exception as ae:
            log(f"VETO: AI unavailable ({ae}) - skip {sym}")
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

        # 60% TP1, 25% TP2, 15% Runner (Frontloaded profit taking)
        try:
            with contextlib.redirect_stdout(sys.stderr):
                tp1_qty = float(t.exchange.amount_to_precision(resolved, qty * 0.60))
                tp2_qty = float(t.exchange.amount_to_precision(resolved, qty * 0.25))
                if tp1_qty > 0:
                    t.place_limit_order(sym, close_side, tp1_qty, tp1_price, reduce_only=True)
                if tp2_qty > 0:
                    t.place_limit_order(sym, close_side, tp2_qty, tp2_price, reduce_only=True)
                log(f"LADDER PLACED {sym}: TP1 {tp1_qty} @ {tp1_price} (60%) | TP2 {tp2_qty} @ {tp2_price} (25%) | Runner {round(qty - tp1_qty - tp2_qty, 4)} (15%)")
        except Exception as te:
            log(f"TP placement error {sym}: {te}")

        risk_real = qty * sl_dist
        trade_record = {
            "symbol": sym, "side": side_str, "entry": fill, "qty": qty,
            "sl": sl_price, "tp1": tp1_price, "tp2": tp2_price,
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

        try:
            TelegramNotifier().notify_trade_opened(sym, {
                "action": side_str, "entry_price": fill, "stop_loss": sl_price,
                "take_profit_1": f"{tp1_price} (40%) & {tp2_price} (40%)",
                "leverage": MAX_LEVERAGE,
                "required_margin_usdt": round(notional / MAX_LEVERAGE, 2),
                "risk_amount_usdt": round(risk_real, 2),
                "risk_pct": f"{sl_dist / price * 100:.2f}%",
            }, f"Compression breakout 4h | vol {meta['vol_ratio']:.2f}x | cluster: {cluster} | {ai_reason}", mode="live")
        except Exception as tge:
            log(f"Telegram notify error: {tge}")

def run():
    with contextlib.redirect_stdout(sys.stderr):
        t = BinanceFuturesTrader(is_testnet=False)

    st = load_state()
    log_close_if_flat(t, st)
    sweep_orphan_orders(t)

    managed_syms = manage_open_positions(t)
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
