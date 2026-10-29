# FREEZE — 20 Trade Checkpoint

**Status: BEKUKAN SEMUA PARAMETER sampai 20 trade closed tercatat di `trade_closed.csv`.**

Alasan: per 2026-10-29 sampel baru 7 trade. Optimasi parameter di atas sampel sekecil itu
adalah overfitting, bukan edge. Bug fix boleh, ganti setting tidak.

## Yang DIBEKUKAN (jangan diubah tanpa 20 trade)

| Parameter | Nilai | Lokasi |
|---|---|---|
| Risk base | 2.0% equity | `RISK_TIERS` di `auto_pilot_trader.py` |
| Risk ladder | 2.0 / 2.4 / 2.8 / 3.2 / 3.5% | `RISK_TIERS` |
| Risk cap | 1.6x risk base | `risk_max_budget()` |
| TP ladder | 1.0R / 1.2R / 1.4R / 1.7R | blok `LADDER TP MULTI-LEVEL` |
| Porsi TP | 26 / 24 / 22 / 18 % + runner 10% | idem |
| Trailing | BE @0.8R, +0.5R @1.2R, +1.25R @2.0R | `manage_open_positions()` |
| Timeframe | 4H primary + 1H confirm (60% size) | scan loop |
| Filter entry | RSI 36-58, BB clearance >=1.5%, vol >=1.0x | `src/strategy.py` |
| Min AI conf | 0.85 | `src/multi_analyst.py` |
| Max posisi | 3 | `.env` `MAX_OPEN_POSITIONS` |

## Yang BOLEH diubah kapan saja

- **Bug fix** (logika salah, order gagal, exception, state corrupt)
- **Observability**: logging, pencatatan metrik, notifikasi
- **Safety**: circuit breaker, orphan order sweep, verifikasi SL aktif

## Metrik wajib per trade

Dicatat otomatis ke `trade_excursion.csv`:
`ts, symbol, side, entry, net, R, mfe_pct, mae_pct, sl_pct`

- **R** = hasil akhir dalam satuan risiko
- **MFE** = puncak profit tertinggi yang pernah dicapai (%)
- **MAE** = drawdown terdalam yang pernah dialami (%)

## Cara baca hasil di checkpoint 20 trade

| Temuan | Artinya | Tindakan |
|---|---|---|
| MFE sering jauh di atas 1.7R | TP kepagian | geser ladder naik |
| MFE jarang lewat 1.0R | TP kekecengan | tarik TP1 lebih dekat |
| MAE sering mendekati -1R lalu profit | SL terlalu ketat | lebarkan SL / kecilkan size |
| Expectancy < 0 setelah 20 trade | edge tidak ada | STOP, jangan tambah risk |

## Aturan checkpoint

1. Evaluasi HANYA setelah 20 trade closed.
2. Lanjut kalau expectancy >= +0.15R.
3. Stop dan audit ulang kalau expectancy < 0.
4. Jangan naikkan risk untuk mengejar target — itu bukan strategi, itu harapan.


## Changelog keputusan (terdokumentasi, bukan drift)

- **2026-10-29 23:30** — `MAX_DAILY_ENTRIES` 3 -> 6.
  Alasan: cap entri harian tumpang tindih dengan `MAX_OPEN_POSITIONS=3`.
  Dengan 3 slot, risiko total sudah dibatasi; cap entri cuma menghalangi
  redeploy modal setelah TP kena di hari yang sama. Batas risiko TIDAK berubah
  karena slot posisi, `MAX_EXPOSURE_MULT=2.0`, dan `MAX_DAILY_LOSS_PCT=5%` tetap.
  `MAX_OPEN_POSITIONS` sengaja TIDAK dinaikkan — itu yang benar-benar menambah risiko.

## Changelog (bukan tuning, penegakan aturan)

- **RSI 1h hard veto** — veto "DILARANG BELI DI PUCUK" sebelumnya hanya memakai RSI
  timeframe sinyal. Sinyal dari 4h jadi lolos walau 1h sudah vertikal (INJ: 4h 57.2 < 58,
  1h 81.5 -> -1.01R). Sekarang RSI 1h juga dicek (>=58 LONG / <=42 SHORT), ambang sama.
  Ini penegakan aturan yang sudah ada, bukan parameter baru.
  Dampak terukur pada 8 entry terakhir: memblokir BNB, LTC, ONDO, INJ (semua rugi, total
  -1.73R) tanpa memblokir satu pun yang profit (ZEC +1.23R, XRP/GTC masih jalan).
