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

## Kandidat #1 DITERAPKAN — 2026-11-05 (checkpoint 29 close)

**Aturan:** entry ditolak kalau lebar SL > **5,0%** dari harga (`MAX_SL_PCT=5.0`, `0` = filter mati).
Pre-registrasi: lihat Kandidat #1 di atas, ditulis **sebelum** hasilnya diketahui.

**Bukti (29 close penuh, semua live, bukan backtest):**

| Kelompok | n | Total R | Expectancy |
|---|---|---|---|
| SL <= 5% | 19 | +6,67R | +0,35R |
| SL > 5%  | 10 | -1,32R | -0,13R |

**Catatan jujur:** dari 10 trade yang akan ditolak, **4 justru profit**
(INJ +0,23 / GTC +0,52 / UNI +0,44 / +1 lainnya) total **+2,06R**; 6 sisanya **-3,38R**.
Jadi filternya bukan "buang semua yang jelek" — agregatnya yang rugi.

**Ambang tidak dioptimasi.** Kalau ambang digeser ke 6%, hasilnya kelihatan lebih bagus
(n=6, -1,49R), tapi itu curve-fitting pada 29 sampel dan sengaja **tidak** dilakukan.
5,0% dipakai persis seperti yang ditulis di pre-registrasi.

**Efek samping yang diterima:** frekuensi entry turun ~1/3 (10 dari 29 trade historis akan
ditolak). Kuota harian 3/hari dan slot 3 tidak berubah.

## Perbaikan 2026-11-06 (bukan parameter strategi)

- **BUG PnL dobel.** `recon_ids` tinggal di state; begitu state direset, close
  berikutnya menjumlah ulang SEMUA fill sejak entry. Bukti Q: baris PARTIAL
  +0,9383 (31 Okt) lalu close final +1,8970, padahal fill ketiga cuma +0,9590.
  Kelebihan catat **+0,9383** (net 33 close 4,58 -> 3,64).
- **FIX.** Id fill sekarang juga disimpan di sidecar `.recon_ids.json` yang tidak
  disentuh `save_state()`; pembacaan `seen = state | sidecar`. Selftest 90/90.
- **Data.** Baris Q di `trade_closed.csv` dikoreksi: gross 0,9590 / net 0,8421.
- Angka acuan setelah koreksi: 33 close, net **+3,64**, **+5,82R**,
  expectancy **+0,176R**, WR 55,9%, avg win +0,57R vs avg loss -0,32R.

## Target +$100 — aturan tangga risiko (pre-registrasi, ditulis 2026-11-06)

Target: net trading **+$100** (equity ~155,9) dari deposit 55,78.
Hitungan saat pre-registrasi: expectancy +0,176R, ~1,5 close/hari, equity 56,87.
Compounding (expectancy dianggap tetap, ini asumsi bukan janji):
risk 2% ~180 hari | risk 3% ~120 hari | risk 5% ~72 hari.

Risk 5% **ditolak**: breaker harian 5% berarti satu loss langsung menghentikan
hari itu, jadi 5% tidak bisa dipakai bersama aturan breaker yang sekarang.

Aturan yang dikunci SEBELUM hasil diketahui:

1. Risk tetap **2%** sampai (a) net trading akun >= +$2,00 dan (b) minimal
   **20 close baru** sejak 2026-11-06.
2. Naik ke **3%** hanya kalau dari 20 close baru itu: expectancy >= **+0,15R**,
   profit factor >= **1,5**, max drawdown < **15%**, dan tidak ada pelanggaran
   aturan (entry lewat kuota, posisi tanpa stop, dsb).
3. Gagal salah satu -> tetap 2%, hitung ulang 20 close berikutnya. Ambang tidak
   digeser setelah hasil keluar.
4. Plafon **3%** sampai equity 100. Di atas itu evaluasi ulang dengan data baru.
5. Tambah modal bukan bagian rencana ini (keputusan user, bukan bot).

## 2026-11-08 - Universe dinamis (bukan parameter strategi)

Perubahan: `WATCHLIST` 45 koin hardcode TIDAK lagi jadi batas pemindaian.
`SCAN_MODE=market` menyusun daftar simbol tiap siklus dari `fetch_tickers()`:
semua perpetual USDT-M aktif, volume 24 jam >= $20 juta, tanpa stablecoin/fiat
dan tanpa token leveraged (UP/DOWN/BULL/BEAR/3L/3S), di-pre-rank volume desc,
dipotong `SCAN_PRE_RANK` = 50 simbol. Kalau API gagal -> fallback WATCHLIST.

Yang TIDAK berubah: RSI 58/42, ADX 22, ambang BB, MAX_SL_PCT 5.0, ladder TP,
risk 2%, MAX_OPEN_POSITIONS 3, MAX_DAILY_ENTRIES 3, seluruh aturan veto.
Patch ini mengubah **populasi trade**, bukan parameter entry/exit.

Alasan terukur: 20 dari 45 koin hardcode tidak lolos filter likuiditas $20 juta
(termasuk DOOD, AGT, APR, BR, IOST, Q, JCT, BTW). Di koin tipis biaya melebihi
edge: IOST realized +0,0486 tetapi net -0,0222 setelah komisi+funding. Ranking
lama memakai volume surge sehingga sistematis memilih koin paling liar.

Ukuran keberhasilan (diukur, bukan diklaim): 20 close pertama dengan universe
baru dibanding baseline 33 close (net +$3,64, expectancy +0,176R, PF 2,42,
WR 55,9%). Metrik: expectancy R, PF, rata-rata biaya per trade, max DD.
Kalau expectancy tidak naik -> universe dikembalikan (`SCAN_MODE=static`) dan
dilaporkan GAGAL. Ambang tidak digeser setelah hasil diketahui.

## 2026-11-08 - Ranking kandidat: pita volume surge (bukan surge terbesar)

Data 29 trade yang punya catatan entry + hasil (trade_log.csv x trade_excursion.csv):

| surge | n | expectancy |
|---|---:|---:|
| < 1,2x | 10 | +0,160R |
| 1,2-1,6x | 11 | +0,288R |
| >= 1,6x | 8 | +0,024R |

Selama ini `candidates.sort(vol_ratio, reverse=True)` mendahulukan surge TERBESAR,
yaitu kelompok dengan expectancy paling buruk. Sekarang diurutkan berdasarkan
kedekatan ke 1,4x (`SCAN_RANK_MODE=band`). Rollback: `SCAN_RANK_MODE=surge`.

Catatan kejujuran: n kecil (8-11 per kelompok), ini in-sample, bisa jadi noise.
Karena itu bukan klaim "sudah lebih bagus" - dibuktikan atau dibatalkan pada
20 close berikutnya dengan metrik expectancy R, PF, dan jumlah trade.

Yang TIDAK berubah: RSI 58/42, ADX 22, ambang BB, MAX_SL_PCT 5.0, ladder TP,
risk 2%, slot 3, kuota 3/hari, semua veto.

## 2026-11-08 - Konfirmasi volume bar 4h (UJI, pre-register)

**Yang berubah:** entry ditolak kalau volume bar 4h terakhir (bar close) < **0,6x** rata-rata volume 20 bar.
Env: `MIN_VOLR_4H=0.6`. Rollback: `MIN_VOLR_4H=0`.

**Yang TIDAK berubah:** RSI 42-64 / 36-58, ADX 22, ambang BB, `MAX_SL_PCT=5.0`, ladder TP,
risk 2%, slot 3, kuota 3 entry/hari, semua veto (anti-pucuk, Sentiment, news guard).

**Alasan terukur** (37 trade punya fitur entry lengkap, 29 Okt - 8 Nov, in-sample):

| Volume bar 4h | n | Win rate | Expectancy |
|---|---|---|---|
| < 0,6x | 13 | 31% | -0,10R |
| >= 0,6x | 24 | 62% | +0,23R |
| < 1,0x | 18 | 39% | -0,06R |
| >= 1,0x | 19 | 63% | +0,29R |

Dibatasi ke koin yang masih lolos universe likuiditas baru: < 0,6x -> n=7, WR 29%, exp -0,08R;
>= 0,6x -> n=13, WR 62%, exp +0,35R. Jadi efeknya bukan cuma efek koin tipis.

Dua posisi yang ditutup 2026-11-08 (BNB 0,03x, DOOD 0,11x) ada di kelompok < 0,6x.

**Kejujuran soal batas bukti:** n kecil, dan 20 kombinasi fitur diuji -> Fisher p = 0,09-0,19.
Artinya ini kandidat, bukan bukti. Ambang 0,6 dipilih sebagai batas alami data (0,59 lalu 0,62)
dan angka bulat, BUKAN optimum statistik (1,36) supaya tidak curve-fitting.

**Metrik keberhasilan (dinilai setelah 20 close baru):**
- Lulus: win rate >= 55% DAN expectancy >= +0,15R pada entry yang lolos filter.
- Gagal: win rate < 50% atau expectancy negatif -> `MIN_VOLR_4H=0`, dicabut, dan ditulis di sini bahwa gagal.

**Konsekuensi yang diterima:** frekuensi entry turun (13 dari 37 historis akan ditolak, ~35%),
jadi profit lebih lambat per kalender, ditukar dengan kualitas entry.


---

## Pre-registrasi 2026-11-12: slippage stop-market (BELUM diubah)

**Pemicu:** 12 Nov 2026 dua loss lewat 1R di token saham —
SKHYNIX -1,02R (slip -1,55%), EWY -1,14R (slip -0,89%). Total bocor 0,26R dari eksekusi.

**Data yang membantah dugaan awal:** kedalaman top-of-book saat entry justru cukup
(SKHYNIX $7.638, EWY $3.138, DRAM $28.610). Jadi penyebabnya bukan buku tipis,
tapi harga lompat melewati stop (gap), bukan ketiadaan lawan transaksi.

**Aturan yang dipra-registrasi (diputuskan SEBELUM data berikutnya ada):**
- Diukur: rata-rata `slip_pct` dari `slippage.csv` pada 10 close berikutnya (13 Nov - selesai checkpoint).
- Ambang: rata-rata slip <= 0,15R -> tidak ada perubahan (stop-market dipertahankan).
- Ambang: rata-rata slip > 0,15R -> stop diganti `STOP` limit dengan buffer 0,5% dari trigger
  (batas rugi terkunci, risiko tidak terisi saat gap diterima sadar).
- Yang TIDAK dilakukan: melarang token saham. Tiga trade token saham terbaik
  (ANTHROPIC +0,60/+0,66, MSTR +1,11, CRCL +1,17) justru dari kelas yang sama,
  jadi larangan itu akan memotong edge yang sudah terbukti.

**Status:** nol perubahan parameter. Parameter tetap beku sampai checkpoint 20 close.


---

## Mandat 2026-11-13: kewenangan tutup posisi

User menyerahkan keputusan tutup posisi ke assistant. Supaya tidak jadi intervensi
liar yang merusak pengukuran, kewenangan itu dibatasi tiga pemicu saja:

1. **Stop hilang di exchange** - posisi tanpa order proteksi (dikonfirmasi 2x baca).
   Tindakan: flatten seketika. Ini kegagalan proteksi, bukan keputusan trading.
2. **Dead money** - posisi terbuka >24 jam dengan |R| < 0,20 DAN tren bar 4h sudah
   berbalik melawan posisi. Tindakan: tutup di harga pasar.
3. **TP/SL tersentuh** - ditangani bot, bukan intervensi.

Yang **tidak** dibenarkan: menutup posisi merah cuma karena takut, menutup posisi
hijau sebelum TP karena ingin mengunci, atau menutup karena berita. Semua itu
menambah variabel dan membuat exp/R tidak lagi mencerminkan strategi.

Setiap close manual wajib dicatat alasannya (pemicu 1/2/3) supaya bisa diaudit
dan dikeluarkan dari statistik strategi kalau perlu.


---

## Target 2026-11-14: equity $100 pada 2027-02-14 (90 hari)

**Titik awal:** equity $59,67. Target $100 = +67,6% dalam 90 hari = **+0,574%/hari**.
Baseline sekarang 0,26%/hari (risk 2%, exp 0,103R, ~2,5 close/hari). Butuh ~2,2x.

**Syarat matematis yang harus dipenuhi bersamaan:**
- exp >= +0,15R per close
- risk 3% (dibuka hanya setelah syarat ladder terpenuhi)
- frekuensi >= 2,5 close/hari

**Gerbang go/no-go (diputuskan sebelum data ada):**

| tanggal | yang diukur | lulus jika | gagal maka |
|---|---|---|---|
| 2026-11-20 | checkpoint vol4h 20/20 | exp >= +0,15R | cabut filter, baseline ulang |
| 2026-11-30 | exp 20 close pasca-putusan | exp >= +0,15R | target turun jadi $75, risk tetap 2% |
| 2026-12-31 | equity | >= $68 (+14%) | evaluasi ulang pendekatan |
| 2027-01-31 | equity | >= $82 | target turun, jangan naikkan risk |
| 2027-02-14 | equity | >= $100 | target meleset, dilaporkan apa adanya |

**Larangan tegas:** menaikkan risk di atas 3% untuk mengejar tanggal, atau menambah
syarat entry baru di tengah jalan, DILARANG. Satu-satunya jalur ke target adalah exp.

**Ambang berhenti (dari mandat sebelumnya):** equity < $47 atau 4 minggu berturut exp negatif.


---

## Putusan checkpoint filter vol4h - GAGAL (2026-11-17)

**Hipotesis (dipra-registrasi 2026-11-08):** entry hanya di bar 4h dengan volume
>= 0,6x rata-rata 20 bar akan menaikkan expectancy.

**Hasil (19 close, 2026-11-08 07:24 s/d 2026-11-17 22:36 WIB):**
- WR **42%** (ambang lulus 50%)
- exp **-0,130R** (ambang lulus +0,15R)
- Lunas secara matematis: dengan 1 close tersisa, exp maksimum yang mungkin
  (-2,47 + 1,0)/20 = **-0,074R** - tetap gagal. Tidak perlu menunggu close ke-20.

**Putusan:** filter **DICABUT**. `MIN_VOLR_4H=0`. Alasan: tidak menaikkan edge;
sampel 19 close konsisten di bawah ambang, bukan soal keberuntungan satu-dua trade.

**Pembanding sebelum filter (2026-10 s/d 08 Nov):** exp +0,13R pada 46 close.
Artinya filter ini tidak menambah apa pun yang bisa dibuktikan.

**Langkah berikut (dipra-registrasi):** ukur baseline 20 close tanpa filter.
Sesudah itu uji kandidat #2 (ladder TP: avg win 0,66R vs avg loss 0,81R,
payoff 0,82:1 - lihat catatan 2026-11-15). Filter sesi ditunda sesudahnya.


---

## Pra-registrasi: uji gate momentum entry (2026-11-17)

**Konteks:** checkpoint vol4h gagal (exp -0,130R, dicabut 2026-11-17).
Median MFE 48 trade = **0,65R**, 31/48 tidak pernah lewat 0,8R. Diagnosis: entry
tidak punya follow-through. Exit tuning sudah dibuktikan bukan solusi (TP ladder
tidak bisa menolong trade yang tidak pernah sampai 1R).

**Yang diuji:** satu gate momentum di entry, dipilih dari `entry_feat.csv`.

**Aturan pemilihan variabel (dikunci SEBELUM data dianalisis):**
1. Kandidat variabel: ai_conf, vol_ratio, RSI, ADX, ATR%, spread, depth, sesi.
2. Sampel minimum: 20 entry tercatat. Bucket harus punya >= 7 trade.
3. Variabel dipilih = yang punya selisih exp terbesar antara bucket atas dan bawah,
   dengan syarat selisih >= 0,30R. Kalau tidak ada yang >= 0,30R -> **tidak ada gate
   yang dipasang**, dan uji dinyatakan gagal tanpa menyentuh parameter.
4. Ambang = median sampel (bukan angka yang dioptimalkan).
5. Hanya SATU variabel. Bukan dua, bukan kombinasi.
6. Gate dipasang sebagai kode baru, bukan dengan mengubah ambang yang sudah ada.

**Kriteria lulus (20 close setelah gate):**
- exp >= **+0,10R**
- WR >= **45%** (turun dari 50% karena gate selektif biasanya menurunkan WR)
- Jumlah kandidat lolos tetap >= ~40/hari (dicek dari funnel log)

**Kalau gagal:** gate dicabut, ditulis di sini dengan angka, dan **bot dihentikan**.
Tidak ada uji variabel ketiga. Dua kegagalan (vol4h, gate momentum) = bukti cukup
bahwa pendekatan pullback ini tidak punya edge yang bisa ditemukan lewat tuning.

**Batas waktu:** analisis dilakukan paling lambat **2026-11-28**. Kalau sampai
tanggal itu `entry_feat.csv` belum mencapai 20 entry, uji dibatalkan dan bot
dihentikan (bukan diperpanjang tanpa batas).


---

## Putusan gate momentum - GAGAL di tahap seleksi variabel (2026-11-17)

**Metode:** backfill fitur dari candle 4h tertutup untuk 54 dari 63 close
(9 gagal: simbol /PARTIAL dan token saham ANTHROPIC tidak ada di binanceusdm).

**Hasil bucket volr4h (satu-satunya variabel dengan selisih besar):**
```
0,0-0,7  n=20  exp -0,186R  WR 30%
0,7-1,0  n=10  exp -0,035R  WR 60%
1,0-1,3  n=11  exp -0,144R  WR 36%
1,3-1,8  n= 4  exp -0,300R  WR 25%
>=1,8    n= 9  exp +0,752R  WR 89%
```
Split median (0,93): atas +0,070R vs bawah -0,073R = **+0,142R**.
Ambang pra-registrasi +0,30R -> **TIDAK LULUS**.

**Kenapa tidak dipasang:** bentuknya spike di ujung, bukan gradien. Bucket
1,3-1,8 negatif (-0,300R) lalu >=1,8 melonjak. WR 89% pada n=9 adalah noise,
bukan edge. Memasang gate >=1,8 = mencocokkan angka ke sampel kecil, kesalahan
yang sama dengan filter vol4h.

**Putusan:**
- Tidak ada gate dipasang. Nol parameter strategi berubah.
- `MAX_DAILY_ENTRIES=0` -> **entry live dihentikan**. Posisi terbuka (kalau ada)
  tetap dikelola trail + watchdog.
- Dua hipotesis gugur: filter vol4h, gate momentum.

**Arah berikutnya (bukan tuning, ini ganti keluarga entry):**
Data MFE menunjukkan median gerak hanya 0,65R - pullback tidak dapat
follow-through. Satu-satunya bucket yang untung adalah volume expansion
(>=1,8x), yang secara karakter adalah **momentum/breakout**, bukan pullback.
Rencana: uji entry breakout volume-expansion **di shadow** (tanpa uang) minimal
20 sinyal, diukur dari kline setelah sinyal muncul. Baru bicara uang kalau
shadow itu exp >= +0,15R.
