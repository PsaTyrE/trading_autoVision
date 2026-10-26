import ccxt
import pandas as pd
from typing import Optional
from src.config import config
from src.indicators import calculate_technical_indicators, extract_market_snapshot, _sig_round

class MarketDataFeeder:
    def __init__(self, exchange=None, is_testnet: bool = False):
        self.is_testnet = is_testnet or (config.trading_mode == 'demo')
        if exchange:
            self.exchange = exchange
        else:
            exchange_params = {
                'enableRateLimit': True,
                'timeout': 6000,
            }
            self.exchange = ccxt.binanceusdm(exchange_params)
            if self.is_testnet:
                self._route_to_demo(self.exchange)
        self._fallback_exchange = None

    def _route_to_demo(self, ex):
        for k, v in list(ex.urls['api'].items()):
            if isinstance(v, str) and 'https://fapi.binance.com' in v:
                ex.urls['api'][k] = v.replace('https://fapi.binance.com', 'https://demo-fapi.binance.com')

    def _get_fallback_exchange(self):
        if self._fallback_exchange is None:
            self._fallback_exchange = ccxt.binanceusdm({'enableRateLimit': True, 'timeout': 6000})
            self._route_to_demo(self._fallback_exchange)
        return self._fallback_exchange

    def fetch_ohlcv(self, symbol: str, timeframe: Optional[str] = None, limit: int = 150) -> Optional[pd.DataFrame]:
        """
        Fetches historical candlestick (OHLCV) data from Binance Futures.
        Automatically falls back to demo-fapi endpoint if primary endpoint experiences an IP rate limit (HTTP 418).
        """
        if not symbol or not isinstance(symbol, str) or not symbol.strip():
            return None

        tf = timeframe or config.timeframe
        clean_sym = symbol.strip()
        try:
            raw_candles = self.exchange.fetch_ohlcv(clean_sym, timeframe=tf, limit=limit)
            if raw_candles:
                return self._parse_candles(raw_candles)
        except Exception as e:
            # Fallback to demo-fapi if live WAF rate-limited (HTTP 418) or connection dropped
            try:
                raw_candles = self._get_fallback_exchange().fetch_ohlcv(clean_sym, timeframe=tf, limit=limit)
                if raw_candles:
                    return self._parse_candles(raw_candles)
            except Exception as fe:
                logger.debug(f"Fallback OHLCV fetch failed: {fe}")
            print(f"[!] Error fetching OHLCV for {symbol}: {e}")
            return None
        return None

    def _parse_candles(self, raw_candles) -> pd.DataFrame:
        df = pd.DataFrame(raw_candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
        return df

    def get_market_analysis_context(self, symbol: str, timeframe: Optional[str] = None) -> Optional[dict]:
        """
        Fetches OHLCV, computes all indicators, and formats the snapshot for AI and Quant Engine.
        Includes Multi-Timeframe (MTF) 1-Hour Trend Confirmation.
        """
        tf = timeframe or config.timeframe
        df = self.fetch_ohlcv(symbol, timeframe=tf, limit=150)
        if df is None or len(df) < 40:
            return None

        df_with_indicators = calculate_technical_indicators(df)
        snapshot = extract_market_snapshot(df_with_indicators, symbol=symbol, timeframe=tf)

        # Multi-Timeframe (1H) Macro Confirmation
        try:
            df_1h = self.fetch_ohlcv(symbol, timeframe='1h', limit=50)
            if df_1h is not None and len(df_1h) >= 30:
                # AUDIT FIX: drop the still-forming 1h candle so the macro regime cannot
                # repaint intra-hour (the primary path already honours use_closed_candle_only).
                if bool(getattr(config, "use_closed_candle_only", True)) and len(df_1h) > 30:
                    df_1h = df_1h.iloc[:-1]
                df_1h_ind = calculate_technical_indicators(df_1h)
                latest_1h = df_1h_ind.iloc[-1]
                p_1h = float(latest_1h['close'])
                ema20_1h = float(latest_1h['ema_20'])
                ema50_1h = float(latest_1h['ema_50'])
                rsi_1h = float(latest_1h['rsi'])
                
                macro_trend = "BULLISH" if (p_1h >= ema20_1h >= ema50_1h and rsi_1h >= 46) else (
                    "BEARISH" if (p_1h < ema20_1h and ema20_1h < ema50_1h and rsi_1h <= 48) else "NEUTRAL"
                )
                snapshot["mtf_1h"] = {
                    "trend": macro_trend,
                    "ema_20": _sig_round(ema20_1h),
                    "ema_50": _sig_round(ema50_1h),
                    "rsi": round(rsi_1h, 2)
                }
            else:
                snapshot["mtf_1h"] = None
        except Exception:
            snapshot["mtf_1h"] = None

        return snapshot

    def fetch_current_price(self, symbol: str) -> float:
        """
        Gets current ticker price with resilient fallback.
        """
        if not symbol or not symbol.strip():
            return 0.0
        clean_sym = symbol.strip()
        try:
            ticker = self.exchange.fetch_ticker(clean_sym)
            return float(ticker.get('last') or ticker.get('close') or 0.0)
        except Exception:
            try:
                ticker = self._get_fallback_exchange().fetch_ticker(clean_sym)
                return float(ticker.get('last') or ticker.get('close') or 0.0)
            except Exception:
                return 0.0
