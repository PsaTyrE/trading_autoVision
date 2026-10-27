import time
import hashlib
import hmac
import logging
import urllib.request
import ccxt
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor
from src.config import config

logger = logging.getLogger(__name__)

class BinanceFuturesTrader:
    """
    Executes orders on Binance Futures (USD-M) via CCXT.
    Supports both Binance Real Futures and Binance Demo Trading (demo-fapi).
    """
    def __init__(self, is_testnet: Optional[bool] = None):
        if is_testnet is not None:
            self.is_testnet = is_testnet
        else:
            self.is_testnet = (config.trading_mode != "live")

        # Dynamically route credentials: Use LIVE credentials if is_testnet is False, else DEMO credentials
        if self.is_testnet:
            api_key = config.binance_demo_api_key or config.binance_api_key
            api_secret = config.binance_demo_api_secret or config.binance_api_secret
        else:
            api_key = config.binance_live_api_key or config.binance_api_key
            api_secret = config.binance_live_api_secret or config.binance_api_secret

        exchange_params = {
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': {
                'adjustForTimeDifference': True,
                'recvWindow': 60000,
                'fetchCurrencies': False,
                'fetchOpenOrders': {'warnWithoutSymbol': False},
            }
        }

        self.exchange = ccxt.binanceusdm(exchange_params)
        self.exchange.has['fetchCurrencies'] = False
        # Diag key: fingerprint only (last 4), never the secret — proves which key is live.
        try:
            _fp = str(getattr(self.exchange, "apiKey", "") or "")[-4:]
            print(f"[Binance] {'DEMO' if self.is_testnet else 'LIVE'} apiKey fingerprint: ****{_fp}")
        except Exception:
            pass

        if self.is_testnet:
            # Route all fapi endpoints to Binance Demo Trading API
            for k, v in list(self.exchange.urls['api'].items()):
                if isinstance(v, str) and 'https://fapi.binance.com' in v:
                    self.exchange.urls['api'][k] = v.replace('https://fapi.binance.com', 'https://demo-fapi.binance.com')

        # Synchronize Windows clock offset
        for attempt in range(3):
            try:
                diff = self.exchange.load_time_difference()
                print(f"[Binance Sync] Clock synchronized with Binance server (offset: {diff}ms)")
                break
            except Exception as e:
                import time
                if attempt == 2:
                    print(f"[!] Warning syncing clock with Binance: {e}")
                time.sleep(1)

        # Load markets for precision & symbol rules
        try:
            self.exchange.load_markets()
        except Exception as e:
            print(f"[!] Warning loading markets: {e}")

        self._cache: Dict[str, Any] = {}
        self.last_error: str = ""
        # AUDIT FIX: explicit health flag for the conditional-order endpoint. Empty list
        # must mean 'no stops', never 'the fetch failed'.
        self.conditional_orders_ok: bool = True

    def resolve_symbol(self, symbol: Optional[str]) -> Optional[str]:
        """Resolves symbol to exact CCXT Binance USD-M format (e.g. XAUUSD / XAU/USD -> XAU/USDT:USDT)."""
        if not symbol:
            return None
        s = symbol.strip().upper().replace(" ", "").split(':')[0]        
        candidates = [s]
        if "/" in s:
            parts = s.split("/")
            if len(parts) == 2:
                base = parts[0]
                quote = "USDT" if parts[1] == "USD" else parts[1]
                candidates.extend([f"{base}/{quote}:{quote}", f"{base}/{quote}"])
        else:
            if s.endswith("USDC"):
                base = s[:-4]
                candidates.extend([f"{base}/USDC:USDC", f"{base}/USDC"])
            elif s.endswith("USDT"):
                base = s[:-4]
                candidates.extend([f"{base}/USDT:USDT", f"{base}/USDT"])
            elif s.endswith("USD"):
                base = s[:-3]
                candidates.extend([f"{base}/USDT:USDT", f"{base}/USDT"])
            else:
                candidates.extend([f"{s}/USDT:USDT", f"{s}/USDC:USDC", f"{s}/USDT"])

        if hasattr(self.exchange, "markets") and self.exchange.markets:
            for cand in candidates:
                if cand in self.exchange.markets:
                    if getattr(config, "tradfi_blacklist_tradifi", False):
                        try:
                            _info = ((self.exchange.markets[cand] or {}).get("info") or {})
                            if _info.get("contractType") == "TRADIFI_PERPETUAL" or "TradFi" in str(_info.get("underlyingSubType", "")):
                                self.last_error = f"TRADIFI_PERPETUAL blocked by tradfi_blacklist_tradifi: {cand}"
                                return None
                        except Exception:
                            pass
                    return cand

        for cand in candidates:
            if ":" in cand:
                return cand
        fallback_quote = s.split('/')[1] if '/' in s else 'USDT'
        base = s.split('/')[0]
        return f"{base}/{fallback_quote}:{fallback_quote}"

    def get_trading_fee_rate(self, symbol: str) -> Optional[float]:
        """Live taker fee rate straight from Binance (VIP-tier aware). None if unavailable,
        so callers fall back to config instead of guessing."""
        resolved_sym = self.resolve_symbol(symbol)
        ckey = f"fee_{resolved_sym}"
        now = time.time()
        cached = self._cache.get(ckey)
        if cached and (now - cached["t"] < float(getattr(config, "fee_cache_ttl_seconds", 300))):
            return cached["data"]
        rate = None
        try:
            fee = self.exchange.fetch_trading_fee(resolved_sym)
            raw = fee.get("taker")
            rate = float(raw) if raw is not None else None
        except Exception as e:
            self.last_error = str(e)
        self._cache[ckey] = {"t": now, "data": rate}
        return rate

    def get_balance(self, asset: Optional[str] = "USDT", force: bool = False) -> Dict[str, Any]:
        """Fetches exact real cash balance from Binance for the specified asset (default: USDT, or USDC)."""
        target_asset = (asset or "USDT").upper()
        now = time.time()
        ckey = f"balance_{target_asset}"
        cached = self._cache.get(ckey)
        if not force and cached and (now - cached["t"] < 5.0):
            return cached["data"]
        try:
            balance = self.exchange.fetch_balance()
            info = balance.get('info', {})

            # 1. Read specific asset dictionary from CCXT
            asset_dict = balance.get(target_asset, {})
            free = float(asset_dict.get('free', 0.0) or 0.0)
            total = float(asset_dict.get('total', 0.0) or 0.0)

            # 2. Extract exact walletBalance and availableBalance from Binance raw assets array
            for a in info.get('assets', []):
                if a.get('asset') == target_asset:
                    wb = float(a.get('walletBalance', 0.0) or 0.0)
                    avail = float(a.get('availableBalance', 0.0) or 0.0)
                    if wb > 0:
                        total = wb
                    if avail > 0:
                        free = avail
                    break

            # Also provide breakout for USDT and USDC
            usdt_bal = float(balance.get('USDT', {}).get('total', 0.0) or 0.0)
            usdc_bal = float(balance.get('USDC', {}).get('total', 0.0) or 0.0)
            for a in info.get('assets', []):
                if a.get('asset') == 'USDT' and float(a.get('walletBalance', 0.0) or 0.0) > 0:
                    usdt_bal = float(a.get('walletBalance'))
                elif a.get('asset') == 'USDC' and float(a.get('walletBalance', 0.0) or 0.0) > 0:
                    usdc_bal = float(a.get('walletBalance'))

            res = {
                "free": free,
                "total": total,
                "asset": target_asset,
                "usdt": usdt_bal,
                "usdc": usdc_bal
            }
            self._cache[ckey] = {"t": now, "data": res}
            return res
        except Exception as e:
            if cached:
                cached["t"] = now + 15.0
                return cached["data"]
            print(f"[!] Error fetching Binance balance: {e}")
            fallback_res = {"free": 0.0, "total": 0.0, "asset": target_asset, "usdt": 0.0, "usdc": 0.0}
            self._cache[ckey] = {"t": now + 15.0, "data": fallback_res}
            return fallback_res

    def check_other_wallets(self, asset: Optional[str] = None) -> Dict[str, float]:
        """Checks if asset exists in Spot or Simple Earn to guide the user."""
        res = {"spot": 0.0, "earn": 0.0}
        if self.is_testnet:
            return res
        target_asset = (asset or config.default_quote_asset).upper()
        try:
            spot = ccxt.binance({
                'apiKey': self.exchange.apiKey,
                'secret': self.exchange.secret,
                'enableRateLimit': True,
                'options': {'adjustForTimeDifference': True, 'recvWindow': 60000}
            })
            spot.load_time_difference()
            try:
                b = spot.fetch_balance()
                res["spot"] = float(b.get(target_asset, {}).get("free", 0.0) or 0.0)
            except Exception as e:
                logger.debug(f"Spot balance check failed: {e}")
            try:
                earn = spot.sapiGetSimpleEarnFlexiblePosition({'size': 10})
                for r in earn.get('rows', []):
                    if r.get('asset') == target_asset:
                        res["earn"] = float(r.get('totalAmount', 0.0) or 0.0)
            except Exception as e:
                logger.debug(f"Earn balance check failed: {e}")
        except Exception as e:
            print(f"[!] Error checking other wallets: {e}")
        return res

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetches active futures positions with full trading details (with 2s TTL cache)."""
        now = time.time()
        ckey = f"pos_{symbol or 'all'}"
        cached = self._cache.get(ckey)
        if cached and (now - cached["t"] < 5.0):
            return cached["data"]
        # RETRY_FIX: NetworkError transient, retry with backoff
        for attempt in range(3):
            try:
                # Fetch without symbol filter so CCXT doesn't reject symbol format
                positions = self.exchange.fetch_positions()
                break
            except Exception as e:
                if attempt < 2:
                    wait = 2 ** attempt  # 1s, 2s
                    print(f"[!] Position fetch attempt {attempt+1}/3 failed: {e}. Retry in {wait}s...")
                    time.sleep(wait)
                else:
                    raise

        active = []
        target_clean = symbol.split(':')[0] if symbol else None
        for p in positions:
            contracts = float(p.get('contracts', 0) or 0)
            if abs(contracts) > 0:
                pos_sym = p['symbol'].split(':')[0]
                if target_clean and pos_sym != target_clean:
                    continue
                entry = float(p.get('entryPrice', 0) or 0)
                mark = float(p.get('markPrice', 0) or 0)
                pnl = float(p.get('unrealizedPnl', 0) or 0)
                liq = float(p.get('liquidationPrice', 0) or 0)
                lev = int(p.get('leverage', config.leverage) or config.leverage)
                notional = float(p.get('notional', 0) or (contracts * mark))
                margin = notional / lev if lev > 0 else 0
                pct = float(p.get('percentage', 0) or ((pnl / margin * 100) if margin > 0 else 0))

                active.append({
                    "symbol": pos_sym,
                    # SIDE_FIX: contracts is unsigned in ccxt; `or contracts > 0` made every row LONG.
                    "side": "LONG" if p.get('side') == 'long' else "SHORT",
                    "contracts": contracts,
                    "entry_price": entry,
                    "mark_price": mark,
                    "unrealized_pnl": round(pnl, 4),
                    "percentage": round(pct, 2),
                    "leverage": lev,
                    "liquidation_price": liq,
                    "margin": round(margin, 2)
                })
        self._cache[ckey] = {"t": now, "data": active}
        return active

    def get_realized_pnl(self, symbol: Optional[str] = None, limit: int = 50) -> float:
        """Fetches real realized PnL directly from Binance Futures account income (with 5s cache)."""
        now = time.time()
        ckey = f"realized_pnl_{symbol or 'all'}"
        cached = self._cache.get(ckey)
        if cached and (now - cached["t"] < 5.0):
            return cached["data"]
        try:
            params = {'incomeType': 'REALIZED_PNL', 'limit': limit}
            if symbol and symbol.strip() and symbol.upper() != "ALL":
                clean = symbol.replace('/', '').split(':')[0]
                params['symbol'] = clean
            res = self.exchange.fapiPrivateGetIncome(params)
            total = sum(float(x.get('income', 0.0)) for x in res)
            val = round(total, 4)
            self._cache[ckey] = {"t": now, "data": val}
            return val
        except Exception as e:
            if cached:
                return cached["data"]
            return 0.0

    def get_order_fill(self, symbol: str, order_id: str) -> Dict[str, Any]:
        """
        Returns the ACTUAL fill details of an order (average fill price, filled qty, and fee cost).
        Used for accurate, fee-inclusive PnL accounting instead of theoretical grid prices.
        """
        try:
            resolved_sym = self.resolve_symbol(symbol)
            o = self.exchange.fetch_order(order_id, resolved_sym)
            avg = float(o.get("average") or o.get("price") or 0.0)
            filled = float(o.get("filled") or 0.0)
            fee_cost = 0.0
            fee = o.get("fee")
            if isinstance(fee, dict) and fee.get("cost") is not None:
                fee_cost = abs(float(fee.get("cost") or 0.0))
            # Binance order payloads do not always carry commission; when missing,
            # the authoritative fee is taken from get_net_income() (COMMISSION income type).
            return {"avg_price": avg, "filled": filled, "fee_cost": fee_cost, "raw": o}
        except Exception as e:
            self.last_error = str(e)
            return {"avg_price": 0.0, "filled": 0.0, "fee_cost": 0.0, "raw": None}

    def get_net_income(self, symbol: Optional[str] = None, since_ms: Optional[int] = None) -> Dict[str, float]:
        """
        Fetches NET realized income from Binance Futures for a symbol (or all symbols):
        REALIZED_PNL + COMMISSION + FUNDING_FEE. This is the exchange's own truth about
        money made/lost, so the dashboard never over-reports profit by ignoring fees.
        """
        try:
            types = ["REALIZED_PNL", "COMMISSION", "FUNDING_FEE"]
            total = 0.0
            breakdown = {"REALIZED_PNL": 0.0, "COMMISSION": 0.0, "FUNDING_FEE": 0.0}
            params: Dict[str, Any] = {"limit": 1000}
            if symbol and symbol.strip() and symbol.upper() != "ALL":
                clean = symbol.replace('/', '').split(':')[0]
                params["symbol"] = clean
            if since_ms:
                params["startTime"] = int(since_ms)
            res = self.exchange.fapiPrivateGetIncome(params)
            for x in res or []:
                it = x.get("incomeType")
                if it in breakdown:
                    breakdown[it] += float(x.get("income", 0.0) or 0.0)
            total = sum(breakdown.values())
            return {"net": round(total, 6), **{k: round(v, 6) for k, v in breakdown.items()}}
        except Exception as e:
            self.last_error = str(e)
            return {"net": 0.0, "REALIZED_PNL": 0.0, "COMMISSION": 0.0, "FUNDING_FEE": 0.0}

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetches active open orders (with 2s TTL cache)."""
        now = time.time()
        ckey = f"orders_{symbol or 'all'}"
        cached = self._cache.get(ckey)
        if cached and (now - cached["t"] < 5.0):
            return cached["data"]
        try:
            resolved_sym = self.resolve_symbol(symbol)
            if resolved_sym and hasattr(self.exchange, "markets") and self.exchange.markets:
                if resolved_sym not in self.exchange.markets:
                    return []
            orders = self.exchange.fetch_open_orders(symbol=resolved_sym)
            self._cache[ckey] = {"t": now, "data": orders}
            return orders
        except Exception as e:
            if cached:
                cached["t"] = now + 15.0
                return cached["data"]
            self._cache[ckey] = {"t": now + 15.0, "data": []}
            return []

    def get_open_orders_formatted(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetches and cleans active open orders for frontend display."""
        try:
            orders = self.get_open_orders(symbol)
            formatted = []
            for o in orders:
                formatted.append({
                    "id": o.get("id"),
                    "time": o.get("datetime") or o.get("timestamp"),
                    "symbol": o.get("symbol", "").split(":")[0],
                    "type": (o.get("type") or "LIMIT").upper(),
                    "side": (o.get("side") or "BUY").upper(),
                    "price": float(o.get("price") or 0.0),
                    "amount": float(o.get("amount") or 0.0),
                    "filled": float(o.get("filled") or 0.0),
                    "remaining": float(o.get("remaining") or (float(o.get("amount") or 0) - float(o.get("filled") or 0))),
                    "status": (o.get("status") or "NEW").upper()
                })
            return formatted
        except Exception as e:
            print(f"[!] Error fetching formatted open orders: {e}")
            return []

    def _collect_historical_symbols(self, symbol: Optional[str] = None) -> List[str]:
        """Gathers relevant symbols to query order and trade history from Binance with caching."""
        target_symbols = []
        if symbol and symbol.strip() and symbol.upper() != "ALL":
            target_symbols.append(symbol.strip().split(':')[0])
            return target_symbols

        now = time.time()
        cached = self._cache.get("hist_symbols")
        if cached and (now - cached["t"] < 30.0):
            return cached["data"]

        # 1. From active open orders
        for o in self.get_open_orders():
            sym = (o.get("symbol") or "").split(":")[0]
            if sym and sym not in target_symbols:
                target_symbols.append(sym)

        # 2. From active positions
        for p in self.get_open_positions():
            sym = p.get("symbol")
            if sym and sym not in target_symbols:
                target_symbols.append(sym)

        # 3. From recent Binance Futures account income (e.g. QNTUSDT -> QNT/USDT)
        if not self.is_testnet:
            try:
                res = self.exchange.fapiPrivateGetIncome({'limit': 20})
                for item in res:
                    raw_sym = item.get('symbol')
                    if raw_sym:
                        for q in ['USDT', 'USDC']:
                            if raw_sym.endswith(q):
                                base = raw_sym[:-len(q)]
                                coin_sym = f"{base}/{q}"
                                if coin_sym not in target_symbols:
                                    target_symbols.append(coin_sym)
            except Exception as e:
                logger.debug(f"Symbol parsing from orders failed: {e}")

        # 4. From config default trading symbols
        for cs in config.trading_symbols:
            clean_cs = cs.split(':')[0]
            if clean_cs not in target_symbols:
                target_symbols.append(clean_cs)

        self._cache["hist_symbols"] = {"t": now, "data": target_symbols}
        return target_symbols

    def get_order_history(self, symbol: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
        """Fetches historical orders (FILLED, CANCELED, etc.) from Binance dynamically with parallel execution & cache."""
        now = time.time()
        ckey = f"order_hist_{symbol or 'all'}_{limit}"
        cached = self._cache.get(ckey)
        if cached and (now - cached["t"] < 10.0):
            return cached["data"]
        try:
            target_symbols = self._collect_historical_symbols(symbol)
            if not target_symbols:
                return []

            all_orders = []
            def _fetch_sym_orders(sym):
                try:
                    resolved = self.resolve_symbol(sym)
                    return self.exchange.fetch_orders(symbol=resolved, limit=limit)
                except Exception:
                    return []

            with ThreadPoolExecutor(max_workers=5) as executor:
                for res in executor.map(_fetch_sym_orders, target_symbols[:6]):
                    if res:
                        all_orders.extend(res)

            formatted = []
            for o in sorted(all_orders, key=lambda x: x.get('timestamp') or 0, reverse=True)[:limit]:
                formatted.append({
                    "id": str(o.get("id") or ""),
                    "time": o.get("datetime") or o.get("timestamp"),
                    "symbol": (o.get("symbol") or "").split(":")[0],
                    "type": (o.get("type") or "LIMIT").upper(),
                    "side": (o.get("side") or "BUY").upper(),
                    "price": float(o.get("price") or 0.0),
                    "amount": float(o.get("amount") or 0.0),
                    "filled": float(o.get("filled") or 0.0),
                    "status": (o.get("status") or "UNKNOWN").upper()
                })
            self._cache[ckey] = {"t": now, "data": formatted}
            return formatted
        except Exception as e:
            if cached:
                cached["t"] = now + 15.0  # Back off
                return cached["data"]
            print(f"[!] Error fetching order history: {e}")
            self._cache[ckey] = {"t": now + 15.0, "data": []}
            return []

    def get_trade_history(self, symbol: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
        """Fetches user executed trades from Binance dynamically with parallel execution & cache."""
        now = time.time()
        ckey = f"trade_hist_{symbol or 'all'}_{limit}"
        cached = self._cache.get(ckey)
        if cached and (now - cached["t"] < 10.0):
            return cached["data"]
        try:
            target_symbols = self._collect_historical_symbols(symbol)
            if not target_symbols:
                return []

            all_trades = []
            def _fetch_sym_trades(sym):
                try:
                    resolved = self.resolve_symbol(sym)
                    return self.exchange.fetch_my_trades(symbol=resolved, limit=limit)
                except Exception:
                    return []

            with ThreadPoolExecutor(max_workers=5) as executor:
                for res in executor.map(_fetch_sym_trades, target_symbols[:6]):
                    if res:
                        all_trades.extend(res)

            formatted = []
            for t in sorted(all_trades, key=lambda x: x.get('timestamp') or 0, reverse=True)[:limit]:
                fee_val = 0.0
                fee_curr = "USDT"
                fee_info = t.get("fee")
                if fee_info:
                    fee_val = float(fee_info.get("cost") or 0.0)
                    fee_curr = fee_info.get("currency") or "USDT"

                formatted.append({
                    "id": str(t.get("id") or ""),
                    "time": t.get("datetime") or t.get("timestamp"),
                    "symbol": (t.get("symbol") or "").split(":")[0],
                    "side": (t.get("side") or "BUY").upper(),
                    "price": float(t.get("price") or 0.0),
                    "amount": float(t.get("amount") or 0.0),
                    "cost": float(t.get("cost") or (float(t.get("price") or 0) * float(t.get("amount") or 0))),
                    "fee": f"{fee_val:.4f} {fee_curr}",
                    "order_id": str(t.get("order") or "")
                })
            self._cache[ckey] = {"t": now, "data": formatted}
            return formatted
        except Exception as e:
            if cached:
                cached["t"] = now + 15.0  # Back off
                return cached["data"]
            print(f"[!] Error fetching trade history: {e}")
            self._cache[ckey] = {"t": now + 15.0, "data": []}
            return []

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancels a specific order."""
        try:
            self._cache.clear()
            self.exchange.cancel_order(order_id, symbol)
            return True
        except Exception as e:
            print(f"[!] Error canceling order {order_id}: {e}")
            return False

    def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """Cancels all open orders efficiently in a single bulk call per symbol.

        Also clears CONDITIONAL stops (separate algo endpoint that cancel_all_orders
        and fetch_open_orders both miss), so a fresh start / close leaves nothing behind.
        """
        cancelled_count = 0
        self._cache.clear()
        cancelled_count += self.cancel_conditional_orders(symbol)
        try:
            resolved_sym = self.resolve_symbol(symbol)
            if resolved_sym:
                try:
                    self.exchange.cancel_all_orders(resolved_sym)
                    return 1
                except Exception as e:
                    logger.debug(f"Cancel all orders on resolved sym failed: {e}")

            orders = self.get_open_orders(symbol)
            symbols_to_cancel = set()
            for o in orders:
                sym = o.get("symbol") or resolved_sym
                if sym:
                    symbols_to_cancel.add(sym)

            for sym in symbols_to_cancel:
                rsym = self.resolve_symbol(sym)
                try:
                    self.exchange.cancel_all_orders(rsym)
                    cancelled_count += 1
                except Exception as e:
                    logger.debug(f"Cancel all orders on {sym} failed: {e}")
                    for o in [ord for ord in orders if (ord.get("symbol") or resolved_sym) == sym]:
                        try:
                            self.exchange.cancel_order(o["id"], rsym)
                            cancelled_count += 1
                        except Exception as e2:
                            logger.debug(f"Cancel individual order {o.get('id')} failed: {e2}")
            return cancelled_count
        except Exception as e:
            print(f"[!] Error in cancel_all_orders: {e}")
            return cancelled_count

    def close_position(self, symbol: str) -> Dict[str, Any]:
        """Closes any active position on the symbol at market price with reduceOnly, auto-chunking if > MARKET_LOT_SIZE."""
        try:
            self._cache.clear()
            positions = self.get_open_positions(symbol)
            if not positions or abs(positions[0].get("contracts", 0)) == 0:
                return {"success": True, "closed": False, "message": f"Tidak ada posisi aktif pada {symbol}."}

            pos = positions[0]
            contracts = abs(pos["contracts"])
            close_side = "sell" if pos["side"] == "LONG" else "buy"
            resolved_sym = self.resolve_symbol(symbol)

            if not self.exchange.markets:
                self.exchange.load_markets()

            market = self.exchange.market(resolved_sym)
            max_market_qty = float(market.get('limits', {}).get('market', {}).get('max') or 0.0)
            if max_market_qty <= 0:
                for f in market.get('info', {}).get('filters', []):
                    if f.get('filterType') == 'MARKET_LOT_SIZE':
                        max_market_qty = float(f.get('maxQty', 0.0) or 0.0)
                        break

            try:
                formatted_amount = float(self.exchange.amount_to_precision(resolved_sym, contracts))
            except Exception:
                formatted_amount = contracts

            if formatted_amount <= 0:
                return {"success": True, "closed": False, "message": f"Jumlah kontrak tidak valid untuk ditutup ({contracts})."}

            # Auto-chunking if position exceeds Binance max market order size (prevents code -4005)
            if max_market_qty > 0 and formatted_amount > max_market_qty:
                # AUDIT FIX: read the exchange min qty so a tiny final remainder is merged
                # into the previous chunk instead of being sent alone and rejected, which
                # would leave a naked stub position behind.
                min_qty = float(market.get('limits', {}).get('amount', {}).get('min') or 0.0)
                if min_qty <= 0:
                    for f in market.get('info', {}).get('filters', []):
                        if f.get('filterType') in ('MARKET_LOT_SIZE', 'LOT_SIZE'):
                            min_qty = float(f.get('minQty', 0.0) or 0.0)
                            if min_qty > 0:
                                break
                remaining = formatted_amount
                chunks_executed = 0
                last_order = None
                while remaining > 0:
                    chunk = min(remaining, max_market_qty)
                    # Merge a sub-minimum remainder into this (final) chunk.
                    if 0 < (remaining - chunk) < min_qty:
                        chunk = remaining
                    try:
                        chunk_formatted = float(self.exchange.amount_to_precision(resolved_sym, chunk))
                    except Exception:
                        chunk_formatted = chunk
                    if chunk_formatted <= 0:
                        break
                    last_order = self._submit_order(
                        symbol=resolved_sym,
                        type='market',
                        side=close_side,
                        amount=chunk_formatted,
                        params={'reduceOnly': True}
                    )
                    chunks_executed += 1
                    remaining = round(remaining - chunk_formatted, 8)
                    time.sleep(0.1)

                # Confirm the position is actually flat; report honestly if a remainder is left.
                leftover = self.get_open_positions(symbol)
                still = abs(float(leftover[0].get("contracts", 0) or 0)) if leftover else 0.0
                if still > 0:
                    return {
                        "success": False,
                        "closed": False,
                        "remaining": still,
                        "order": last_order,
                        "message": f"Posisi {symbol} tertutup sebagian; SISA {still} masih terbuka (perlu tindakan)."
                    }
                return {
                    "success": True,
                    "closed": True,
                    "order": last_order,
                    "message": f"Posisi {symbol} ({pos['side']} {formatted_amount}) berhasil ditutup dalam {chunks_executed} market batch."
                }
            else:
                order = self._submit_order(
                    symbol=resolved_sym,
                    type='market',
                    side=close_side,
                    amount=formatted_amount,
                    params={'reduceOnly': True}
                )
                return {"success": True, "closed": True, "order": order, "message": f"Posisi {symbol} ({pos['side']} {formatted_amount}) berhasil ditutup pada harga pasar."}
        except Exception as e:
            print(f"[!] Error closing position on {symbol}: {e}")
            return {"success": False, "closed": False, "error": str(e)}

    def setup_symbol_settings(self, symbol: str, leverage: int = 5, margin_type: str = "ISOLATED"):
        """Configures leverage and margin type. Failures are logged (not swallowed) so a
        mis-sized order is never silently sent at the wrong leverage."""
        resolved_sym = self.resolve_symbol(symbol)
        try:
            self.exchange.set_leverage(leverage, resolved_sym)
        except Exception as e:
            print(f"[!] set_leverage failed on {resolved_sym} (target {leverage}x): {e}")

        try:
            self.exchange.set_margin_mode(margin_type, resolved_sym)
        except Exception as e:
            # -4046 = 'No need to change margin type' — benign, everything else is not.
            if "-4046" not in str(e):
                print(f"[!] set_margin_mode failed on {resolved_sym} ({margin_type}): {e}")

    def _sign_tradfi_agreement(self) -> bool:
        """Sign the TradFi-Perps agreement (one-off per key, raw signed POST).

        Classic USD-M: POST {fapi}/stock/contract. Portfolio-margin accounts:
        POST https://papi.binance.com/papi/v1/um/stock/contract. True on SUCCESS/200.
        """
        api_key = str(getattr(self.exchange, "apiKey", "") or "")
        secret = str(getattr(self.exchange, "secret", "") or "")
        if not api_key or not secret:
            return False

        def _post(base: str, path: str) -> bool:
            ts = int(time.time() * 1000)
            qs = f"recvWindow=60000&timestamp={ts}"
            sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
            url = f"{base}{path}?{qs}&signature={sig}"
            req = urllib.request.Request(url, data=b"", headers={"X-MBX-APIKEY": api_key}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    body = r.read().decode("utf-8", "replace")
                    return r.status == 200 and "SUCCESS" in body.upper()
            except Exception as e:
                logger.debug(f"TradFi sign {base}{path} failed: {e}")
                return False

        try:
            fapi_base = str((self.exchange.urls.get("api") or {}).get("fapiPrivate") or "https://fapi.binance.com/fapi/v1")
            if _post(fapi_base, "/stock/contract"):
                return True
            if not self.is_testnet and _post("https://papi.binance.com/papi/v1", "/um/stock/contract"):
                return True  # ponytail: PM probe — add explicit PM flag when account mode known
            return False
        except Exception as e:
            logger.debug(f"TradFi sign failed: {e}")
            return False

    def _submit_order(self, *args, **kwargs) -> Optional[Dict[str, Any]]:
        """Single choke point for EVERY order placed on the exchange.

        Logs each attempt (success or failure) to the append-only audit ledger, so no
        order path — current or future — can bypass the trail. Delegates to ccxt unchanged.
        """
        _retried = bool(kwargs.pop("_tradfi_retried", False))
        def _ledger_append(event, **f):
            try:
                from src.trade_ledger import ledger
                ledger.append(event, is_live=not self.is_testnet, **f)
            except Exception as e:
                logger.debug(f"Ledger append exception: {e}")

        _sym = kwargs.get("symbol") or (args[0] if args else None)
        _params = kwargs.get("params") or {}
        _ledger_append("order_attempt", symbol=_sym, type=kwargs.get("type"),
                       side=kwargs.get("side"), amount=kwargs.get("amount"),
                       price=kwargs.get("price"),
                       reduce_only=bool(_params.get("reduceOnly")),
                       stop_price=_params.get("stopPrice"))
        try:
            order = getattr(self.exchange, "create_order")(*args, **kwargs)
        except Exception as e:
            _ledger_append("order_rejected", symbol=_sym, side=kwargs.get("side"),
                           type=kwargs.get("type"), amount=kwargs.get("amount"), error=str(e))
            if "-4411" in str(e) and not _retried and getattr(config, "tradfi_auto_sign", True):
                _fp = str(getattr(self.exchange, "apiKey", "") or "")[-4:]
                print(f"[TradFi] -4411 on {_sym} (key ****{_fp}): signing agreement, retry once")
                if self._sign_tradfi_agreement():
                    _ledger_append("tradfi_agreement_signed", symbol=_sym, key_fp=f"****{_fp}")
                    return self._submit_order(*args, **kwargs, _tradfi_retried=True)
                _ledger_append("tradfi_sign_failed", symbol=_sym, key_fp=f"****{_fp}")
            raise
        try:
            _info = (order or {}).get("info") or {}
            _ledger_append("order_placed", symbol=_sym, side=kwargs.get("side"),
                           type=kwargs.get("type"), amount=kwargs.get("amount"),
                           price=kwargs.get("price"),
                           order_id=_info.get("orderId") or _info.get("algoId") or (order or {}).get("id"),
                           status=(order or {}).get("status"))
        except Exception as e:
            logger.debug(f"Order placed ledger note skipped: {e}")
        return order

    def place_limit_order(self, symbol: str, side: str, amount: float, price: float, reduce_only: bool = False) -> Optional[Dict[str, Any]]:
        """Places a limit order with precise price and quantity formatting, guaranteeing Binance min notional."""
        try:
            self._cache.clear()
            self.last_error = ""
            resolved_sym = self.resolve_symbol(symbol)
            if not self.exchange.markets:
                self.exchange.load_markets()
            market = self.exchange.market(resolved_sym)

            formatted_price = float(self.exchange.price_to_precision(resolved_sym, price))

            # Auto-calculate safe quantity meeting Binance min notional & amount precision BEFORE amount_to_precision
            safe_qty = amount
            if not reduce_only:
                min_cost = float(market.get('limits', {}).get('cost', {}).get('min') or config.min_notional_threshold)
                min_amount = float(market.get('limits', {}).get('amount', {}).get('min') or 0.0)
                step = float(market.get('precision', {}).get('amount') or 0.0)
                needed_qty = max(amount, min_amount, (min_cost * float(getattr(config, "min_notional_buffer", 1.02))) / formatted_price if formatted_price > 0 else 0.0)
                if step > 0:
                    import math
                    steps_needed = math.ceil(needed_qty / step)
                    safe_qty = steps_needed * step
                else:
                    safe_qty = needed_qty

            formatted_qty = float(self.exchange.amount_to_precision(resolved_sym, safe_qty))

            params = {}
            if reduce_only:
                params['reduceOnly'] = True

            order = self._submit_order(
                symbol=resolved_sym,
                type='limit',
                side=side.lower(),
                amount=formatted_qty,
                price=formatted_price,
                params=params
            )
            return order
        except Exception as e:
            self.last_error = str(e)
            print(f"[!] Error placing {side} limit order on {symbol} at {price}: {e}")
            return None

    def get_ticker(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetches current ticker data for a symbol (cached for 1s)."""
        now = time.time()
        ckey = f"ticker_{symbol}"
        cached = self._cache.get(ckey)
        if cached and (now - cached["t"] < 1.0):
            return cached["data"]
        try:
            resolved_sym = self.resolve_symbol(symbol)
            ticker = self.exchange.fetch_ticker(resolved_sym)
            self._cache[ckey] = {"t": now, "data": ticker}
            return ticker
        except Exception:
            return None

    def place_market_order(self, symbol: str, side: str, amount: float, reduce_only: bool = False) -> Optional[Dict[str, Any]]:
        """Places a market order with precise quantity formatting, guaranteeing Binance min notional."""
        try:
            self._cache.clear()
            self.last_error = ""
            resolved_sym = self.resolve_symbol(symbol)
            if not self.exchange.markets:
                self.exchange.load_markets()
            market = self.exchange.market(resolved_sym)

            safe_qty = amount
            if not reduce_only:
                min_cost = float(market.get('limits', {}).get('cost', {}).get('min') or config.min_notional_threshold)
                min_amount = float(market.get('limits', {}).get('amount', {}).get('min') or 0.0)
                step = float(market.get('precision', {}).get('amount') or 0.0)
                ticker = self.get_ticker(resolved_sym)
                ref_price = float((ticker.get('last') or ticker.get('close') or 0.0) if ticker else 0.0)
                if ref_price <= 0:
                    try:
                        ob = self.exchange.fetch_order_book(resolved_sym, limit=1)
                        ref_price = float(ob['bids'][0][0] if side.lower() == 'sell' else ob['asks'][0][0])
                    except Exception as e:
                        logger.debug(f"Order book fetch for ref price failed: {e}")
                needed_qty = max(amount, min_amount, (min_cost * float(getattr(config, "min_notional_buffer", 1.02))) / ref_price if ref_price > 0 else 0.0)
                if step > 0:
                    import math
                    steps_needed = math.ceil(needed_qty / step)
                    safe_qty = steps_needed * step
                else:
                    safe_qty = needed_qty

            try:
                formatted_qty = float(self.exchange.amount_to_precision(resolved_sym, safe_qty))
            except Exception:
                formatted_qty = safe_qty

            if formatted_qty <= 0:
                self.last_error = f"Calculated quantity is 0 or negative: {formatted_qty}"
                return None

            params = {}
            if reduce_only:
                params['reduceOnly'] = True

            order = self._submit_order(
                symbol=resolved_sym,
                type='market',
                side=side.lower(),
                amount=formatted_qty,
                params=params
            )
            return order
        except Exception as e:
            self.last_error = str(e)
            print(f"[!] Error placing {side} market order on {symbol} (amount: {amount}): {e}")
            return None

    def _raw_symbol(self, symbol: Optional[str]) -> Optional[str]:
        """Binance raw symbol id (e.g. 'XLMUSDT') for the raw-endpoint calls below."""
        if not symbol:
            return None
        try:
            return self.exchange.market(self.resolve_symbol(symbol)).get("id")
        except Exception:
            return None

    def get_conditional_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch open CONDITIONAL orders (STOP_MARKET / TAKE_PROFIT_MARKET).

        These live in a SEPARATE Binance endpoint from regular limit orders —
        fetch_open_orders never returns them, which is why stops 'disappeared'
        from the dashboard. This is the only truthful view of the crash net.

        On failure this raises a flag (self.conditional_orders_ok=False) instead of
        silently returning [] — an empty list must mean 'no stops', not 'fetch broke',
        otherwise de-duplication logic could conclude a protected position is naked.
        """
        try:
            params: Dict[str, Any] = {}
            raw = self._raw_symbol(symbol)
            if raw:
                params["symbol"] = raw
            # /fapi/v1/openAlgoOrders returns [] for CONDITIONAL (STOP_MARKET) orders.
            # allAlgoOrders is the only endpoint that lists them; filter live ones.
            res = self.exchange.fapiPrivateGetAllAlgoOrders(params) or []
            res = [o for o in res if str(o.get("algoStatus", "")).upper() in ("NEW", "WORKING")]
            self.conditional_orders_ok = True
            return res
        except Exception as e:
            self.conditional_orders_ok = False
            self.last_error = f"conditional-orders fetch failed: {e}"
            print(f"[!] Error fetching conditional orders: {e}")
            return []

    def cancel_conditional_orders(self, symbol: Optional[str] = None) -> int:
        """Cancel every open conditional order (algoId) on the symbol. Returns count."""
        n = 0
        for o in self.get_conditional_orders(symbol):
            algo_id = o.get("algoId")
            if not algo_id:
                continue
            try:
                self.exchange.fapiPrivateDeleteAlgoOrder({"algoId": algo_id})
                n += 1
            except Exception as e:
                print(f"[!] Error cancelling algo order {algo_id}: {e}")
        return n

    def _stop_tick_size(self, resolved_sym: str) -> float:
        """Venue price tick (step) for the symbol. NEVER a hardcoded literal.

        BUGFIX 2026-10-13: the old code read limits.price.min as the tick, but that
        field is the PRICE_FILTER minPrice (e.g. 39.86 for ETHUSDT), not the tickSize.
        The churn tolerance (tick x stop_replace_tick_tolerance) ballooned to ~$40 and
        set_protective_stop silently refused every trailing raise smaller than that.
        Order of truth: raw PRICE_FILTER tickSize -> ccxt precision decimals -> 0.0.
        limits.price.min is never used as a tick again."""
        try:
            m = self.exchange.market(resolved_sym)
            info = m.get("info") or {}
            for f in (info.get("filters") or []):
                if str(f.get("filterType") or "").upper() == "PRICE_FILTER" and f.get("tickSize") is not None:
                    try:
                        tick = float(f.get("tickSize"))
                        if tick > 0:
                            return tick
                    except (TypeError, ValueError):
                        pass
            prec = (m.get("precision", {}) or {}).get("price")
            if isinstance(prec, bool):
                return 0.0
            if isinstance(prec, int) and prec > 0:
                return float(10 ** (-prec))
            if isinstance(prec, float) and 0 < prec < 1:
                return float(prec)
            return 0.0
        except Exception:
            return 0.0

    def stop_within_tolerance(self, symbol: str, current_stop: float, new_stop: float) -> bool:
        """True when `new_stop` sits within a tick/epsilon of `current_stop`, i.e. re-placing
        the protective stop would be pure churn. Tolerance = exchangeInfo tick x the config
        knob, or a pct-of-price epsilon when no tick is published. No literals."""
        try:
            tick = self._stop_tick_size(self.resolve_symbol(symbol) or symbol)
            tol = tick * float(getattr(config, "stop_replace_tick_tolerance", 1.0) or 0.0)
            if tol <= 0:
                tol = abs(float(new_stop or 0.0)) * float(getattr(config, "stop_replace_price_epsilon_pct", 0.0005) or 0.0)
            return abs(float(new_stop) - float(current_stop)) <= tol
        except Exception:
            return False

    def set_protective_stop(self, symbol: str, stop_price: float, amount: Optional[float] = None, position_side: str = "LONG") -> bool:
        """Serialized wrapper around _place_protective_stop.

        RACE FIX: the main loop (cron, every 2 min) and safety_net (its own cron) both call
        this. Both read the existing-stop list before either had placed one, so both placed
        -> two identical STOP_MARKETs on one position (observed 2026-10-21, 370ms apart).
        The in-process churn guard cannot see the other process. A host-local flock serializes
        them. ponytail: flock is host-local, which is enough because both callers run on the Pi.
        """
        import fcntl
        with open(f"/tmp/latihan_protect_{os.getuid()}.lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                return self._place_protective_stop(symbol, stop_price, amount, position_side)
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def _place_protective_stop(self, symbol: str, stop_price: float, amount: Optional[float] = None, position_side: str = "LONG") -> bool:
        """
        Places (or refreshes) a HARD STOP-MARKET reduceOnly stop-loss directly on Binance.

        This is the safety net that protects the position even if the bot process
        crashes, the Raspberry Pi reboots, or the polling loop lags: the exchange
        itself closes the position when price crosses stop_price.

        Existing STOP orders on the symbol are cancelled first to avoid duplicates.
        Returns True if a fresh stop order was successfully placed.
        """
        try:
            resolved_sym = self.resolve_symbol(symbol)
            if not resolved_sym:
                return False
            sp = float(self.exchange.price_to_precision(resolved_sym, float(stop_price)))
            if sp <= 0:
                return False

            # FIX 2(a): STOP-CHURN GUARD. If a live STOP_MARKET for this symbol already sits
            # within the venue's own tick/epsilon of the requested price, do NOTHING — the stop
            # has not meaningfully moved. Tolerance comes from exchangeInfo (tick size), never a
            # literal. This is what kills the ~155 re-placements/15h churn.
            try:
                existing_stops = [o for o in self.get_conditional_orders(symbol)
                                  if "STOP" in (o.get("orderType") or "").upper()]
            except Exception:
                existing_stops = []
            # DEDUPE_FIRST: the churn guard below returns early, so duplicates that are
            # already on the book would survive forever (observed 2026-10-22: two qty-13
            # stops at 0.20757, and set_protective_stop kept saying True). Collapse to one
            # before the guard can short-circuit.
            if len(existing_stops) > 1:
                existing_stops.sort(key=lambda o: int(o.get("algoId") or o.get("orderId") or 0))
                for _d in existing_stops[1:]:
                    try:
                        self.exchange.fapiPrivateDeleteAlgoOrder({"algoId": _d.get("algoId") or _d.get("orderId")})
                    except Exception as de:
                        print(f"[!] Could not delete duplicate stop {_d.get('algoId')}: {de}")
                print(f"[!] De-duped {len(existing_stops) - 1} extra stop(s) on {symbol}, kept {existing_stops[0].get('algoId')}")
                existing_stops = existing_stops[:1]

            # QTY_GUARD: matching the trigger price is NOT enough. After a partial TP the
            # stop can still carry the pre-TP quantity; if it is SMALLER than the live
            # position the uncovered remainder is naked, and returning True here would keep
            # it naked forever (churn guard short-circuits every later call). Only
            # short-circuit when the stop actually covers the whole position. The live
            # position is the source of truth, never the `amount` argument: callers pass the
            # entry qty, which is stale the moment TP1 fills.
            _need = 0.0
            try:
                _p = self.get_open_positions(symbol)
                if _p:
                    _need = abs(float(_p[0].get("contracts", 0) or 0))
            except Exception:
                _need = 0.0
            if _need <= 0:
                _need = float(amount or 0.0)
            for _o in existing_stops:
                _cur = float(_o.get("triggerPrice") or _o.get("stopPrice") or 0.0)
                _have = float(_o.get("quantity") or _o.get("origQty") or 0.0)
                if _cur > 0 and self.stop_within_tolerance(symbol, _cur, sp) and _have >= _need * 0.999:
                    self._cache.clear()
                    return True  # protected at this level AND covering the whole position

            # AUDIT FIX: place the NEW stop FIRST, verify it, and only then cancel
            # the old conditional orders. The old order cancelled first, opening a window
            # with ZERO stop (and a permanent hole if the new order threw).

            # Resolve the quantity to protect. The LIVE position wins over `amount`:
            # after a partial TP the caller's qty is the pre-TP size, and a stop larger than
            # the position makes the churn guard above re-place forever.
            qty = float(amount or 0.0)
            try:
                pos = self.get_open_positions(symbol)
            except Exception:
                pos = None
            if pos:
                live_qty = abs(float(pos[0].get("contracts", 0) or 0))
                if live_qty > 0:
                    qty = live_qty
            elif qty <= 0:
                self.last_error = "No open position to protect"
                return False
            if qty <= 0:
                self.last_error = "No open position to protect"
                return False

            amt = float(self.exchange.amount_to_precision(resolved_sym, qty))
            if amt <= 0:
                self.last_error = f"Stop quantity rounded to 0 (raw={qty})"
                return False

            exit_side = "sell" if str(position_side).upper() == "LONG" else "buy"

            # FIX 2(b): CANCEL-BEFORE-REPLACE. Remove the existing protective stop(s) for this
            # symbol BEFORE placing the replacement, so the book never temporarily (or permanently)
            # holds two stops. The prior trigger is remembered so it can be re-armed if the new
            # placement fails — the position is never left naked. Invariant preserved: exactly ONE
            # protective STOP_MARKET per open position.
            prev_sp = 0.0
            for o in existing_stops:
                prev_sp = float(o.get("triggerPrice") or o.get("stopPrice") or prev_sp or 0.0)
                oid = o.get("algoId") or o.get("orderId")
                if oid is None:
                    continue
                try:
                    self.exchange.fapiPrivateDeleteAlgoOrder({"algoId": oid})
                except Exception as ce:
                    print(f"[!] Could not cancel stale algo order {oid}: {ce}")

            new_stop = None
            last_err = None
            for attempt in range(3):
                try:
                    new_stop = self._submit_order(
                        symbol=resolved_sym,
                        type="STOP_MARKET",
                        side=exit_side,
                        amount=amt,
                        params={"stopPrice": sp, "reduceOnly": True}
                    )
                    if new_stop:
                        break
                except Exception as oe:
                    last_err = oe
                    time.sleep(0.3)
            if not new_stop:
                # Re-arm the just-cancelled stop so the position is never left naked.
                if prev_sp > 0:
                    try:
                        self._submit_order(
                            symbol=resolved_sym, type="STOP_MARKET", side=exit_side, amount=amt,
                            params={"stopPrice": float(self.exchange.price_to_precision(resolved_sym, prev_sp)), "reduceOnly": True})
                    except Exception as e:
                        logger.debug(f"Re-arm stop failed: {e}")
                self.last_error = f"Failed to place protective stop: {last_err}"
                print(f"[!] Failed to place protective stop on {symbol} @ {stop_price}: {last_err}")
                return False

            # VERIFY the new order carries a real id/algoId — never report "protected" on a ghost.
            # (ccxt id is a STRING, the algo endpoint returns algoId as an INT; normalize to str.)
            _info = new_stop.get("info") or {}
            new_id = _info.get("algoId") or _info.get("orderId") or new_stop.get("id")
            new_id = str(new_id) if new_id is not None else None
            if new_id is None:
                self.last_error = "Protective stop placed without an id/algoId"
                return False

            # Post-condition: the protective stop MUST still be live. If the de-dup above (or any
            # race) removed it, re-place once so a position is never returned as "protected" while
            # naked on the exchange.
            # GHOST_RECHECK: allAlgoOrders lags the placement by ~1s, so a single
            # read right after placing reported the new stop as "missing" and the old
            # code re-placed it -> two identical STOP_MARKETs on one position
            # (observed 2026-10-22: algoId ...607 and ...621, both qty 13 @ 0.20757).
            # Poll before concluding it is gone, then de-dupe whatever is on the book.
            still = []
            for _ in range(4):
                try:
                    still = [x for x in self.get_conditional_orders(symbol)
                             if str(x.get("algoId") or x.get("orderId")) == new_id]
                except Exception:
                    still = [1]
                    break
                if still:
                    break
                time.sleep(0.4)
            if not still:
                try:
                    self._submit_order(
                        symbol=resolved_sym, type="STOP_MARKET", side=exit_side,
                        amount=amt, params={"stopPrice": sp, "reduceOnly": True})
                    print(f"[!] Protective stop was missing after de-dup — re-placed on {symbol} @ {sp}")
                except Exception as re_e:
                    self.last_error = f"Stop vanished and re-place failed: {re_e}"
                    return False
            # INVARIANT: exactly one STOP_MARKET per position. Any duplicate that slipped
            # through (retry, ghost re-place, second process) is deleted here, keeping the
            # oldest algoId. Runs inside the caller's flock.
            try:
                stops = [o for o in self.get_conditional_orders(symbol)
                         if "STOP" in (o.get("orderType") or "").upper()]
                if len(stops) > 1:
                    stops.sort(key=lambda o: int(o.get("algoId") or o.get("orderId") or 0))
                    for o in stops[1:]:
                        self.exchange.fapiPrivateDeleteAlgoOrder({"algoId": o.get("algoId") or o.get("orderId")})
                    print(f"[!] De-duped {len(stops) - 1} extra stop(s) on {symbol}, kept {stops[0].get('algoId')}")
            except Exception as de:
                print(f"[!] Stop de-dup failed on {symbol}: {de}")
            self._cache.clear()
            return True
        except Exception as e:
            self.last_error = str(e)
            print(f"[!] Failed to set protective stop on {symbol} @ {stop_price}: {e}")
            return False

    def execute_bracket_order(self, symbol: str, sizing: Dict[str, Any]) -> bool:
        """
        Executes Entry market order and immediately places attached Stop-Loss & Take-Profit.
        """
        try:
            resolved_sym = self.resolve_symbol(symbol)
            action = sizing["action"]  # LONG or SHORT
            entry_side = "buy" if action == "LONG" else "sell"
            exit_side = "sell" if action == "LONG" else "buy"
            amount = float(self.exchange.amount_to_precision(resolved_sym, sizing["quantity"]))
            sl_price = float(self.exchange.price_to_precision(resolved_sym, sizing["stop_loss"]))
            tp_price = float(self.exchange.price_to_precision(resolved_sym, sizing["take_profit_1"]))

            self.setup_symbol_settings(resolved_sym, sizing["leverage"], config.margin_type)

            print(f"[Binance Live] Executing {action} on {resolved_sym} - Amount: {amount}")
            entry_order = self._submit_order(
                symbol=resolved_sym,
                type='market',
                side=entry_side,
                amount=amount
            )
            print(f"[Binance Live] Entry Order Filled: {entry_order.get('id')}")

            # AUDIT FIX (critical safety): the entry is only valid if a stop-loss is LIVE on
            # the exchange. Place the SL first, retry, and if it still fails, immediately
            # market-close the just-opened position — never leave a naked live position.
            sl_ok = False
            last_sl_err = None
            for attempt in range(3):
                try:
                    self._submit_order(
                        symbol=resolved_sym,
                        type='STOP_MARKET',
                        side=exit_side,
                        amount=amount,
                        params={'stopPrice': sl_price, 'reduceOnly': True}
                    )
                    sl_ok = True
                    print(f"[Binance Live] Stop-Loss set at {sl_price}")
                    break
                except Exception as sl_err:
                    last_sl_err = sl_err
                    time.sleep(0.3)
            if not sl_ok:
                print(f"[!] CRITICAL: Stop-Loss FAILED on {resolved_sym} ({last_sl_err}). Emergency close.")
                try:
                    self.close_position(resolved_sym)
                except Exception as ce:
                    print(f"[!] Emergency close also failed on {resolved_sym}: {ce}")
                self.last_error = f"Stop-loss failed, position emergency-closed: {last_sl_err}"
                return False

            # Take Profit (non-fatal — the stop is the safety net; a missing TP only caps upside)
            try:
                self._submit_order(
                    symbol=resolved_sym,
                    type='TAKE_PROFIT_MARKET',
                    side=exit_side,
                    amount=amount,
                    params={'stopPrice': tp_price, 'reduceOnly': True}
                )
                print(f"[Binance Live] Take-Profit set at {tp_price}")
            except Exception as tp_err:
                print(f"[!] Failed to place Take-Profit order: {tp_err}")

            return True
        except Exception as e:
            print(f"[!] Error executing Binance bracket order: {e}")
            return False
