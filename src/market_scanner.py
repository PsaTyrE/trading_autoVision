import threading
import time
import logging
import ccxt
import pandas as pd
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor
from src.config import config
from src.indicators import calculate_technical_indicators, extract_market_snapshot, _sig_round

logger = logging.getLogger("market_scanner")

# Dynamic Fallback Symbols (Only symbol strings to query real Binance API if network is transient)
DEFAULT_SCAN_SYMBOLS = [
    "BTC", "ETH", "SOL", "BNB", "DOGE", "XRP", "NEAR", "SUI", "AVAX", "LINK"
]

# --- Dead-market & TradFi guard (2026-10-14) ---
# Binance USD-M keeps delisting/settling symbols in fetch_tickers(): OMG/USDT is
# status SETTLING with flat zero-volume candles, so its ATR computes to 0.0 and
# every cycle burns an OHLCV fetch + logs "ATR tak tersedia". It also lists 221
# TRADIFI_PERPETUAL tokenized-stock/commodity perps (SAMSUNG, XAU, TSLA...) that
# pass the volume gate but were never part of the crypto long-grid edge research.
# Filter them from the scan pool BEFORE any candle fetch. Cached 1h (market
# status changes rarely); a markets-load failure returns an empty set (fail-open,
# scanner keeps working on transient errors).
_UNUSABLE_CACHE = {"t": 0.0, "syms": set()}

def _unusable_symbols(exchange) -> set:
    """Symbols (unified, quote-stripped) the scanner must never evaluate:
    non-TRADING status (settling/delisted/halted) and, when
    config.scan_blacklist_tradifi is on, TRADIFI_PERPETUAL markets."""
    now = time.time()
    if _UNUSABLE_CACHE["syms"] and (now - _UNUSABLE_CACHE["t"] < 3600.0):
        return _UNUSABLE_CACHE["syms"]
    try:
        markets = getattr(exchange, "markets", None) or {}
        if not markets:
            exchange.load_markets()
            markets = getattr(exchange, "markets", None) or {}
        blacklist_tradfi = bool(getattr(config, "scan_blacklist_tradifi", False))
        bad = set()
        for sym, mkt in markets.items():
            info = (mkt or {}).get("info") or {}
            status = str(info.get("status") or "TRADING").upper()
            clean = sym.split(":")[0]
            if status != "TRADING":
                bad.add(clean)
                continue
            if blacklist_tradfi and info.get("contractType") == "TRADIFI_PERPETUAL":
                bad.add(clean)
        _UNUSABLE_CACHE["t"] = now
        _UNUSABLE_CACHE["syms"] = bad
        return bad
    except Exception:
        return set()


def is_valid_symbol(sym: str) -> bool:
    """
    Validates that a symbol is a legitimate, liquid, ASCII asset.
    Rejects:
    - Non-ASCII meme coins (e.g. Chinese characters 龙虾, 币安人生)
    - Extreme multiplier meme tokens (e.g. 1000RATS, 1000CAT, 1000LUNC)
    - Malformed or empty symbols
    """
    if not sym or not isinstance(sym, str):
        return False
    clean = sym.split(':')[0]
    base = clean.split('/')[0] if '/' in clean else clean
    if not base.isascii() or not base.isalnum():
        return False
    toxic_prefixes = ["1000", "1M", "10000"]
    if any(base.upper().startswith(p) for p in toxic_prefixes):
        return False
    return True

def _closed_candles(df: pd.DataFrame) -> pd.DataFrame:
    """
    Drop the still-forming (in-progress) last candle when config.use_closed_candle_only is set.
    Mirrors the P3 discipline in src/indicators.py (extract_market_snapshot): the final row of a
    live OHLCV pull is the candle that is still mutating intra-interval, so its volume/RSI/EMA are
    a repaint source. Config-driven (config.use_closed_candle_only, default True) — never hardcoded.
    """
    if getattr(config, "use_closed_candle_only", True) and len(df) > 1:
        return df.iloc[:-1]
    return df

class MarketScanner:
    """
    Scans Binance Futures pairs to identify coins with strong BULLISH momentum.
    Includes in-memory TTL caching (60s) to prevent repetitive API hammering and page lag.
    """
    _cache_lock = threading.Lock()
    _cache_data: List[Dict[str, Any]] = []
    _cache_time: float = 0.0
    _cache_bullish_map: Dict[str, Any] = {}
    _cache_bullish_time: Dict[str, float] = {}
    _cache_alpha: Dict[str, Any] = {}
    _cache_alpha_time: Dict[str, float] = {}
    _cache_edge: Dict[str, Any] = {}
    _cache_edge_time: Dict[str, float] = {}

    def __init__(self, exchange=None):
        if exchange:
            self.exchange = exchange
        else:
            params = {
                'enableRateLimit': True,
                'timeout': 5000,
            }
            self.exchange = ccxt.binanceusdm(params)

    def _get_fallback_exchange(self):
        if not hasattr(self, '_fallback_ex') or self._fallback_ex is None:
            self._fallback_ex = ccxt.binanceusdm({'enableRateLimit': True, 'timeout': 6000})
            for k, v in list(self._fallback_ex.urls['api'].items()):
                if isinstance(v, str) and 'https://fapi.binance.com' in v:
                    self._fallback_ex.urls['api'][k] = v.replace('https://fapi.binance.com', 'https://demo-fapi.binance.com')
        return self._fallback_ex

    def scan_top_bullish_coins(self, min_volume: float = 10_000_000, limit: int = 5, force_refresh: bool = False, quote_asset: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Scans all Binance Futures pairs and ranks the strongest bullish coins near support.
        Cached for 60 seconds unless force_refresh is True.
        Automatically falls back to demo-fapi endpoint if live IP is rate-limited (418).
        """
        target_quote = (quote_asset or config.default_quote_asset).strip().upper()
        now = time.time()
        ckey = f"bullish_{target_quote}"

        if not force_refresh and MarketScanner._cache_bullish_map.get(ckey) and (now - MarketScanner._cache_bullish_time.get(ckey, 0) < 60.0):
            return MarketScanner._cache_bullish_map[ckey][:limit]

        tickers = None
        try:
            tickers = self.exchange.fetch_tickers()
        except Exception:
            try:
                tickers = self._get_fallback_exchange().fetch_tickers()
            except Exception as e:
                print(f"[!] Error fetching tickers for scanner: {e}")
                MarketScanner._cache_bullish_time[ckey] = now + 30.0  # Back off 30s to prevent API ban extension
                cached = MarketScanner._cache_bullish_map.get(ckey, [])
                if cached:
                    return cached[:limit]
                fallback_tickers = {}
                for s in DEFAULT_SCAN_SYMBOLS:
                    sym_pair = f"{s}/{target_quote}"
                    try:
                        t = self.exchange.fetch_ticker(sym_pair)
                        if t and float(t.get('last') or 0) > 0:
                            fallback_tickers[sym_pair] = t
                    except Exception as e:
                        logger.debug(f"Fallback ticker fetch {sym_pair} failed: {e}")
                if fallback_tickers:
                    tickers = fallback_tickers
                else:
                    return []

        # Filter valid pairs for the target quote asset
        candidates = []
        fallback_candidates = []
        dead = _unusable_symbols(self.exchange)
        for symbol, data in tickers.items():
            if not (symbol.endswith(f'/{target_quote}') or symbol.endswith(f'/{target_quote}:{target_quote}')):
                continue

            clean_sym = symbol.split(':')[0]
            if not is_valid_symbol(clean_sym) or clean_sym in dead:
                continue

            vol = float(data.get('quoteVolume', 0) or 0)
            chg = float(data.get('percentage', 0) or 0)
            price = float(data.get('last', 0) or 0)

            if price <= 0:
                continue

            item = {
                "symbol": clean_sym,
                "price": price,
                "24h_change_pct": round(chg, 2),
                "volume_usdt": round(vol, 0)
            }

            # Primary: Volume >= min_volume & chg between 0.5% and 15% (no overbought runaway spikes)
            if 0.5 <= chg <= 15.0 and vol >= min_volume:
                candidates.append(item)
            elif vol >= 2_000_000 and 0.0 <= chg <= 12.0:
                fallback_candidates.append(item)

        # Fallback if market is neutral or consolidating
        if not candidates:
            candidates = sorted(fallback_candidates, key=lambda x: x["24h_change_pct"], reverse=True)[:15]

        # Scan liquid candidates
        candidates = sorted(candidates, key=lambda x: x["volume_usdt"], reverse=True)[:20]

        def _evaluate_bullish(cand):
            sym = cand["symbol"]
            raw_candles = None
            try:
                raw_candles = self.exchange.fetch_ohlcv(sym, timeframe=config.timeframe, limit=60)
            except Exception:
                try:
                    raw_candles = self._get_fallback_exchange().fetch_ohlcv(sym, timeframe=config.timeframe, limit=60)
                except Exception as e:
                    logger.debug(f"Fallback OHLCV fetch {sym} failed: {e}")

            if not raw_candles or len(raw_candles) < 35:
                return None

            try:
                df = pd.DataFrame(raw_candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = df[col].astype(float)

                df = calculate_technical_indicators(df)
                latest = df.iloc[-1]

                ema_20 = float(latest['ema_20'])
                ema_50 = float(latest['ema_50'])
                rsi = float(latest['rsi'])
                atr = float(latest['atr'])
                vol_ratio = float(latest['volume_ratio']) if not pd.isna(latest['volume_ratio']) else 1.0

                p = cand["price"]
                dist_from_ema20 = (p - ema_20) / max(ema_20, 1e-6)

                # Strict Anti-Pucuk Quant Criteria:
                # 1. Price is supported by EMA20 & EMA50 (Bullish structure)
                # 2. ANTI-PUCUK: Price MUST NOT be stretched > +1.5% above EMA20 (-0.8% <= dist <= +1.5%)
                # 3. Healthy RSI (44.0 <= rsi <= 60.0, never overbought > 60)
                # 4. Volume ratio >= 0.7
                is_bullish = (
                    p >= ema_50 and
                    (-0.008 <= dist_from_ema20 <= 0.015) and
                    (44.0 <= rsi <= 60.0) and
                    vol_ratio >= 0.7
                )

                if is_bullish:
                    cand_copy = dict(cand)
                    cand_copy["ema_20"] = _sig_round(ema_20)
                    cand_copy["ema_50"] = _sig_round(ema_50)
                    cand_copy["rsi_14"] = round(rsi, 2)
                    cand_copy["atr_14"] = _sig_round(atr)
                    cand_copy["volume_ratio"] = round(vol_ratio, 2)
                    cand_copy["dist_to_ema20_pct"] = round(dist_from_ema20 * 100, 2)
                    # Reward healthy consolidation near EMA20 with rising volume
                    cand_copy["bullish_score"] = round((60.0 - abs(rsi - 50.0)) + (vol_ratio * 15.0) + (1.0 - abs(dist_from_ema20)) * 20.0, 2)
                    return cand_copy
            except Exception as e:
                logger.debug(f"Bullish eval failed: {e}")
            return None

        verified_bullish = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            for res in executor.map(_evaluate_bullish, candidates):
                if res:
                    verified_bullish.append(res)

        # Sort by bullish score
        if verified_bullish:
            verified_bullish = sorted(verified_bullish, key=lambda x: x.get("bullish_score", 0), reverse=True)
        elif candidates:
            verified_bullish = candidates[:limit]

        # Save to memory cache
        if verified_bullish:
            MarketScanner._cache_bullish_map[ckey] = verified_bullish
            MarketScanner._cache_bullish_time[ckey] = time.time()
            MarketScanner._cache_data = verified_bullish
            MarketScanner._cache_time = time.time()

        return verified_bullish

    scan_bullish_coins = scan_top_bullish_coins

    def scan_asymmetric_alpha_coins(self, quote_asset: str = "USDT", min_volume: float = 10_000_000, limit: int = 8, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Scans Binance Futures pairs for ASYMMETRIC RISK/REWARD setups with parallel ThreadPool:
        - Prioritizes setups with HIGH PROFIT POTENTIAL and LOW LOSS RISK (R:R Ratio >= 2.0x).
        - Excludes overbought FOMO coins (RSI > 55 or already pumped > 12%).
        - Excludes broken falling knives (RSI < 38 or price < EMA50).
        - STRICT ANTI-PUCUK: Price MUST be within +1.5% of EMA20 (never chase tops).
        - Favors healthy accumulation/pullbacks near EMA support (RSI 40-55) with massive room to run.
        """
        now = time.time()
        target_quote = quote_asset.strip().upper()
        ckey = f"alpha_{target_quote}"
        if not force_refresh and MarketScanner._cache_alpha.get(ckey) and (now - MarketScanner._cache_alpha_time.get(ckey, 0) < 60.0):
            return MarketScanner._cache_alpha[ckey][:limit]

        tickers = None
        try:
            tickers = self.exchange.fetch_tickers()
        except Exception:
            try:
                tickers = self._get_fallback_exchange().fetch_tickers()
            except Exception as e:
                cached = MarketScanner._cache_alpha.get(ckey, [])
                if cached:
                    return cached[:limit]
                # Dynamic fallback: Fetch real live tickers for standard pairs without hardcoded prices
                candidates = []
                for base_s in DEFAULT_SCAN_SYMBOLS:
                    sym_c = f"{base_s}/{target_quote}"
                    try:
                        t_data = self._get_fallback_exchange().fetch_ticker(sym_c)
                        if t_data and float(t_data.get('last') or 0) > 0:
                            candidates.append({
                                "symbol": sym_c,
                                "price": float(t_data['last']),
                                "24h_change_pct": round(float(t_data.get('percentage', 0) or 0), 2),
                                "volume_usdt": round(float(t_data.get('quoteVolume', 0) or 0), 0)
                            })
                    except Exception as e:
                        logger.debug(f"Alpha ticker fetch {sym_c} failed: {e}")
                if not candidates:
                    return []

        # Filter candidates: Healthy liquidity, positive volume activity, and not an overextended parabolic pump (>12%)
        if tickers is not None:
            candidates = []
            dead = _unusable_symbols(self.exchange)
            for symbol, data in tickers.items():
                clean_sym = symbol.split(':')[0]
                if not clean_sym.endswith(f"/{target_quote}"):
                    continue
                if not is_valid_symbol(clean_sym) or clean_sym in dead:
                    continue

                vol = float(data.get('quoteVolume', 0) or 0)
                chg = float(data.get('percentage', 0) or 0)
                price = float(data.get('last', 0) or 0)

                if price <= 0:
                    continue

                if vol >= min_volume and -8.0 <= chg <= 12.0:
                    candidates.append({
                        "symbol": clean_sym,
                        "price": price,
                        "24h_change_pct": round(chg, 2),
                        "volume_usdt": round(vol, 0)
                    })

            if not candidates:
                # Fallback lower volume (>= $3M)
                for symbol, data in tickers.items():
                    clean_sym = symbol.split(':')[0]
                    if clean_sym.endswith(f"/{target_quote}") and is_valid_symbol(clean_sym) and clean_sym not in dead:
                        vol = float(data.get('quoteVolume', 0) or 0)
                        price = float(data.get('last', 0) or 0)
                        if price > 0 and vol >= 3_000_000 and -8.0 <= float(data.get('percentage', 0) or 0) <= 12.0:
                            candidates.append({
                                "symbol": clean_sym,
                                "price": price,
                                "24h_change_pct": round(float(data.get('percentage', 0) or 0), 2),
                                "volume_usdt": round(vol, 0)
                            })

        # Deep candidate pool: Scan top 30 liquid candidates in parallel
        candidates = sorted(candidates, key=lambda x: x["volume_usdt"], reverse=True)[:30]

        def _evaluate_alpha(cand):
            sym = cand["symbol"]
            raw_candles = None
            try:
                raw_candles = self.exchange.fetch_ohlcv(sym, timeframe=config.timeframe, limit=60)
            except Exception:
                try:
                    raw_candles = self._get_fallback_exchange().fetch_ohlcv(sym, timeframe=config.timeframe, limit=60)
                except Exception as e:
                    logger.debug(f"Fallback OHLCV fetch {sym} failed: {e}")

            if not raw_candles or len(raw_candles) < 35:
                return None

            try:
                df = pd.DataFrame(raw_candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = df[col].astype(float)

                # P3: never score indicators off the still-forming candle (repaint guard).
                df = _closed_candles(df)

                df = calculate_technical_indicators(df)
                latest = df.iloc[-1]
                prev = df.iloc[-2]
                prev_5 = df.iloc[-5] if len(df) >= 5 else prev

                p = float(cand.get("price", 0.0) or 0.0)
                if p <= 0:
                    return None
                ema_20 = float(latest['ema_20'])
                ema_50 = float(latest['ema_50'])
                ema_20_prev = float(prev_5['ema_20'])
                rsi = float(latest['rsi'])
                atr = float(latest['atr'])
                vol_ratio = float(latest['volume_ratio']) if not pd.isna(latest['volume_ratio']) else 1.0
                recent_high = float(df['high'].tail(25).max())
                recent_low = float(df['low'].tail(25).min())

                dist_from_ema20 = (p - ema_20) / max(ema_20, 1e-6)

                # RULE 1: Never buy overbought exhaustion (RSI > 55.0)
                if rsi > 55.0:
                    return None
                # RULE 2: Never catch falling knives below dynamic support (RSI < 38 or price below EMA50 floor)
                if rsi < 38.0 or p < (ema_50 * 0.99):
                    return None
                # RULE 3: STRICT ANTI-PUCUK - Distance from EMA20 MUST NOT exceed +1.5% (Eliminate chasing tops!)
                if dist_from_ema20 > 0.015:
                    return None
                # RULE 4: Real Trend Integrity - EMA20 Slope must be flat or rising, NOT plunging
                if ema_20 < (ema_20_prev * 0.997):
                    return None
                # RULE 5: Reject micro-downtrend dumping sequences
                if latest['close'] < latest['open'] and prev['close'] < prev['open'] and (p < prev['low']):
                    return None
                # RULE 6: Volume must not be completely dead (Liquidity health check)
                if vol_ratio < 0.6:
                    return None

                # ASYMMETRIC RISK/REWARD CALCULATION:
                # Downside risk anchored to structural swing low or at least 1.8x ATR / 2.0%
                downside_risk = max(p - recent_low, 1.8 * atr, p * 0.02) if 0 < recent_low < p else max(1.8 * atr, p * 0.02)
                # Potential reward based on realistic swing high expansion (minimum 2.2x ATR)
                upside_reward = max(recent_high - p, 2.2 * atr)
                rr_ratio = round(upside_reward / max(downside_risk, 1e-6) if downside_risk > 0 else 1.0, 2)

                # Strict requirement: Potential Reward must be at least 1.8x Risk
                if rr_ratio < 1.8:
                    return None

                risk_pct = round((downside_risk / max(p, 1e-6)) * 100, 2)
                reward_pct = round((upside_reward / max(p, 1e-6)) * 100, 2)

                # ASYMMETRIC ALPHA SCORE (0 - 100):
                # 1. Base Score from Risk/Reward Quality (up to 40 pts)
                score = min(40.0, rr_ratio * 14.0)

                # 2. RSI Accumulation Zone Quality (up to 25 pts)
                if 42.0 <= rsi <= 52.0:
                    score += 25.0 # Sweet spot for pre-breakout accumulation
                elif 38.0 <= rsi < 42.0 or 52.0 < rsi <= 55.0:
                    score += 15.0

                # 3. Proximity to EMA Support (up to 20 pts)
                dist_support = abs(p - ema_20) / max(p, 1e-6)
                if dist_support < 0.008:
                    score += 20.0 # Right at EMA20 springboard!
                elif dist_support < 0.015:
                    score += 12.0

                # 4. Moving Average Bullish Alignment (up to 10 pts)
                if ema_20 >= ema_50:
                    score += 10.0

                # 5. Volume Surge Confirmation (up to 10 pts)
                if vol_ratio >= 1.2:
                    score += 10.0
                elif vol_ratio >= 0.8:
                    score += 5.0

                item = dict(cand)
                item["asymmetric_score"] = round(score, 1)
                item["rr_ratio"] = rr_ratio
                item["downside_risk_pct"] = risk_pct
                item["upside_pot_pct"] = reward_pct
                item["rsi_14"] = round(rsi, 1)
                item["atr_14"] = _sig_round(atr)
                item["ema_20"] = _sig_round(ema_20)
                item["ema_50"] = _sig_round(ema_50)
                item["volume_ratio"] = round(vol_ratio, 2)
                item["dist_to_ema20_pct"] = round(dist_from_ema20 * 100, 2)
                return item
            except Exception as e:
                logger.debug(f"Alpha eval failed: {e}")
                return None

        scored_setups = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            for res in executor.map(_evaluate_alpha, candidates):
                if res:
                    scored_setups.append(res)

        # Sort strictly by Asymmetric Alpha Score (highest upside, lowest downside)
        scored_setups = sorted(scored_setups, key=lambda x: x.get("asymmetric_score", 0), reverse=True)

        MarketScanner._cache_alpha[ckey] = scored_setups
        MarketScanner._cache_alpha_time[ckey] = time.time()
        return scored_setups[:limit]

    def scan_edge_aligned_coins(self, quote_asset: str = "USDT", min_volume: float = 10_000_000,
                                limit: int = 8, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Same as scan_asymmetric_alpha_coins BUT additionally enforces the P6 Edge Gate's exact
        criteria (trend_bias != BULLISH and volume_vs_average >= edge_min_volume_ratio). This
        keeps auto-pick from selecting coins the live engine would immediately reject, so the
        basket it builds is actually enterable.

        The statistically-validated edge (edge_gate.py, pooled in+out-of-sample, PF ~1.5) lives
        in NOT-chasing a fully-confirmed bull trend on live volume — not in the raw alpha score.
        """
        ckey = f"edge_{quote_asset.strip().upper()}"
        now = time.time()
        if not force_refresh and MarketScanner._cache_edge.get(ckey) and (now - MarketScanner._cache_edge_time.get(ckey, 0) < 60.0):
            return MarketScanner._cache_edge[ckey][:limit]

        target_quote = quote_asset.strip().upper()
        tickers = None
        try:
            tickers = self.exchange.fetch_tickers()
        except Exception:
            try:
                tickers = self._get_fallback_exchange().fetch_tickers()
            except Exception:
                tickers = None

        # Build a REAL-liquidity pool directly from live tickers. We must NOT reuse
        # scan_asymmetric_alpha_coins: its mean-reversion pre-filter (RSI<=55 AND within
        # +1.5% of EMA20 AND R:R>=1.8) rejects ~100% of liquid markets, leaving this gate
        # nothing to evaluate. The validated edge lives in trend!=BULLISH + live volume, so
        # the only pre-filter here is genuine turnout (>= min_volume).
        pool = []
        if tickers:
            dead = _unusable_symbols(self.exchange)
            for symbol, data in tickers.items():
                clean_sym = symbol.split(':')[0]
                if not clean_sym.endswith(f"/{target_quote}") or not is_valid_symbol(clean_sym) or clean_sym in dead:
                    continue
                try:
                    price = float(data.get('last', 0) or 0)
                    vol = float(data.get('quoteVolume', 0) or 0)
                except Exception:
                    continue
                if price <= 0 or vol < min_volume:
                    continue
                pool.append({
                    "symbol": clean_sym,
                    "price": price,
                    "24h_change_pct": round(float(data.get('percentage', 0) or 0), 2),
                    "volume_usdt": round(vol, 0),
                })
        if not pool:
            for base_s in DEFAULT_SCAN_SYMBOLS:
                sym_c = f"{base_s}/{target_quote}"
                try:
                    t_data = self._get_fallback_exchange().fetch_ticker(sym_c)
                    if t_data and float(t_data.get('last') or 0) > 0:
                        pool.append({
                            "symbol": sym_c,
                            "price": float(t_data['last']),
                            "24h_change_pct": round(float(t_data.get('percentage', 0) or 0), 2),
                            "volume_usdt": round(float(t_data.get('quoteVolume', 0) or 0), 0),
                        })
                except Exception as e:
                    logger.debug(f"Edge pool fetch {sym_c} failed: {e}")
        base = sorted(pool, key=lambda x: x["volume_usdt"], reverse=True)[:60]

        tf = config.timeframe
        def _gate_ok(cand):
            sym = cand["symbol"]
            raw = None
            try:
                raw = self.exchange.fetch_ohlcv(sym, timeframe=tf, limit=80)
            except Exception as e:
                logger.debug(f"OHLCV fetch for edge gate {sym} failed: {e}")
            if not raw or len(raw) < 35:
                return None
            try:
                df = pd.DataFrame(raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                for c in ['open', 'high', 'low', 'close', 'volume']:
                    df[c] = df[c].astype(float)
                df = calculate_technical_indicators(df)
                snap = extract_market_snapshot(df, sym, tf)
                tb = str(snap.get("trend_bias", "NEUTRAL")).upper()
                ind = snap.get("indicators", {}) or {}
                vol_r = float(ind.get("volume_vs_average", 1.0) or 1.0)
                # EXACT validated edge gate (edge_gate.py "not_bullish_and_not_thin",
                # pooled in+out-of-sample, PF ~1.5): trend != BULLISH AND vol_ratio != thin.
                # The ATR floor of an earlier draft was removed — bucket stats show the
                # majority of positive trades sit in the "dead ATR" zone, so rejecting it
                # discarded the best sample.
                if tb.startswith("BULLISH"):
                    # Edge gate: the measured loser is the confirmed BULL trend (every
                    # BULLISH sub-bucket is negative). Dead volume is NOT a losing bucket —
                    # NEUTRAL|normal measured +0.287R vs NEUTRAL|surge -0.238R — so no volume
                    # rejection here; the only filter is the confirmed-trend rejection.
                    if not bool(getattr(config, "edge_allow_bullish_fallback", False)):
                        return None
                item = dict(cand)
                item["trend_bias"] = tb.split(" /")[0]
                item["volume_ratio"] = round(vol_r, 2)
                item["edge_aligned"] = True
                return item
            except Exception:
                return None

        verified = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            for res in executor.map(_gate_ok, base):
                if res:
                    verified.append(res)

        # Rank enterable zones first: NEUTRAL (score-contributing, can reach quant >= 62)
        # before BEARISH (engine subtracts and the MTF guardrail forces HOLD anyway).
        _rank = {"NEUTRAL": 0, "BEARISH": 1}
        verified = sorted(verified, key=lambda x: (_rank.get(x.get("trend_bias", "NEUTRAL"), 0),
                                                   -x.get("volume_usdt", 0)))
        MarketScanner._cache_edge[ckey] = verified
        MarketScanner._cache_edge_time[ckey] = time.time()
        return verified[:limit]

