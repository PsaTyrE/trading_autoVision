"""External stop / position watchdog.

The bot's own self-heal (``web_manager._run_loop``, every ~100s) only runs while
the bot process is ALIVE. If the bot crashes, hangs, or the Pi reboots while a
position is open, the exchange STOP_MARKET order is the only net left — and
nothing independently re-checks that it is still there.

This watchdog is the out-of-process safety net: it reads open positions and
conditional (algo) orders STRAIGHT from Binance and reports any position that is
NAKED (open with no STOP order protecting it).

It DETECTS and ALERTS; it does NOT auto-place stops. Re-placing a stop without
the bot's intended stop price would be guessing the risk — a wrong guess is worse
than a loud alert. It runs whether or not the bot process is alive.

Account selection: it checks BOTH the live and the demo account by default. The
bot's runtime mode can differ from ``config.trading_mode`` (a live session is a
runtime override, and the .env default may be stale), so trusting the config
alone once made this watchdog read the wrong account. Positions only exist in one
account, so sweeping both never misses a naked one — and the live account (real
money) is always covered, even when the bot is down.

Correctness note: an algo-fetch failure is reported as UNKNOWN, never as naked.
``get_conditional_orders`` raises a flag (``conditional_orders_ok=False``) on
fetch failure; treating that empty list as "no stops" would be a false alarm that
could trigger a wrong manual intervention.

Run:
    python -m src.stop_watchdog                 # both accounts, human summary
    python -m src.stop_watchdog --mode live     # live only
    python -m src.stop_watchdog --json          # machine-readable
    python -m src.stop_watchdog --interval 120  # continuous (for systemd)
Exit: 0 protected | 2 naked position(s) | 3 exchange/read error
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List, Optional

_TRADER_CACHE: Dict[str, Any] = {}


def _is_stop(order: Dict[str, Any]) -> bool:
    kind = (order.get("orderType") or order.get("type") or "").upper()
    return "STOP" in kind


def _with_nonce_retry(fn, trader):
    """TIMESYNC_RETRY: jalankan fn(), dan kalau gagal -1021 resync jam lalu
    coba sekali lagi. Pi tidak punya RTC; setelah NTP men-step jam, offset
    ccxt yang di-cache basi dan SEMUA request bertanda tangan ditolak -1021
    sampai di-resync. Tanpa ini watchdog melapor ERROR palsu (2026-11-09 21:23)."""
    try:
        return fn()
    except Exception as e:
        if "-1021" not in str(e):
            raise
        try:
            trader.exchange.load_time_difference()
        except Exception as _e:  # noqa: BLE001 - resync gagal bukan alasan mati
            print(f"[watchdog] clock resync failed: {_e}", flush=True)
        return fn()


def _trader_for(mode: str):
    """Build (and cache) a trader bound to the live or demo account."""
    from src.live_trader import BinanceFuturesTrader
    if mode not in _TRADER_CACHE:
        _TRADER_CACHE[mode] = BinanceFuturesTrader(is_testnet=(mode != "live"))
    return _TRADER_CACHE[mode]


def check(trader=None, is_testnet: Optional[bool] = None) -> Dict[str, Any]:
    """Read positions + conditional orders for ONE account; return a verdict.

    ``trader`` may be injected (tests) — any object exposing
    ``get_open_positions()``, ``get_conditional_orders()`` and the
    ``conditional_orders_ok`` flag works.
    """
    if trader is None:
        from src.live_trader import BinanceFuturesTrader
        if is_testnet is None:
            from src.config import config
            is_testnet = config.trading_mode != "live"
        trader = BinanceFuturesTrader(is_testnet=is_testnet)

    result: Dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": "demo" if getattr(trader, "is_testnet", True) else "live",
        "positions": [],
        "naked": [],
        "unknown": [],
        "errors": [],
        "ok": True,
    }

    try:
        positions = _with_nonce_retry(trader.get_open_positions, trader)
    except Exception as e:  # cannot even see the book -> do not certify
        result["errors"].append(f"positions fetch failed: {e}")
        result["ok"] = False
        return result

    # ALGO_PER_SYMBOL: /fapi/v1/allAlgoOrders REQUIRES a symbol and answers
    # -1102 without one, so sweep one symbol per open position. No positions ->
    # nothing to protect, and no fetch that could fail. The flag still
    # distinguishes "no stops" (empty list) from "the fetch broke".
    cond: List[Dict[str, Any]] = []
    cond_ok = True
    for _pos in positions:
        _sym = _pos.get("symbol")
        try:
            cond.extend(_with_nonce_retry(lambda s=_sym: trader.get_conditional_orders(s), trader) or [])
        except Exception as e:
            cond_ok = False
            result["errors"].append(f"conditional-orders fetch failed ({_sym}): {e}")
        if not bool(getattr(trader, "conditional_orders_ok", True)):
            cond_ok = False

    stops_by_sym: Dict[str, List[Dict[str, Any]]] = {}
    for o in cond:
        if _is_stop(o):
            stops_by_sym.setdefault((o.get("symbol") or "").upper(), []).append(o)

    for p in positions:
        sym = p["symbol"]
        raw = sym.replace("/", "").upper()
        stops = stops_by_sym.get(raw, []) or stops_by_sym.get(sym.upper(), [])
        row = {
            "symbol": sym,
            "side": p.get("side"),
            "contracts": p.get("contracts"),
            "entry_price": p.get("entry_price"),
            "mark_price": p.get("mark_price"),
            "unrealized_pnl": p.get("unrealized_pnl"),
            "stop_orders": len(stops),
            "stop_prices": [s.get("triggerPrice") or s.get("stopPrice") for s in stops],
            "protected": None,
        }
        if not cond_ok:
            result["unknown"].append(sym)          # cannot certify — NOT naked
        elif stops:
            row["protected"] = True
        else:
            # Jendela cancel->place saat trail bisa bikin daftar stop kosong
            # sesaat. Baca ulang sekali setelah jeda; baru tuduh NAKED kalau
            # tetap kosong. Stop asli tetap tertangkap, alarm palsu hilang.
            time.sleep(3)
            try:
                again = _with_nonce_retry(lambda s=sym: trader.get_conditional_orders(s), trader) or []
                again = [o for o in again if _is_stop(o)]
            except Exception as e:
                again = None
                result["errors"].append(f"re-check failed ({sym}): {e}")
            if again:
                row["protected"] = True
                row["stop_orders"] = len(again)
                row["stop_prices"] = [x.get("triggerPrice") or x.get("stopPrice") for x in again]
            elif again == []:
                row["protected"] = False
                result["naked"].append(sym)
            else:
                row["protected"] = None
                result["unknown"].append(sym)
        result["positions"].append(row)

    if result["errors"] or result["naked"] or result["unknown"]:
        result["ok"] = False
    return result


def check_all(modes: tuple = ("live", "demo")) -> Dict[str, Any]:
    """Sweep several accounts and flatten the verdicts into one result."""
    out: Dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "accounts": {},
        "naked": [],
        "unknown": [],
        "errors": [],
        "ok": True,
    }
    for m in modes:
        r = check(trader=_trader_for(m))
        out["accounts"][m] = r
        out["naked"] += [f"{m}:{s}" for s in r["naked"]]
        out["unknown"] += [f"{m}:{s}" for s in r["unknown"]]
        out["errors"] += [f"{m}: {e}" for e in r["errors"]]
    out["ok"] = not (out["naked"] or out["unknown"] or out["errors"])
    return out


def _fmt_pos(p: Dict[str, Any]) -> str:
    tag = {True: "OK", False: "NAKED", None: "UNKNOWN"}[p["protected"]]
    sp = (" @ " + ", ".join(str(x) for x in p["stop_prices"])) if p["stop_prices"] else ""
    pnl = p["unrealized_pnl"]
    pnl_s = f"{pnl:+.4f}" if isinstance(pnl, (int, float)) else str(pnl)
    return (f"{p['symbol']} {p['side']} {p['contracts']} | entry {p['entry_price']} "
            f"mark {p['mark_price']} | uPnL {pnl_s} | stops={p['stop_orders']}{sp}  [{tag}]")


def summary(result: Dict[str, Any]) -> str:
    """Render a single-account result (from ``check``)."""
    lines = [f"STOP WATCHDOG  {result['ts']}  mode={result['mode']}"]
    for p in result["positions"]:
        lines.append("  " + _fmt_pos(p))
    if not result["positions"]:
        lines.append("  (no open positions)")
    for e in result["errors"]:
        lines.append(f"  ! {e}")
    if result["ok"]:
        lines.append(f"VERDICT: PROTECTED ({len(result['positions'])} position(s))")
    elif result["naked"]:
        lines.append(f"VERDICT: NAKED -> {', '.join(result['naked'])} (no STOP on exchange!)")
    elif result["unknown"]:
        lines.append(f"VERDICT: UNVERIFIED -> {', '.join(result['unknown'])} (algo fetch failed)")
    else:
        lines.append("VERDICT: ERROR (could not read exchange state)")
    return "\n".join(lines)


def summary_all(result: Dict[str, Any]) -> str:
    """Render a multi-account result (from ``check_all``)."""
    lines = [f"STOP WATCHDOG  {result['ts']}  accounts={','.join(result['accounts'])}"]
    for mode, r in result["accounts"].items():
        lines.append(f"  [{mode.upper()}]")
        for p in r["positions"]:
            lines.append("    " + _fmt_pos(p))
        if not r["positions"]:
            lines.append("    (no open positions)")
        for e in r["errors"]:
            lines.append(f"    ! {e}")
    if result["ok"]:
        n = sum(len(r["positions"]) for r in result["accounts"].values())
        lines.append(f"VERDICT: PROTECTED ({n} position(s) across {len(result['accounts'])} account(s))")
    elif result["naked"]:
        lines.append(f"VERDICT: NAKED -> {', '.join(result['naked'])} (no STOP on exchange!)")
    elif result["unknown"]:
        lines.append(f"VERDICT: UNVERIFIED -> {', '.join(result['unknown'])} (algo fetch failed)")
    else:
        lines.append("VERDICT: ERROR (could not read exchange state)")
    return "\n".join(lines)


def alert(result: Dict[str, Any]) -> None:
    """Send a Telegram alert when the verdict is not fully protected (best-effort)."""
    if result["ok"]:
        return
    try:
        from src.telegram_notifier import TelegramNotifier
        notifier = TelegramNotifier()
    except Exception:
        return
    if not getattr(notifier, "enabled", False):
        return
    lines = ["⚠️ <b>STOP WATCHDOG</b>"]
    for s in result["naked"]:
        lines.append(f"🔴 <b>NAKED</b>: {s} — open with NO STOP order on the exchange!")
    for s in result["unknown"]:
        lines.append(f"❓ {s}: could not verify stops (algo fetch failed)")
    for e in result["errors"]:
        lines.append(f"• {e}")
    notifier.send_message("\n".join(lines))


def _resync_clocks() -> None:
    """CLOCK_RESYNC: re-measure the exchange clock offset for cached traders.

    ``load_time_difference()`` runs once at trader construction. This service is
    long-lived and starts at boot, before NTP has stepped the Pi clock, so the
    stored offset is stale for the whole process lifetime and every signed
    request fails -1021. One /time call per account per cycle is cheap.
    """
    for trader in list(_TRADER_CACHE.values()):
        try:
            trader.exchange.load_time_difference()
        except Exception as e:  # noqa: BLE001 - a resync must never kill the loop
            print(f"[watchdog] clock resync failed: {e}", flush=True)


def _render(result: Dict[str, Any], as_json: bool) -> None:
    print(json.dumps(result, indent=2, ensure_ascii=False) if as_json else summary_all(result), flush=True)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="External stop/position watchdog")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-alert", action="store_true", help="suppress the Telegram alert")
    ap.add_argument("--mode", choices=["both", "live", "demo"], default="both",
                    help="which account(s) to sweep (default: both)")
    ap.add_argument("--interval", type=int, default=0,
                    help="run continuously, re-checking every N seconds (0 = one-shot)")
    args = ap.parse_args(argv)

    modes = ("live", "demo") if args.mode == "both" else (args.mode,)

    if args.interval and args.interval > 0:
        # Long-running watchdog: a single bad run must never kill the loop, so
        # every cycle is isolated in try/except and the sleep always happens.
        print(f"[watchdog] starting, interval={args.interval}s, modes={modes}", flush=True)
        while True:
            try:
                _resync_clocks()  # CLOCK_RESYNC
                r = check_all(modes)
                if not args.no_alert:
                    alert(r)
                _render(r, args.json)
            except Exception as e:  # noqa: BLE001 — a watchdog never dies
                print(f"[watchdog] run failed: {e}", flush=True)
            time.sleep(args.interval)
        return 0  # unreachable

    result = check_all(modes)
    if not args.no_alert:
        alert(result)
    _render(result, args.json)

    if result["errors"]:
        return 3
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
