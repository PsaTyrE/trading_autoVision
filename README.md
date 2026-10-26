# 🤖 Autonomous AI Crypto Futures Trading Bot

Sistem trading otomatis untuk **Binance Futures (USD-M)** yang ditenagai oleh **Google Gemini AI** dan dilengkapi dengan **Risk Management Guardrail** berlapis.

Sistem ini mendukung transisi bertahap:
1. **Paper Trading** (Simulasi saldo virtual $1,000 dengan fee realistik 0.04% & liquidation calculation)
2. **Binance Testnet** (Testing order asli di server simulasi Binance)
3. **Binance Live** (Trading riil dengan akun Binance Anda)

---

## 🚀 Fitur Utama

- **🧠 Google Gemini Reasoning Engine**: Menganalisis kondisi pasar secara holistik (Trend EMA20/50/200, Momentum RSI, MACD Histogram, Volatilitas ATR, Bollinger Bands, Volume breakout).
- **🛡️ Strict Risk Management Guardrails**:
  - **Dynamic Position Sizing**: Menghitung lot berdasarkan toleransi risiko per transaksi (misal: maks 2% dari modal).
  - **Mandatory Hard Stop-Loss & Take-Profit**: Setiap posisi selalu dilindungi SL & TP.
  - **Risk-to-Reward Filter**: Hanya membuka posisi jika rasio RR $\ge 1.5$ dan Confidence AI $\ge 70\%$.
  - **Circuit Breaker**: Otomatis menghentikan aktivitas trading selama 24 jam jika kerugian harian menyentuh batas (misal: -5%).
  - **Isolated Margin Cap**: Mencegah posisi melikuidasi sisa modal di dompet.
- **📱 Real-Time Telegram Alerts**: Notifikasi instan saat sinyal dieksekusi, TP/SL tercapai, dan ringkasan PnL.
- **💻 Console Dashboard**: Tampilan visual real-time via terminal dengan tabel status portofolio.

---

## 📁 Struktur Direktori

```text
├── .env                  # Tempat menyimpan API Key Anda (Aman & terlindungi oleh .gitignore)
├── .env.example          # Template konfigurasi
├── requirements.txt      # Dependensi Python
├── main.py               # Entry point utama bot
└── src/
    ├── config.py         # Pengelola konfigurasi & validasi Pydantic
    ├── indicators.py     # Kalkulasi teknikal (EMA, RSI, MACD, ATR, BB)
    ├── market_data.py    # Feeder data candlestick Binance Futures via CCXT
    ├── ai_analyzer.py    # Integrasi penalaran Gemini AI + Fallback Quant Engine
    ├── risk_manager.py   # Guardrail manajemen risiko & circuit breaker
    ├── paper_trader.py   # Simulator paper trading & portofolio virtual
    ├── live_trader.py    # Eksekutor order Binance Futures (Testnet & Live)
    └── telegram_notifier.py # Pengirim notifikasi Telegram
```

---

## ⚙️ Cara Menyiapkan API & Konfigurasi

Buka file [`.env`](file:///d:/Dataku/Gawean_Aing/wegodev/latihan/.env) dan lengkapi data berikut:

1. **Binance API**:
   - `BINANCE_API_KEY`: Masukkan API Key dari Binance.
   - `BINANCE_API_SECRET`: Masukkan Secret Key dari Binance.
   *(Pastikan opsi penarikan / `Enable Withdrawals` dalam keadaan **NONAKTIF** demi keamanan).*

2. **Google Gemini API**:
   - `GEMINI_API_KEY`: Dapatkan gratis di [Google AI Studio](https://aistudio.google.com/).

3. **Telegram Notifier (Opsional)**:
   - Buat bot di Telegram via `@BotFather` untuk mendapatkan `TELEGRAM_BOT_TOKEN`.
   - Dapatkan Chat ID Anda via `@userinfobot` untuk `TELEGRAM_CHAT_ID`.

---

## 🏃 Cara Menjalankan Bot

Pastikan environment virtual aktif:
```bash
.\venv\Scripts\activate
```

### 1. Uji Coba Sekali Jalan (Single Cycle / Test Run)
Memeriksa data pasar, sinyal AI, dan status portofolio tanpa looping:
```bash
python main.py --once
```

### 2. Menjalankan Bot 24/7 (Mode Simulasi Paper Trading)
Sangat disarankan untuk menguji strategi terlebih dahulu:
```bash
python main.py --mode paper --interval 15
```

### 3. Menjalankan di Binance Testnet
```bash
python main.py --mode testnet --interval 15
```

### 4. Menjalankan di Binance Live (Real Money)
```bash
python main.py --mode live --interval 15
```
