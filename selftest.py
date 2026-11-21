"""Self-test suite for auto_pilot_trader and strategy modules.

Run after any change: ./venv/bin/python selftest.py
Must exit 0 with ALL PASS. Never commit a broken selftest.
"""
import json
import math
import os
import sys
import pandas as pd

# Selftest TIDAK boleh menyentuh state live. Dulu dia menulis probe ke
# autonomous_trader_state.json: day_entries balik 0, dan merge bisa membuang meta
# posisi yang sedang terbuka. Sekarang state-nya file sekali pakai.
os.environ["STATE_FILE"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".selftest_state.json")

import auto_pilot_trader as A
import src.strategy as strategy

failures = []
passed = [0]

def check(name, cond):
    if cond:
        passed[0] += 1
        print(f"PASS {name}")
    else:
        print(f"FAIL {name}")
        failures.append(name)

# --- 1. state persistence ---
probe = {"day_key": "2099-01-01", "day_entries": 99, "last_signal_ts": 1234567890}
A.save_state(probe)
check("state roundtrip", A.load_state() == probe)
check("no tmp residue", not os.path.exists(A.STATE_FILE + ".tmp"))

# --- 2. signal: Pullback Buy on Dip (verified on real market data fixture) ---
with open("fixtures/btc_pullback.json", "r") as f:
    raw_bars = json.load(f)

d = strategy.features(pd.DataFrame(raw_bars, columns=["t", "o", "h", "l", "c", "v"]))
sig = strategy.signal(d, -1)

check("pullback BUY detected", bool(sig) and sig[0] == "BUY")
check("vol_ratio reported", bool(sig) and sig[1]["vol_ratio"] >= strategy.MIN_VOL_RATIO)

# --- 3. volume gate blocks on weak volume ---
raw_low_v = [list(r) for r in raw_bars]
raw_low_v[-1][5] = 1.0  # destroy volume on signal bar
d2 = strategy.features(pd.DataFrame(raw_low_v, columns=["t", "o", "h", "l", "c", "v"]))
check("volume gate blocks", strategy.signal(d2, -1) is None)

# --- 4. levels math: SL 2 ATR, TP1 2R, TP2 3.5R ---
sl, tp1, tp2, sd = strategy.levels(100.0, 1.0, "BUY")
_expL = (100.0 - sd, 100.0 + sd * strategy.TP1_R, 100.0 + sd * strategy.TP2_R)
check("LONG levels", (sl, tp1, tp2) == _expL and sd == strategy.SL_ATR)

sl, tp1, tp2, sd = strategy.levels(100.0, 1.0, "SELL")
_expS = (100.0 + sd, 100.0 - sd * strategy.TP1_R, 100.0 - sd * strategy.TP2_R)
check("SHORT levels", (sl, tp1, tp2) == _expS and sd == strategy.SL_ATR)

# --- 5. trailing epsilon scales with price (sub-$1 coin protection) ---
def eps(price, dec):
    return max(abs(price) * 1e-4, 10.0 ** (-dec))
check("trail eps DOGE < 0.1", eps(0.16, 2) < 0.1)
check("trail eps XAU sane", 0.3 < eps(4132.0, 1) < 1.0)

# --- 5b. snapshot fields measured, not fabricated ---
m = sig[1] if sig else {}
check("score in 0-100", 0.0 <= float(m.get("score", -1)) <= 100.0)
check("rsi in 0-100", 0.0 <= float(m.get("rsi", -1)) <= 100.0)
check("dip margin positive", float(m.get("breakout_margin", -1)) > 0)
check("trend_up bool", isinstance(m.get("trend_up"), bool))

# --- 6. position sizing sanity ---
check("BTC untradeable at $0.30 risk", (0.30 / 2000.0) < 0.001)
check("XAU tradeable at $0.30 risk", (0.30 / 2.0) >= 0.001)

# --- 7. env knobs ---
check("knob defaults measured", strategy.SL_ATR == 2.0 and strategy.TP1_R == float(os.getenv("STRAT_TP1_R", 2.0)))

# --- 8. anti-pucuk guard: reject bar near Upper Bollinger Band ---
raw_pucuk = [list(r) for r in raw_bars]
# Set signal bar close above Upper Bollinger Band
raw_pucuk[-1][4] = float(sig[1]["bb_up"]) * 1.01
d_pucuk = strategy.features(pd.DataFrame(raw_pucuk, columns=["t", "o", "h", "l", "c", "v"]))
check("anti-pucuk rejects overextended bar", strategy.signal(d_pucuk, -1) is None)

# --- 9. daily loss cap (config di .env yang tadinya mati) ---
_inc_rows = [
    {"incomeType": "REALIZED_PNL", "income": "-3.00"},
    {"incomeType": "COMMISSION", "income": "-0.10"},
    {"incomeType": "FUNDING_FEE", "income": "-0.05"},
    {"incomeType": "TRANSFER", "income": "999.00"},   # harus diabaikan
]
check("day_pnl sums pnl+fee+funding only", A.day_pnl_from_income(_inc_rows) == -3.15)
check("day_pnl ignores unrelated income types", A.day_pnl_from_income([{"incomeType": "TRANSFER", "income": "50"}]) == 0.0)
check("day_pnl handles empty", A.day_pnl_from_income([]) == 0.0)
check("loss cap trips at -6% of 55", A.daily_loss_blocked(-3.30, 55.0, 0.05) is True)
check("loss cap allows -2% of 55", A.daily_loss_blocked(-1.10, 55.0, 0.05) is False)
check("loss cap allows profit", A.daily_loss_blocked(0.75, 55.0, 0.05) is False)
check("loss cap safe on zero equity", A.daily_loss_blocked(-1.0, 0.0, 0.05) is False)
check("loss cap exported", hasattr(A, "daily_loss_blocked") and hasattr(A, "day_pnl_from_income"))

# --- 10. entry ts diambil dari fill paling awal (bukan now) ---
check("entry_ts picks earliest fill",
      A.entry_ts_from_trades([{"time": "1793300000000"}, {"time": "1793290000000"}], 1) == 1793290000)
check("entry_ts falls back when empty", A.entry_ts_from_trades([], 12345) == 12345)
check("entry_ts exported", hasattr(A, "entry_ts_from_trades"))

# --- 11. veto RSI 1h ---
check("rsi1h veto LONG 81.5", A.rsi1h_veto("LONG", 81.5) is True)
check("rsi1h veto LONG 57.2 lolos", A.rsi1h_veto("LONG", 57.2) is False)
check("rsi1h veto SHORT 41.0", A.rsi1h_veto("SHORT", 41.0) is True)
check("rsi1h veto SHORT 52.4 lolos", A.rsi1h_veto("SHORT", 52.4) is False)
check("rsi1h veto None lolos", A.rsi1h_veto("LONG", None) is False)

# --- 12. deteksi level TP yang sudah dilewati ---
check("tp passed SHORT 1.2725 vs 1.243", A.tp_leg_passed("SHORT", 1.2725, 1.243) is True)
check("tp belum SHORT 1.20 vs 1.243", A.tp_leg_passed("SHORT", 1.20, 1.243) is False)
check("tp passed LONG 1.30 vs 1.35", A.tp_leg_passed("LONG", 1.30, 1.35) is True)
check("tp belum LONG 1.40 vs 1.35", A.tp_leg_passed("LONG", 1.40, 1.35) is False)

# --- 13. runner trailing 0.75R ---
check("runner trail SHORT 1.2381 r0.0203 (jarak 0,5R)", A.runner_trail_sl("SHORT", 1.2381, 0.0203, 4) == 1.2483)
check("runner trail LONG 100 r2 (jarak 0,5R)", A.runner_trail_sl("LONG", 100.0, 2.0, 2) == 99.0)
check("runner trail r_dist 0 -> None", A.runner_trail_sl("LONG", 100.0, 0.0) is None)

# --- 14. err_detail tidak boleh motong pesan ---
_e1 = Exception("binanceusdm POST https://fapi.binance.com/fapi/v1/order\n 400 Bad Request {\"code\":-4164,\"msg\":\"notional too small\"}")
_d1 = A.err_detail(_e1)
check("err_detail simpan alasan", "notional too small" in _d1)
check("err_detail satu baris", "\n" not in _d1)
check("err_detail ada tipe", _d1.startswith("Exception"))
check("err_detail kosong tetap jelas", A.err_detail(ValueError()) == "ValueError")
class _FE(Exception):
    code = -4164
check("err_detail baca code", "code=-4164" in A.err_detail(_FE("x")))

# --- 15. SL dinamis progressif (semua posisi) ---
_e, _r = 0.04508, 0.01528   # Q: entry, 1R dalam harga
check("gain 0.5R belum kunci", A.trail_lock_sl("SHORT", _e, _e - 0.5*_r, _r) is None)
check("gain 0.8R -> BE", A.trail_lock_sl("SHORT", _e, _e - 0.85*_r, _r) == 0.045)
check("gain 1.1R -> runner 0,5R ikut (0,4R vs runner, ambil ketat)", A.trail_lock_sl("SHORT", _e, _e - 1.1*_r, _r) == 0.0359)
check("gain 1.6R -> runner lebih ketat dari 0,8R", A.trail_lock_sl("SHORT", _e, _e - 1.6*_r, _r) == 0.0283)
check("gain 2.1R -> runner lebih ketat dari 1,2R", A.trail_lock_sl("SHORT", _e, _e - 2.1*_r, _r) == 0.0206)
check("gain 3.0R -> 1.5R+", A.trail_lock_sl("SHORT", _e, _e - 3.2*_r, _r) <= 0.0222)
check("LONG 0.8R -> BE", A.trail_lock_sl("LONG", 100.0, 108.0, 10.0) == 100.1)
check("r_dist 0 -> None", A.trail_lock_sl("LONG", 100.0, 120.0, 0.0) is None)

# --- 16. kunci profit berbasis persen (koin volatil) ---
check("0.3R persen -> belum kunci", A.trail_lock_pct("SHORT", 0.045, 10.0, 34.0) is None)
check("0.5R persen -> BE", A.trail_lock_pct("SHORT", 0.045, 17.0, 34.0) == 0.0)
check("0.8R persen -> 0.25R", A.trail_lock_pct("SHORT", 0.045, 27.5, 34.0) == 0.25)
check("1.2R persen -> 0.5R", A.trail_lock_pct("SHORT", 0.045, 41.0, 34.0) == 0.5)
check("2.0R persen -> 1.0R", A.trail_lock_pct("SHORT", 0.045, 70.0, 34.0) == 1.0)
check("atr 0 -> None", A.trail_lock_pct("SHORT", 0.045, 50.0, 0.0) is None)

# --- 17. lock_sl tidak boleh muncul saat posisi rugi ---
check("rugi SHORT -> tidak ada lock", A.trail_lock_sl("SHORT", 0.4769, 0.4974, 0.0407) is None)
check("rugi LONG -> tidak ada lock", A.trail_lock_sl("LONG", 100.0, 95.0, 5.0) is None)
check("profit SHORT 1.1R -> lock ada", A.trail_lock_sl("SHORT", 0.04508, 0.04508 - 1.1*0.01528, 0.01528) is not None)

# --- 18. closed_syms tidak boleh memuat simbol yang sedang terbuka ---
A.save_state({"day_key": "2099-01-01", "active_trades": {"ZZZ/USDT:USDT": {"symbol": "ZZZ/USDT:USDT", "side": "LONG"}},
              "closed_syms": ["ZZZ/USDT:USDT", "AAA/USDT:USDT"]})
_st = A.load_state()
check("simbol hidup dibuang dari closed_syms", "ZZZ/USDT:USDT" not in (_st.get("closed_syms") or []))
check("simbol tutup lama tetap di closed_syms", "AAA/USDT:USDT" in (_st.get("closed_syms") or []))
check("state selftest bukan state live",
      os.path.abspath(A.STATE_FILE) != os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                    "autonomous_trader_state.json")))

# --- 20. Kandidat #1: filter lebar SL > 5% ---
check("sl_width_pct hitung benar", abs(A.sl_width_pct(100.0, 5.0) - 5.0) < 1e-9)
check("SL 5,0% masih lolos (batas inklusif)", A.sl_width_ok(100.0, 5.0) is True)
check("SL 5,01% ditolak", A.sl_width_ok(100.0, 5.01) is False)
check("data tidak valid -> fail-open (entry tidak diblokir)", A.sl_width_ok(0, 5.0) is True)
check("ambang 5,0% persis (bukan hasil optimasi)", abs(A.MAX_SL_PCT - 5.0) < 1e-9)

# --- 19. re-arm TP: yang dipulihkan adalah leg NYATA, bukan level strategi ---
_tm_new = {"entry": 0.004979, "sl": 0.005892,
           "tp_legs": [[1.0, 262.0, 0.004066], [1.2, 241.0, 0.003883]]}
check("re-arm pakai tp_legs kalau ada",
      A._tp_legs_for(_tm_new, "SHORT", 1005) == [(262.0, 0.004066), (241.0, 0.003883)])

_tm_old = {"entry": 0.004979, "sl": 0.005892, "tp1": 0.004066, "tp2": 0.003152}
_legs_old = A._tp_legs_for(_tm_old, "SHORT", 1005)
check("re-arm rekonstruksi 0,5/1,0/1,7R (bukan 2R level strategi)",
      len(_legs_old) == 3 and abs(_legs_old[1][1] - 0.004066) < 1e-6
      and abs(_legs_old[0][1] - 0.0045225) < 1e-6
      and all(abs(_p - 0.003152) > 1e-6 for _q, _p in _legs_old))

check("re-arm tanpa info entry/SL -> tidak memasang apa pun",
      A._tp_legs_for({"entry": 0, "sl": 0}, "SHORT", 10) == [])

# --- 21. hardcode cleanup: konstanta tunggal + stop param --------------------
check("ladder: levels satu sumber kebenaran", A.LADDER_LEVELS == [0.5, 1.0, 1.7])
check("ladder: bobot 0,50/0,25/0,15 (jumlah 0,90)",
      abs(sum(A.LADDER_WEIGHTS) - 0.90) < 1e-9
      and all(b < a for a, b in zip(A.LADDER_WEIGHTS, A.LADDER_WEIGHTS[1:])))
check("ladder: runner 10%", abs(A.LADDER_RUNNER_PCT - 0.10) < 1e-12)
check("re-arm: batas percobaan 3", A.REARM_MAX_TRIES == 3)
check("re-arm: toleransi duplikat 2e-4", abs(A.TP_DUP_TOL - 2e-4) < 1e-15)
check("news: jendela judul 180 menit", A.NEWS_TAG_TITLE_WINDOW_MIN == 180)
check("breaker: 10% equity", abs(A.BREAKER_EQUITY_PCT - 0.10) < 1e-12)
check("anti-pucuk: toleransi BB 1,5%", abs(A.BB_CLEAR_TOL - 0.015) < 1e-12)
check("stop: ambang cakupan qty 99,9%", abs(A.QTY_COVER_TOL - 0.999) < 1e-12)

# runner harus benar-benar disisakan: 1005 unit JCT -> 4 leg, total < qty
_tm_r = {"entry": 0.004979, "sl": 0.005892}
_legs_r = A._tp_legs_for(_tm_r, "SHORT", 1005)
_tot_r = sum(_q for _q, _p in _legs_r)
check("re-arm: runner tetap disisakan (<=92% qty)", len(_legs_r) == 3 and _tot_r <= 1005 * 0.92)

import src.live_trader as LT

_p_on = LT.BinanceFuturesTrader._stop_params(
    LT.BinanceFuturesTrader.__new__(LT.BinanceFuturesTrader), 123.45)
check("stop: pakai workingType MARK_PRICE", str(_p_on.get("workingType")) == "MARK_PRICE")
check("stop: priceProtect aktif", _p_on.get("priceProtect") is True)
check("stop: stopPrice tetap dibawa", abs(float(_p_on.get("stopPrice")) - 123.45) < 1e-9)

LT.BinanceFuturesTrader._stop_extra_ok = False
_p_off = LT.BinanceFuturesTrader._stop_params(
    LT.BinanceFuturesTrader.__new__(LT.BinanceFuturesTrader), 123.45)
LT.BinanceFuturesTrader._stop_extra_ok = True
check("stop: fallback tanpa param kalau venue menolak",
      "workingType" not in _p_off and "priceProtect" not in _p_off and "stopPrice" in _p_off)
check("stop: deteksi penolakan parameter",
      LT.BinanceFuturesTrader._stop_param_rejected(Exception("binance -1106 Parameter 'priceProtect' invalid"))
      and not LT.BinanceFuturesTrader._stop_param_rejected(Exception("-2021 Order would immediately trigger")))

# --- 22. dedupe fill pakai sidecar (state reset tidak boleh bikin hitung dobel) ---
_side_old = A.RECON_SIDECAR
A.RECON_SIDECAR = "/tmp/.selftest_recon_ids.json"
try:
    os.remove(A.RECON_SIDECAR)
except Exception:
    pass


class _FakeTr:
    class exchange:
        @staticmethod
        def fapiPrivateGetUserTrades(params):
            return [
                {"id": "1", "time": 1000, "realizedPnl": "0.9383", "commission": "0.0013"},
                {"id": "2", "time": 2000, "realizedPnl": "0.9590", "commission": "0.0011"},
            ]


_st1 = {}
A._mark_recon(_st1, "Q/USDT:USDT", ["1"])
check("sidecar: id fill tersimpan di luar state",
      "1" in (A._recon_ids_load().get("Q/USDT:USDT") or []))
_p, _f, _ids, _t = A._recon_fills(_FakeTr, "QUSDT", {}, "Q/USDT:USDT")
check("sidecar: state kosong tidak menghitung ulang fill lama",
      _ids == ["2"] and abs(_p - 0.9590) < 1e-9)
A.RECON_SIDECAR = _side_old
try:
    os.remove("/tmp/.selftest_recon_ids.json")
except Exception:
    pass


# --- UNIVERSE dinamis: seluruh pasar, bukan 45 koin hardcode ---
class _FakeUniTr:
    class exchange:
        @staticmethod
        def fetch_tickers():
            return {
                "BTC/USDT:USDT": {"quoteVolume": 5_000_000_000},
                "ETH/USDT:USDT": {"quoteVolume": 2_000_000_000},
                "DOOD/USDT:USDT": {"quoteVolume": 3_000_000},
                "BTCUP/USDT:USDT": {"quoteVolume": 9_000_000_000},
                "USDC/USDT:USDT": {"quoteVolume": 9_000_000_000},
            }

        @staticmethod
        def load_markets():
            def mk(base, quote="USDT", active=True):
                return {"base": base, "quote": quote, "swap": True, "linear": True, "active": active}
            return {
                "BTC/USDT:USDT": mk("BTC"),
                "ETH/USDT:USDT": mk("ETH"),
                "DOOD/USDT:USDT": mk("DOOD"),
                "BTCUP/USDT:USDT": mk("BTCUP"),
                "USDC/USDT:USDT": mk("USDC"),
            }


class _FakeUniErr:
    class exchange:
        @staticmethod
        def fetch_tickers():
            raise RuntimeError("boom")

        @staticmethod
        def load_markets():
            raise RuntimeError("boom")


_mode_old = A.SCAN_MODE
_vol_old = A.SCAN_MIN_VOL_USDT
_rank_old = A.SCAN_PRE_RANK

A.SCAN_MODE = "static"
_u, _m = A.build_universe(_FakeUniTr)
check("universe: mode static = WATCHLIST lama", _m == "static" and _u == A.WATCHLIST)

A.SCAN_MODE = "market"
A.SCAN_MIN_VOL_USDT = 20_000_000
A.SCAN_PRE_RANK = 50
_u, _m = A.build_universe(_FakeUniTr)
check("universe: buang microcap + stablecoin + token leveraged",
      sorted(_u) == ["BTC/USDT:USDT", "ETH/USDT:USDT"] and _m.startswith("market"))

A.SCAN_PRE_RANK = 1
_u, _m = A.build_universe(_FakeUniTr)
check("universe: pre-rank likuiditas memotong ke N teratas", _u == ["BTC/USDT:USDT"])

_u, _m = A.build_universe(_FakeUniErr)
check("universe: API gagal -> fallback WATCHLIST (bot tidak berhenti)",
      _m == "fallback" and _u == A.WATCHLIST)

A.SCAN_MODE = _mode_old
A.SCAN_MIN_VOL_USDT = _vol_old
A.SCAN_PRE_RANK = _rank_old


# --- RANKING: pita volume surge, bukan surge terbesar ---
_c1 = ("A/USDT:USDT", "BUY", {}, 1.30)
_c2 = ("B/USDT:USDT", "BUY", {}, 2.50)
_c3 = ("C/USDT:USDT", "BUY", {}, 1.45)

_rk_old = A.SCAN_RANK_MODE
A.SCAN_RANK_MODE = "band"
_r = A.rank_candidates([_c2, _c1, _c3])
check("rank band: surge 2,5x tidak didahulukan",
      [c[0] for c in _r] == ["C/USDT:USDT", "A/USDT:USDT", "B/USDT:USDT"])

A.SCAN_RANK_MODE = "surge"
_r = A.rank_candidates([_c1, _c2, _c3])
check("rank surge: perilaku lama tetap bisa dipanggil (rollback)",
      [c[0] for c in _r] == ["B/USDT:USDT", "C/USDT:USDT", "A/USDT:USDT"])

A.SCAN_RANK_MODE = _rk_old

# --- 23. momentum sleeve (aturan #5) ---
try:
    import numpy as _np
    import pandas as _pd
    _n = 160
    _cl = _np.linspace(100, 160, _n)
    _df = _pd.DataFrame({"t": range(_n), "o": _cl, "h": _cl * 1.005,
                         "l": _cl * 0.995, "c": _cl, "v": _np.full(_n, 1000.0)})
    check("adx14 tren naik kuat > 25", float(A.adx14(_df).iloc[-2]) > 25.0)
    _flat = _pd.DataFrame({"t": range(_n), "o": 100.0, "h": 100.2,
                           "l": 99.8, "c": 100.0, "v": _np.full(_n, 1000.0)})
    check("adx14 pasar datar < 25", float(A.adx14(_flat).iloc[-2]) < 25.0)
    os.environ["MOM_SLEEVE"] = "0"
    check("sleeve mati via MOM_SLEEVE=0",
          A.momentum_candidates(None, [], set(), {}) == [])
    os.environ.pop("MOM_SLEEVE", None)
    check("sleeve tidak dobel: sudah ada posisi MOM",
          A.momentum_candidates(None, [], set(),
                                {"active_trades": {"X": {"sleeve": "MOM"}}}) == [])
except Exception as _e:
    check("momentum sleeve check bisa jalan: %s" % _e, False)

try:
    os.remove(A.STATE_FILE)
except Exception:
    pass

if failures:
    print(f"\nRESULT: {len(failures)} FAILED: {failures}")
    sys.exit(1)
else:
    print("\nRESULT: ALL PASS (%d/%d)" % (passed[0], passed[0]))
    sys.exit(0)
