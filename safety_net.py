#!/usr/bin/env python3
"""Safety net: detect naked positions and auto-arm an ATR-based protective stop.

Reads BOTH live and demo accounts. For every open position without a stop order,
places a STOP_MARKET reduceOnly stop via the audited set_protective_stop() path,
priced ~2 ATR(15m) below the recent swing low for LONGs (mirrored for SHORTs),
clamped to stay above liquidation with headroom. Prints a machine-readable
JSON summary (stdout) and exits 0 if all positions protected, 1 if any action
was taken, 2 on read error.
"""
import json
import os
import statistics
import sys

def main():
    import contextlib
    from tg_notify import send as _tg_send
    from src.live_trader import BinanceFuturesTrader
    # DOTENV_EXPLICIT: cron cwd=/home/pi, so MANUAL_SYMBOLS was silently empty.
    from dotenv import load_dotenv
    base_dir = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(base_dir, ".env"), override=True)
    results = {"accounts": {}, "actions": []}
    exit_code = 0
    # LIVE_ONLY: the bot never trades demo; scanning it only burned API calls
    # and could spam stops onto stale demo positions.
    for is_demo in (False,):
        label = "demo" if is_demo else "live"
        try:
            # Redirect ALL prints (constructor + live_trader internals) to stderr so
            # stdout stays pure JSON for the status file.
            with contextlib.redirect_stdout(sys.stderr):
                t = BinanceFuturesTrader(is_testnet=is_demo)
                # MANUAL_SYMBOLS (2026-10-15): operator-managed positions keep their
                # own stops; the safety net must not touch them.
                _manual = {s.strip().upper() for s in os.getenv("MANUAL_SYMBOLS", "").split(",") if s.strip()}
                # SAFETY_NET_FIX: get_open_positions() returns [] on this account
                # (silent blind spot -> net never armed). fetch_positions is authoritative.
                positions = [p for p in t.exchange.fetch_positions()
                             if float(p.get("contracts") or 0) != 0
                             and str(p.get("symbol", "")).split(":")[0].upper() not in _manual]
            protected, naked = [], []
            for p in positions:
                sym = p.get("symbol")
                side = str(p.get("side") or "").upper()
                conds = []
                try:
                    conds = t.get_conditional_orders(sym)
                except Exception:
                    conds = []
                has_stop = any("STOP" in str(o.get("orderType") or o.get("type") or "").upper() for o in conds)
                rec = {"symbol": sym, "side": side, "protected": has_stop}
                if has_stop:
                    protected.append(rec)
                    continue
                naked.append(rec)
                # price an ATR-based stop
                try:
                    ohlcv = t.exchange.fetch_ohlcv(sym, timeframe="15m", limit=20)
                    trs = []
                    for i in range(1, len(ohlcv)):
                        h, l, c = ohlcv[i][2], ohlcv[i][3], ohlcv[i - 1][4]
                        trs.append(max(h - l, abs(h - c), abs(l - c)))
                    atr = statistics.mean(trs) if trs else 0.0
                    pos = t.exchange.fetch_positions([sym])
                    info = (pos[0].get("info") if pos else {}) or {}
                    mark = float(info.get("markPrice") or 0)
                    liq = float(info.get("liquidationPrice") or 0)
                    if side == "LONG":
                        swing = min(x[3] for x in ohlcv[-6:]) if ohlcv else mark
                        stop = round(min(mark - 2.0 * atr, swing - 0.5 * atr), 4)
                        # never below liquidation + 1.5% headroom
                        floor = liq * 1.015 if liq else stop
                        stop = max(stop, floor)
                        if stop >= mark:
                            stop = round(mark * 0.98, 4)
                    else:
                        swing = max(x[2] for x in ohlcv[-6:]) if ohlcv else mark
                        stop = round(max(mark + 2.0 * atr, swing + 0.5 * atr), 4)
                        ceil = liq * 0.985 if liq else stop
                        stop = min(stop, ceil)
                        if stop <= mark:
                            stop = round(mark * 1.02, 4)
                    # TICK_FIX: round(...,4) ignored tick size and got rejected.
                    stop = float(t.exchange.price_to_precision(sym, stop))
                    # QTY_PASS: amount=None resolved qty through get_open_positions(),
                    # which returns [] here -> the net could never arm a stop.
                    ok = t.set_protective_stop(sym, stop_price=stop,
                                               amount=abs(float(p.get("contracts") or 0)),
                                               position_side=side)
                    results["actions"].append({"account": label, "symbol": sym, "side": side,
                                               "stop": stop, "placed": bool(ok)})
                    if ok:
                        try:
                            _tg_send("🛡️ <b>SAFETY NET</b>\nStop pelindung dipasang otomatis!\n"
                                     "%s (%s) @ $%s\nAkun: %s\nPosisi sebelumnya TANPA stop." % (sym, side, stop, label))
                        except Exception:
                            pass
                    rec["stop_placed"] = stop
                    rec["placed_ok"] = bool(ok)
                    exit_code = 1
                except Exception as e:
                    rec["error"] = str(e)[:200]
                    results["actions"].append({"account": label, "symbol": sym, "error": str(e)[:200]})
                    exit_code = 1
            results["accounts"][label] = {"protected": protected, "naked": naked}
        except Exception as e:
            results["accounts"][label] = {"error": str(e)[:300]}
            exit_code = 2
    # Every print() inside live_trader (fetch errors, fingerprints) is captured to
    # stderr; only the final JSON reaches stdout so the status file stays valid JSON.
    print(json.dumps(results), file=sys.__stdout__)
    sys.exit(exit_code)

if __name__ == "__main__":
    main()
