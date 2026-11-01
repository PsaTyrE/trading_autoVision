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
- **2026-10-30** — `MAX_DAILY_ENTRIES` 6 -> **3 (revert)**.
  Alasan: cap 6 membuat bot menumpuk entri di hari yang sama saat sinyal lemah,
  sementara slot tetap 3. Tidak ada manfaat terukur, hanya menambah churn.
  Nilai aktif sekarang: **3**. Entri di atas dipertahankan sebagai catatan sejarah.

## Changelog (bukan tuning, penegakan aturan)

- **RSI 1h hard veto** — veto "DILARANG BELI DI PUCUK" sebelumnya hanya memakai RSI
  timeframe sinyal. Sinyal dari 4h jadi lolos walau 1h sudah vertikal (INJ: 4h 57.2 < 58,
  1h 81.5 -> -1.01R). Sekarang RSI 1h juga dicek (>=58 LONG / <=42 SHORT), ambang sama.
  Ini penegakan aturan yang sudah ada, bukan parameter baru.
  Dampak terukur pada 8 entry terakhir: memblokir BNB, LTC, ONDO, INJ (semua rugi, total
  -1.73R) tanpa memblokir satu pun yang profit (ZEC +1.23R, XRP/GTC masih jalan).

---

## PRE-REGISTRASI — kandidat perubahan checkpoint 20

**Ditulis 2026-11-01, saat 17 trade closed (3 trade lagi ke checkpoint).**
Ambang di bawah DIKUNCI SEKARANG, sebelum trade ke-18/19/20 terlihat, supaya tidak
bisa digeser setelah hasilnya diketahui. Kalau ambang diubah setelah checkpoint,
itu curve-fitting dan seluruh evaluasi batal.

### Kandidat #1 — Tolak entry jika SL > 5% dari harga

Dasar bukti (17 close, per 2026-11-01):

| Lebar SL | n | Total R | Avg R |
|---|---|---|---|
| <= 5% | 7 | **+5.90R** | **+0.84R** |
| > 5% | 4 | **-1.49R** | **-0.37R** |

Pemisahan 11 dari 11 benar. Korelasi SL% vs R = **-0.512**.

Kasus penentu — APR: SL 17.50%, MFE **+14.55%**, hasil akhir **-0.04R**.
Mekanisme: TP1 = 1.0R = lebar SL. Dengan SL 17.5%, TP1 berjarak 17.5% dan tangga
lock 0.4R berjarak 7% — tidak ada yang pernah tersentuh, jadi profit 14.5% hilang
tanpa pernah terkunci. Ini matematika, bukan nasib.

**Biaya yang sudah diketahui dan diterima:** Q punya SL 33.9% (pelanggaran terparah)
tapi sekarang +1.02R dan sudah membukukan +$0.937. Filter ini akan menolak Q sejak awal.
Diterima karena agregat 7-vs-4 jauh lebih besar dari satu pemenang.

**Ambang dikunci: 5.0%. Eksekusi di trade ke-20, apa pun hasil 3 trade berikutnya.**

### Kandidat #2 — Stagnation stop

Tutup posisi jika **umur >= 12 jam DAN MFE < 0.5R**.

Dasar bukti: 4 trade dengan MFE < 1.5% menghasilkan avg **-0.36R**, tidak ada satu pun
di atas +0.01R. Menunggu 24 jam membuat kerugian membesar ke -1R sebelum dipotong.

Alasan 12 jam: median jarak antar close 3.1 jam, jadi 12 jam = ~4x median. Aturan ini
hanya menyentuh outlier (ZEC 49.6 jam MFE 0.35%, BR 41.6 jam MFE 0.00%), bukan trade normal.

**Ambang dikunci: 12 jam / 0.5R.**

### Yang TIDAK diubah (dan alasannya)

- **Max posisi tetap 3.** 5 slot x $1.14 = $5.70 = tepat ambang circuit breaker
  (`max($5.00, 10% equity)`). 5 SL bersamaan = bot mati, bukan cuma rugi.
  Rata-rata posisi terbuka hanya 0.19 — slot bukan bottleneck.
- **Risk tetap 2% equity.** Menaikkan risk untuk mengejar target bukan strategi.
- **TP ladder tetap 1.0/1.2/1.4/1.7R.** 40.000 run Monte Carlo: ladder jauh +$0.873
  vs rapat +$0.783; dengan drift -4%/jam +$1.611 vs +$1.050.

---

## Kandidat #3 — PENGUKURAN, BUKAN PARAMETER (dikunci 2026-11-01)

Konteks: news guard (`src/forex_factory_news.py`) menahan **entry** 30 menit sebelum
sampai 45 menit sesudah berita merah USD. Posisi yang **sudah terbuka** tidak
diperlakukan khusus.

**Yang dibekukan** (bukan diubah — dinyatakan tidak akan diubah):
Tidak ada perubahan perlakuan posisi terbuka menjelang berita merah: tidak melebarkan
SL, tidak merapatkan SL, tidak menutup posisi. Berlaku sampai terkumpul **>= 5 sampel
slippage nyata** di `slippage.csv` dengan `near_news = 1`.

**Alasan angka** (posisi 2026-11-01, risk $1,14/trade):
- Q: notional $1,61, SL 30% dari harga -> praktis kebal spike.
- INJ: notional $24,54, SL 3,5% dari harga. Gap 2% lewat SL = +$0,49 (0,43R);
  gap 5% = +$1,23 (1,08R).
- Peluang ada posisi terbuka saat berita: rata-rata 0,19 posisi -> ~19%.
- Ekspektasi kerugian tambahan kalau dibiarkan: ~0,19 x $0,70 = **~$0,13**.
- Biaya tiap alternatif lebih besar: melebarkan SL menaikkan risiko di 100% trade;
  merapatkan SL memotong pemenang (BTW: BE 0,5R mematikan MFE 4,88% -> 0,00R);
  tutup-buka bayar fee dua kali dan kehilangan tren.

**Kenapa dibekukan, bukan diserahkan ke selera:** supaya tidak ada aturan yang dibuat
SETELAH melihat hasil — kesalahan yang hampir terulang pada kasus BE 0,5R.

**Yang sudah jalan:** `slippage.csv` (SL terpasang vs harga fill nyata + label
`near_news`), `sl_live` di state (SL aktif, bukan SL awal sinyal), dan notif Telegram
heads-up kalau ada posisi terbuka dekat berita merah.

**Keputusan perlakuan posisi: ditunda** sampai >= 5 sampel, lalu dievaluasi sekali.

## Perbaikan bug 2026-11-02 (bukan perubahan parameter strategi)

- **Re-arm TP memulihkan level yang salah.** `trade_record["tp2"]` = level strategi (≈2R),
  padahal ladder memasang TP2 di **1,2R**. Kalau order TP hilang, leg yang dipulihkan duduk di
  harga yang tidak pernah dipakai strategi (JCT: state 0,003152 vs order nyata 0,003883).
  Sekarang entry menyimpan `tp_legs` (leg yang benar-benar terpasang) dan `tp_failed`
  (leg yang gagal dipasang). Re-arm hanya memulihkan leg yang **gagal**, atau membangun ulang
  seluruh ladder kalau tidak ada order TP sama sekali. Leg yang sudah kena TP tidak pernah
  dipasang ulang (percobaan Q 00:12:07 hampir menaruh 11 unit di 0,026744 yang sudah fill).
- **`save_state()` gagal di `manage_open_positions()` ditelan senyap** → sekarang tercatat
  `STATE SAVE FAILED (manage): ...`. Kalau tulis state gagal, `sl_live` dan akuntansi TP
  parsial hilang tanpa jejak.
- **Posisi ADOPT ditandai `risk_est: true`.** `risk`-nya dihitung dari SL yang **sedang**
  terpasang (bisa sudah trailing), bukan SL awal → R-nya taksiran. Jangan dihitung sebagai
  sampel checkpoint 20 trade tanpa catatan.
- Selftest: **68 check** (3 check baru untuk `_tp_legs_for`).
