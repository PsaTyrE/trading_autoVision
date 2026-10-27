import os
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

from src.web_manager import bot_manager
from src.live_trader import BinanceFuturesTrader
from src.market_scanner import MarketScanner
from src.config import config

app = FastAPI(title="Binance Futures AI Trading Bot Dashboard")

# Models
class StartBotRequest(BaseModel):
    symbol: Optional[str] = None
    mode: Optional[str] = None  # None = automatically read from .env
    investment: float = 0.0
    grid_count: int = 8
    auto_pick: bool = True
    stop_loss: Optional[float] = None
    basket_size: int = 3
    leverage: Optional[int] = None
    quote_asset: Optional[str] = "USDT"

class SwitchModeRequest(BaseModel):
    mode: str

# Reusable trader singletons to prevent repetitive exchange initialization & market loading lag
_trader_instances = {}

def get_cached_trader(mode: str = "demo") -> BinanceFuturesTrader:
    is_testnet = (mode != "live")
    # If the active running bot already has an initialized trader for this mode, reuse it directly
    if bot_manager.trader and getattr(bot_manager.trader, 'is_testnet', None) == is_testnet:
        return bot_manager.trader
    key = "demo" if is_testnet else "live"
    if key not in _trader_instances:
        _trader_instances[key] = BinanceFuturesTrader(is_testnet=is_testnet)
    return _trader_instances[key]

@app.on_event("startup")
async def startup_event():
    # Pass event loop to bot manager for async WebSocket broadcasting
    loop = asyncio.get_running_loop()
    bot_manager.set_event_loop(loop)
    
    # Auto-start the Bot in background thread: dynamically scan & pick Top 3 Bullish Basket!
    # Gate via AUTO_START_BOT so a live deployment can boot the dashboard with the bot
    # OFF and require an explicit operator press (no surprise trading on restart).
    from src.config import config
    if os.getenv("AUTO_START_BOT", "true").strip().lower() in ("0", "false", "no", "off"):
        print("[startup] AUTO_START_BOT disabled — dashboard up, bot OFF (waiting for manual start).")
        return
    default_mode = "live" if config.trading_mode == "live" else "demo"
    if not bot_manager.is_running:
        bot_manager.start_bot(
            symbol=None,  # 100% Dynamic: Scanner will auto-pick Top 3 bullish basket!
            mode=default_mode,
            investment=0.0,  # 0.0 = Dynamically sync 100% from real Binance available balance!
            grid_count=8,
            auto_pick=True,
            basket_size=3
        )

@app.get("/api/status")
@app.get("/api/state")
def get_status(quote: Optional[str] = None):
    if not bot_manager.is_running:
        if quote:
            with bot_manager.lock:
                bot_manager.quote_asset = quote.upper()
        if bot_manager.mode in ["demo", "live"]:
            try:
                target_asset = (quote or bot_manager.quote_asset or "USDT").upper()
                trader = get_cached_trader(bot_manager.mode)
                bal = trader.get_balance(asset=target_asset, force=True)
                with bot_manager.lock:
                    if bot_manager.mode in ["demo", "live"]:
                        bot_manager.wallet_balance = bal.get("total", bot_manager.wallet_balance)
                        bot_manager.available_balance = bal.get("free", bot_manager.available_balance)
            except Exception:
                pass
        elif bot_manager.mode == "simulation":
            with bot_manager.lock:
                if bot_manager.wallet_balance is None or bot_manager.wallet_balance <= 0 or bot_manager.wallet_balance < 50.0:
                    bot_manager.wallet_balance = config.paper_initial_balance
                    bot_manager.available_balance = config.paper_initial_balance
    return bot_manager.get_state()

class SwitchQuoteAssetRequest(BaseModel):
    quote_asset: str

@app.post("/api/quote_asset/switch")
def switch_quote_asset_api(req: Optional[SwitchQuoteAssetRequest] = None):
    target_quote = req.quote_asset.upper() if (req and req.quote_asset) else "USDT"
    if target_quote not in ["USDT", "USDC"]:
        target_quote = "USDT"
    
    with bot_manager.lock:
        bot_manager.quote_asset = target_quote
    
    if not bot_manager.is_running and bot_manager.mode in ["demo", "live"]:
        try:
            trader = get_cached_trader(bot_manager.mode)
            bal = trader.get_balance(asset=target_quote, force=True)
            with bot_manager.lock:
                if bot_manager.mode in ["demo", "live"]:
                    bot_manager.wallet_balance = bal.get("total", 0.0)
                    bot_manager.available_balance = bal.get("free", 0.0)
            bot_manager.add_log(f"Margin Asset diubah ke {target_quote}. Saldo Binance: ${bot_manager.wallet_balance:.2f} {target_quote} (Tersedia: ${bot_manager.available_balance:.2f})", "info")
        except Exception as e:
            bot_manager.add_log(f"Gagal mengambil saldo {target_quote}: {e}", "warning")
    elif bot_manager.mode == "simulation":
        with bot_manager.lock:
            if bot_manager.wallet_balance is None or bot_manager.wallet_balance <= 0 or bot_manager.wallet_balance < 50.0:
                bot_manager.wallet_balance = config.paper_initial_balance
                bot_manager.available_balance = config.paper_initial_balance
        bot_manager.add_log(f"Margin Asset diubah ke {target_quote}. Saldo virtual: ${bot_manager.wallet_balance:,.2f} {target_quote}", "info")
            
    state = bot_manager.get_state()
    bot_manager.broadcast_event({"event": "STATE_UPDATE", "data": state})
    return {"success": True, "quote_asset": target_quote, "state": state}

@app.post("/api/mode/switch")
def switch_mode_api(req: Optional[SwitchModeRequest] = None):
    target_mode = req.mode.lower() if (req and req.mode) else "demo"
    if target_mode not in ["demo", "live", "simulation"]:
        target_mode = "demo"
    
    # If bot is running, stop it cleanly first before switching environment
    if bot_manager.is_running and bot_manager.mode != target_mode:
        bot_manager.stop_bot()

    with bot_manager.lock:
        bot_manager.mode = target_mode
        bot_manager.realized_profit = 0.0
        bot_manager.matched_trades = 0
        bot_manager.unrealized_pnl = 0.0
        bot_manager.position_contracts = 0.0
        bot_manager.deployed_orders.clear()
        bot_manager.active_grids.clear()

    # Sync real balance for the target mode
    target_asset = (bot_manager.quote_asset or "USDT").upper()
    if target_mode in ["demo", "live"]:
        try:
            trader = get_cached_trader(target_mode)
            bal = trader.get_balance(asset=target_asset, force=True)
            with bot_manager.lock:
                if bot_manager.mode == target_mode:
                    bot_manager.wallet_balance = bal.get("total", 0.0)
                    bot_manager.available_balance = bal.get("free", 0.0)
                    bot_manager.trader = trader
            bot_manager.add_log(f"Berhasil terhubung ke Binance {target_mode.upper()}! Saldo: ${bot_manager.wallet_balance:.2f} {target_asset} (Tersedia: ${bot_manager.available_balance:.2f})", "success")
        except Exception as e:
            bot_manager.add_log(f"Gagal mengambil saldo {target_mode.upper()}: {e}", "warning")
    elif target_mode == "simulation":
        with bot_manager.lock:
            bot_manager.wallet_balance = config.paper_initial_balance
            bot_manager.available_balance = config.paper_initial_balance
            bot_manager.trader = None
        bot_manager.add_log(f"Mode SIMULASI aktif. Saldo virtual: ${config.paper_initial_balance:.2f} {target_asset}", "info")

    state = bot_manager.get_state()
    bot_manager.broadcast_event({"event": "STATE_UPDATE", "data": state})
    return {"success": True, "mode": target_mode, "state": state}

@app.get("/api/balance")
def get_balance(mode: str = "demo", asset: str = "USDT"):
    if mode == "simulation":
        with bot_manager.lock:
            bal = bot_manager.wallet_balance if (bot_manager.mode == "simulation" and bot_manager.wallet_balance > 0) else config.paper_initial_balance
            free = bot_manager.available_balance if (bot_manager.mode == "simulation" and bot_manager.available_balance > 0) else config.paper_initial_balance
        return {"free": free, "total": bal, "asset": asset}
    try:
        trader = get_cached_trader(mode)
        return trader.get_balance(asset=asset)
    except Exception as e:
        return {"free": 0.0, "total": 0.0, "error": str(e)}

@app.get("/api/scanner")
def get_scanner(force: bool = False, quote: str = "USDT", type: str = "alpha"):
    try:
        scanner = MarketScanner()
        if type == "bullish":
            coins = scanner.scan_top_bullish_coins(limit=8, force_refresh=force, quote_asset=quote)
        else:
            # 'alpha' token kept for URL stability, but now returns the coins the LIVE auto-pick
            # can actually enter (trend != BULLISH, live volume) — same source the engine uses.
            coins = scanner.scan_edge_aligned_coins(quote_asset=quote, limit=8, force_refresh=force)
        return {"success": True, "type": type, "coins": coins}
    except Exception as e:
        return {"success": False, "error": str(e), "coins": []}

def normalize_symbol_input(symbol: str, default_quote: str = "USDT") -> str:
    """Normalizes any symbol input (e.g. 'BTC', 'BTCUSDT', 'XAU/USDT', 'SOL/USDC') dynamically."""
    if not symbol:
        return ""
    s = symbol.strip().upper().replace(" ", "").split(":")[0]
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 2 and parts[1]:
            quote = "USDT" if parts[1] == "USD" else parts[1]
            return f"{parts[0]}/{quote}"
        return s
    for q in ["USDT", "USDC", "USD1", "BUSD", "USD"]:
        if s.endswith(q) and len(s) > len(q):
            base = s[:-len(q)]
            quote = "USDT" if q == "USD" else q
            return f"{base}/{quote}"
    return f"{s}/{default_quote}"

@app.get("/api/ticker")
def get_ticker_preview(symbol: str, mode: str = "demo", investment: float = 50.0, grid_count: int = None):
    try:
        from src.market_data import MarketDataFeeder
        from src.futures_grid_engine import FuturesGridBot
        clean_sym = normalize_symbol_input(symbol)
        if grid_count is None:
            grid_count = int(getattr(config, "default_grid_count", 6))

        feeder = MarketDataFeeder()
        ticker = feeder.exchange.fetch_ticker(clean_sym)
        last_price = float(ticker.get("last") or 0.0)
        pct = float(ticker.get("percentage") or 0.0)

        # AUDIT FIX: real ATR from candles instead of the fabricated last_price*0.025.
        # A synthetic ATR would make the previewed grid geometry diverge from the live engine.
        atr = 0.0
        try:
            _df = feeder.fetch_ohlcv(clean_sym, timeframe=config.timeframe, limit=60)
            if _df is not None and len(_df) >= 20:
                from src.indicators import calculate_technical_indicators
                _ind = calculate_technical_indicators(_df)
                _atr = float(_ind["atr"].iloc[-1])
                if _atr > 0:
                    atr = _atr
        except Exception:
            atr = 0.0
        if atr <= 0:
            atr = last_price * float(getattr(config, "atr_fallback_pct", 0.02))
        grid_bot = FuturesGridBot(clean_sym, last_price, atr, leverage=config.leverage, total_investment=investment, grid_count=grid_count)
        return {
            "success": True,
            "symbol": clean_sym,
            "price": last_price,
            "change_24h": round(pct, 2),
            "high": float(ticker.get("high") or 0.0),
            "low": float(ticker.get("low") or 0.0),
            "volume": float(ticker.get("quoteVolume") or 0.0),
            "grid_summary": grid_bot.get_summary(),
            "active_grids": grid_bot.active_grids
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/positions")
def get_positions(mode: str = "demo"):
    try:
        active_mode = bot_manager.mode if bot_manager.is_running else mode
        effective_mode = mode if (mode and not bot_manager.is_running) else active_mode
        if effective_mode == "simulation":
            pos_list = []
            with bot_manager.lock:
                if bot_manager.mode == "simulation":
                    for sym, coin in bot_manager.basket_coins.items():
                        if coin.position_contracts > 0:
                            pos_list.append({
                                "symbol": sym,
                                "side": "LONG",
                                "contracts": coin.position_contracts,
                                "entry_price": coin.entry_price,
                                "mark_price": coin.current_price,
                                "margin": round((coin.entry_price * coin.position_contracts) / max(1, coin.leverage), 2),
                                "unrealized_pnl": round(coin.unrealized_pnl, 4),
                                "percentage": round((coin.unrealized_pnl / max(0.01, (coin.entry_price * coin.position_contracts) / max(1, coin.leverage))) * 100, 2),
                                "leverage": coin.leverage,
                                "liquidation_price": 0.0,
                                "tp1_price": round(coin.tp1_price, 6),
                                "tp1_filled": coin.tp1_filled,
                                "stop_loss": round(coin.grid_bot.stop_loss, 6) if coin.grid_bot else 0.0,
                                "trailing_stop_price": round(coin.trailing_stop_price, 6),
                                "is_breakeven_active": coin.is_breakeven_active
                            })
            return {"success": True, "positions": pos_list}
        trader = get_cached_trader(effective_mode)
        pos = trader.get_open_positions()
        # Enrich raw Binance positions with the bot's software-managed exit ladder.
        # TP1 / effective SL / trailing live in bot state (not on Binance), so without
        # this merge the dashboard can show the position but never its target.
        with bot_manager.lock:
            coins = dict(bot_manager.basket_coins) if bot_manager.is_running else {}
        for p in pos:
            c = coins.get(p["symbol"])
            if not c:
                continue
            gb = getattr(c, "grid_bot", None)
            p["tp1_price"] = round(c.tp1_price, 6)
            p["tp1_filled"] = c.tp1_filled
            p["stop_loss"] = round(getattr(gb, "stop_loss", 0.0), 6) if gb else 0.0
            p["trailing_stop_price"] = round(c.trailing_stop_price, 6)
            p["is_breakeven_active"] = c.is_breakeven_active
        return {"success": True, "positions": pos}
    except Exception as e:
        return {"success": False, "error": str(e), "positions": []}

def _fmt_cond(symbol: str, co: dict, now_iso: str) -> dict:
    """Normalize a raw Binance algo/conditional order into the dashboard order row shape."""
    raw_sym = co.get("symbol") or symbol or ""
    return {
        "id": str(co.get("algoId", "")),
        "time": now_iso,
        "symbol": raw_sym,
        "type": (co.get("orderType") or co.get("type") or "CONDITIONAL"),
        "side": (co.get("side") or ""),
        "price": float(co.get("triggerPrice") or co.get("stopPrice") or 0.0),
        "amount": float(co.get("quantity") or co.get("origQty") or 0.0),
        "filled": 0.0,
        "remaining": float(co.get("quantity") or co.get("origQty") or 0.0),
        "status": "OPEN (conditional)",
        "reduce_only": True,
    }

@app.get("/api/orders/open")
def get_open_orders_api(symbol: Optional[str] = None, mode: str = "demo"):
    try:
        sym = symbol.strip() if symbol and symbol.strip() and symbol.upper() != "ALL" else None
        active_mode = bot_manager.mode if bot_manager.is_running else mode
        effective_mode = mode if (mode and not bot_manager.is_running) else active_mode
        if effective_mode == "simulation":
            from datetime import datetime
            now_iso = datetime.utcnow().isoformat() + "Z"
            orders = []
            with bot_manager.lock:
                if bot_manager.mode == "simulation":
                    if bot_manager.basket_coins:
                        for b_sym, b_coin in bot_manager.basket_coins.items():
                            if sym and sym != b_sym:
                                continue
                            for oid, od in getattr(b_coin, "deployed_orders", {}).items():
                                orders.append({
                                    "id": str(oid),
                                    "time": now_iso,
                                    "symbol": b_sym,
                                    "type": "LIMIT",
                                    "side": od.get("side", "BUY"),
                                    "price": float(od.get("grid_price", 0.0)),
                                    "amount": float(od.get("quantity", 0.0)),
                                    "filled": 0.0,
                                    "remaining": float(od.get("quantity", 0.0)),
                                    "status": "OPEN"
                                })
                    elif bot_manager.deployed_orders:
                        for oid, od in bot_manager.deployed_orders.items():
                            orders.append({
                                "id": str(oid),
                                "time": now_iso,
                                "symbol": bot_manager.symbol,
                                "type": "LIMIT",
                                "side": od.get("side", "BUY"),
                                "price": float(od.get("grid_price", 0.0)),
                                "amount": float(od.get("quantity", 0.0)),
                                "filled": 0.0,
                                "remaining": float(od.get("quantity", 0.0)),
                                "status": "OPEN"
                            })
            return {"success": True, "orders": orders}
        trader = get_cached_trader(effective_mode)
        orders = trader.get_open_orders_formatted(symbol=sym)

        # Fallback 1: If symbol=None returned empty, but bot has an active target symbol, query it directly
        if not orders and bot_manager.symbol and bot_manager.is_running:
            orders = trader.get_open_orders_formatted(symbol=bot_manager.symbol)

        # Fallback 2: If symbol is None/ALL, also query each basket coin symbol directly from trader
        if not orders and bot_manager.basket_coins and bot_manager.is_running:
            for b_sym in bot_manager.basket_coins.keys():
                if sym and sym != b_sym:
                    continue
                c_orders = trader.get_open_orders_formatted(symbol=b_sym)
                if c_orders:
                    orders.extend(c_orders)

        # Fallback 3: Synchronize with bot_manager's deployed grid orders if Binance API returned empty
        if not orders and bot_manager.is_running:
            from datetime import datetime
            now_iso = datetime.utcnow().isoformat() + "Z"
            if bot_manager.basket_coins:
                for b_sym, b_coin in bot_manager.basket_coins.items():
                    if sym and sym != b_sym:
                        continue
                    for oid, od in getattr(b_coin, "deployed_orders", {}).items():
                        orders.append({
                            "id": str(oid),
                            "time": now_iso,
                            "symbol": b_sym,
                            "type": "LIMIT",
                            "side": od.get("side", "BUY"),
                            "price": float(od.get("grid_price", 0.0)),
                            "amount": float(od.get("quantity", 0.0)),
                            "filled": 0.0,
                            "remaining": float(od.get("quantity", 0.0)),
                            "status": "OPEN"
                        })
            elif bot_manager.deployed_orders:
                for oid, od in bot_manager.deployed_orders.items():
                    orders.append({
                        "id": str(oid),
                        "time": now_iso,
                        "symbol": bot_manager.symbol,
                        "type": "LIMIT",
                        "side": od.get("side", "BUY"),
                        "price": float(od.get("grid_price", 0.0)),
                        "amount": float(od.get("quantity", 0.0)),
                        "filled": 0.0,
                        "remaining": float(od.get("quantity", 0.0)),
                        "status": "OPEN"
                    })

        # Append CONDITIONAL orders (STOP_MARKET / TAKE_PROFIT_MARKET) — these live in a
        # separate Binance algo endpoint and are invisible to fetch_open_orders. Without
        # this, the dashboard's "Open Orders" hides the exchange-side stop-loss entirely.
        try:
            from datetime import datetime as _dt
            now_iso = _dt.utcnow().isoformat() + "Z"
            cond_syms = [sym] if sym else (list(bot_manager.basket_coins.keys()) if bot_manager.basket_coins else ([bot_manager.symbol] if bot_manager.symbol else []))
            if cond_syms:
                for cs in cond_syms:
                    if not cs:
                        continue
                    cond_list = trader.get_conditional_orders(cs)
                    for co in cond_list:
                        orders.append(_fmt_cond(cs, co, now_iso))
            else:
                # Bot OFF / no known symbols: still surface ALL exchange-side conditional
                # stops so the crash net is never invisible in the dashboard.
                for co in trader.get_conditional_orders(None):
                    raw_sym = co.get("symbol") or ""
                    orders.append(_fmt_cond(raw_sym, co, now_iso))
        except Exception as _e:
            print(f"[!] conditional order merge skipped: {_e}")

        return {"success": True, "orders": orders}
    except Exception as e:
        return {"success": False, "error": str(e), "orders": []}

@app.get("/api/orders/history")
def get_order_history_api(symbol: Optional[str] = None, mode: str = "demo", limit: int = 40):
    try:
        active_mode = bot_manager.mode if bot_manager.is_running else mode
        effective_mode = mode if (mode and not bot_manager.is_running) else active_mode
        if effective_mode == "simulation":
            return {"success": True, "history": []}
        sym = symbol.strip() if symbol and symbol.strip() and symbol.upper() != "ALL" else None
        trader = get_cached_trader(effective_mode)
        history = trader.get_order_history(symbol=sym, limit=limit)
        return {"success": True, "history": history}
    except Exception as e:
        return {"success": False, "error": str(e), "history": []}

@app.get("/api/trades/history")
def get_trade_history_api(symbol: Optional[str] = None, mode: str = "demo", limit: int = 40):
    try:
        active_mode = bot_manager.mode if bot_manager.is_running else mode
        effective_mode = mode if (mode and not bot_manager.is_running) else active_mode
        if effective_mode == "simulation":
            return {"success": True, "trades": []}
        sym = symbol.strip() if symbol and symbol.strip() and symbol.upper() != "ALL" else None
        trader = get_cached_trader(effective_mode)
        trades = trader.get_trade_history(symbol=sym, limit=limit)
        return {"success": True, "trades": trades}
    except Exception as e:
        return {"success": False, "error": str(e), "trades": []}

# In-memory cache for Binance Futures exchangeInfo and market pairs
_exchange_info_cache = {"timestamp": 0.0, "symbols": {}}
_market_pairs_cache = {"timestamp": 0.0, "pairs": []}

@app.get("/api/market/all_pairs")
async def get_all_futures_market_pairs(quote: Optional[str] = None, force: bool = False):
    """
    100% Dynamic Binance USDⓈ-M Futures market data:
    Retrieves baseAsset, quoteAsset, status directly from Binance ExchangeInfo,
    combined with live 24hr tickers, volume, and funding rates.
    """
    import time
    now = time.time()
    
    # Return cache if valid (10s TTL)
    if not force and _market_pairs_cache["pairs"] and (now - _market_pairs_cache["timestamp"] < 10.0):
        all_pairs = _market_pairs_cache["pairs"]
        if quote and quote.upper() != "ALL":
            filtered = [p for p in all_pairs if p.get("quote") == quote.upper()]
        else:
            filtered = all_pairs
        return {
            "success": True,
            "count": len(filtered),
            "cached": True,
            "pairs": filtered
        }

    try:
        import ccxt
        loop = asyncio.get_running_loop()
        exchange = ccxt.binanceusdm({'enableRateLimit': True, 'timeout': 6000})
        if bot_manager.mode == "demo":
            for k, v in list(exchange.urls['api'].items()):
                if isinstance(v, str) and 'https://fapi.binance.com' in v:
                    exchange.urls['api'][k] = v.replace('https://fapi.binance.com', 'https://demo-fapi.binance.com')

        def _route_demo(ex):
            for k, v in list(ex.urls['api'].items()):
                if isinstance(v, str) and 'https://fapi.binance.com' in v:
                    ex.urls['api'][k] = v.replace('https://fapi.binance.com', 'https://demo-fapi.binance.com')

        # 1. Dynamically fetch or reuse ExchangeInfo (1 hour TTL)
        symbols_info = _exchange_info_cache.get("symbols", {})
        if not symbols_info or (now - _exchange_info_cache.get("timestamp", 0.0) > 3600.0) or force:
            try:
                raw_info = await loop.run_in_executor(None, exchange.fapiPublicGetExchangeInfo)
            except Exception:
                _route_demo(exchange)
                raw_info = await loop.run_in_executor(None, exchange.fapiPublicGetExchangeInfo)
            symbols_info = {}
            for s in raw_info.get("symbols", []):
                sym_code = s.get("symbol")
                if sym_code and s.get("status") == "TRADING":
                    symbols_info[sym_code] = {
                        "base": s.get("baseAsset", ""),
                        "quote": s.get("quoteAsset", ""),
                        "margin": s.get("marginAsset", s.get("quoteAsset", ""))
                    }
            _exchange_info_cache["timestamp"] = now
            _exchange_info_cache["symbols"] = symbols_info

        # 2. Concurrently fetch live 24hr tickers and funding rates
        try:
            tickers, premiums = await loop.run_in_executor(
                None,
                lambda: (exchange.fapiPublicGetTicker24hr(), exchange.fapiPublicGetPremiumIndex())
            )
        except Exception:
            _route_demo(exchange)
            tickers, premiums = await loop.run_in_executor(
                None,
                lambda: (exchange.fapiPublicGetTicker24hr(), exchange.fapiPublicGetPremiumIndex())
            )

        fr_map = {}
        for p in premiums:
            sym = p.get("symbol")
            if sym and "lastFundingRate" in p:
                try:
                    fr_map[sym] = float(p["lastFundingRate"]) * 100.0
                except Exception:
                    pass

        # 3. Assemble dynamic pair metadata without any hardcoded slicing
        pairs = []
        for t in tickers:
            sym_raw = t.get("symbol", "")
            meta = symbols_info.get(sym_raw)
            if not meta:
                continue

            base = meta["base"]
            quote_asset = meta["quote"]
            ccxt_symbol = f"{base}/{quote_asset}"

            try:
                price = float(t.get("lastPrice", 0))
                chg_pct = float(t.get("priceChangePercent", 0))
                vol_quote = float(t.get("quoteVolume", 0))
            except Exception:
                continue

            if vol_quote >= 1_000_000_000:
                vol_str = f"{vol_quote / 1_000_000_000:.2f}B"
            elif vol_quote >= 1_000_000:
                vol_str = f"{vol_quote / 1_000_000:.2f}M"
            elif vol_quote >= 1_000:
                vol_str = f"{vol_quote / 1_000:.2f}K"
            else:
                vol_str = f"{vol_quote:.2f}"

            funding_rate = fr_map.get(sym_raw, 0.0)
            is_gold = any(g in base.upper() for g in ["XAU", "PAXG", "GOLD"])

            pairs.append({
                "symbol": ccxt_symbol,
                "raw_symbol": sym_raw,
                "base": base,
                "quote": quote_asset,
                "price": price,
                "change_pct": round(chg_pct, 2),
                "volume_usdt": vol_quote,
                "volume_str": vol_str,
                "funding_rate": funding_rate,
                "is_gold": is_gold
            })

        # Sort highest volume first
        pairs.sort(key=lambda x: x["volume_usdt"], reverse=True)
        _market_pairs_cache["timestamp"] = now
        _market_pairs_cache["pairs"] = pairs

        if quote and quote.upper() != "ALL":
            result_pairs = [p for p in pairs if p.get("quote") == quote.upper()]
        else:
            result_pairs = pairs

        return {
            "success": True,
            "count": len(result_pairs),
            "cached": False,
            "pairs": result_pairs
        }
    except Exception as e:
        if _market_pairs_cache["pairs"]:
            all_cached = _market_pairs_cache["pairs"]
            return {
                "success": True,
                "count": len(all_cached),
                "cached": True,
                "pairs": all_cached,
                "warning": str(e)
            }
        # Fallback: Dynamically query fallback demo-fapi endpoint for real live tickers
        try:
            fb_ex = bot_manager.feeder._get_fallback_exchange()
            fb_tickers = await loop.run_in_executor(None, fb_ex.fetch_tickers)
            live_pairs = []
            for s_name, t_data in fb_tickers.items():
                if not (s_name.endswith("/USDT") or s_name.endswith("/USDC") or s_name.endswith("/USDT:USDT") or s_name.endswith("/USDC:USDC")):
                    continue
                lp = float(t_data.get("last", 0) or 0)
                if lp <= 0:
                    continue
                clean_s = s_name.split(":")[0]
                b_asset, q_asset = clean_s.split("/")
                vq = float(t_data.get("quoteVolume", 0) or 0)
                v_str = f"{vq/1e6:.2f}M" if vq >= 1e6 else f"{vq/1e3:.2f}K"
                live_pairs.append({
                    "symbol": clean_s,
                    "raw_symbol": clean_s.replace("/", ""),
                    "base": b_asset,
                    "quote": q_asset,
                    "price": lp,
                    "change_pct": round(float(t_data.get("percentage", 0) or 0), 2),
                    "volume_usdt": vq,
                    "volume_str": v_str,
                    "funding_rate": 0.01,
                    "is_gold": "XAU" in b_asset or "PAXG" in b_asset
                })
            live_pairs.sort(key=lambda x: x["volume_usdt"], reverse=True)
            if quote and quote.upper() != "ALL":
                live_pairs = [p for p in live_pairs if p.get("quote") == quote.upper()]
            if live_pairs:
                _market_pairs_cache["timestamp"] = now
                _market_pairs_cache["pairs"] = live_pairs
                return {"success": True, "count": len(live_pairs), "cached": False, "pairs": live_pairs}
        except Exception:
            pass

        return {"success": False, "count": 0, "cached": False, "pairs": [], "error": f"Gagal memuat pair Binance: {e}"}

@app.post("/api/bot/start")
def start_bot(req: Optional[StartBotRequest] = None):
    if req is None:
        req = StartBotRequest()
    res = bot_manager.start_bot(
        symbol=req.symbol,
        mode=req.mode,
        investment=req.investment,
        grid_count=req.grid_count,
        auto_pick=req.auto_pick,
        stop_loss=req.stop_loss,
        basket_size=req.basket_size,
        leverage=req.leverage,
        quote_asset=req.quote_asset
    )
    return res

@app.post("/api/bot/stop")
def stop_bot():
    return bot_manager.stop_bot()

@app.get("/api/ledger/verify")
def ledger_verify():
    """Replay the append-only audit ledger hash chain; reports first tampered seq."""
    try:
        from src.trade_ledger import ledger
        return ledger.verify()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/ledger/tail")
def ledger_tail(n: int = 20):
    """Most recent ledger entries (order attempts, fills, realised exits)."""
    try:
        from src.trade_ledger import ledger
        return {"entries": ledger.tail(n), "stats": ledger.stats()}
    except Exception as e:
        return {"entries": [], "error": str(e)}

class CancelOrderRequest(BaseModel):
    order_id: str
    symbol: str
    mode: Optional[str] = "demo"

class ClosePositionRequest(BaseModel):
    symbol: str
    mode: Optional[str] = "demo"

@app.post("/api/orders/cancel")
def cancel_single_order(req: CancelOrderRequest, mode: Optional[str] = None):
    try:
        target_mode = req.mode or mode or "demo"
        if target_mode == "simulation":
            with bot_manager.lock:
                if req.order_id in bot_manager.deployed_orders:
                    del bot_manager.deployed_orders[req.order_id]
                for c in bot_manager.basket_coins.values():
                    if req.order_id in c.deployed_orders:
                        del c.deployed_orders[req.order_id]
            return {"success": True}
        trader = get_cached_trader(target_mode)
        ok = trader.cancel_order(req.order_id, req.symbol)
        return {"success": ok}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/orders/cancel-all")
def cancel_all_orders_api(symbol: Optional[str] = None, mode: str = "demo"):
    try:
        active_mode = bot_manager.mode if bot_manager.is_running else mode
        if active_mode == "simulation":
            bot_manager.reset_orders_and_target()
            return {"success": True, "cancelled_count": 0}
        trader = get_cached_trader(active_mode)
        sym = symbol.strip() if symbol and symbol.strip() and symbol.upper() != "ALL" else None
        cancelled = trader.cancel_all_orders(sym)
        # Reset bot target coin & clear visual state
        bot_manager.reset_orders_and_target()
        return {"success": True, "cancelled_count": cancelled}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/position/close")
def close_position_api(req: ClosePositionRequest, mode: Optional[str] = None):
    try:
        target_mode = req.mode or mode or "demo"
        if target_mode == "simulation":
            with bot_manager.lock:
                for sym, c in bot_manager.basket_coins.items():
                    if not req.symbol or sym == req.symbol:
                        c.position_contracts = 0.0
                        c.unrealized_pnl = 0.0
            bot_manager.broadcast_event({"event": "STATE_UPDATE", "data": bot_manager.get_state()})
            return {"success": True, "message": f"Simulation position for {req.symbol} closed."}
        trader = get_cached_trader(target_mode)
        res = trader.close_position(req.symbol)
        try:
            target_asset = (bot_manager.quote_asset or "USDT").upper()
            bal = trader.get_balance(asset=target_asset, force=True)
            with bot_manager.lock:
                if bot_manager.mode in ["demo", "live"]:
                    bot_manager.wallet_balance = bal.get("total", bot_manager.wallet_balance)
                    bot_manager.available_balance = bal.get("free", bot_manager.available_balance)
            bot_manager.broadcast_event({"event": "STATE_UPDATE", "data": bot_manager.get_state()})
        except Exception:
            pass
        return res
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    bot_manager.ws_clients.add(websocket)
    try:
        # Send initial state
        await websocket.send_json({"event": "INITIAL_STATE", "data": bot_manager.get_state()})
        while True:
            # Keep socket alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        bot_manager.ws_clients.discard(websocket)
    except Exception:
        bot_manager.ws_clients.discard(websocket)

# Static files & SPA fallback
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
os.makedirs(WEB_DIR, exist_ok=True)

@app.get("/")
def serve_index():
    index_path = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return JSONResponse({"status": "Dashboard frontend starting..."})

app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
