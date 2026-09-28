
# Broker Accumulation Tracker IDX

Tracker untuk mengestimasi **akumulasi / distribusi broker** pada saham BEI menggunakan endpoint Zapi Pluang Broker Summary.

> Penting: ini bukan data kepemilikan resmi. Endpoint upstream broker summary hanya menampilkan broker teratas per sisi. Karena itu `net_lots` di proyek ini berarti cumulative net activity broker yang terlihat pada dataset, bukan jumlah saham yang secara pasti masih dipegang broker/nasabah broker.

## Fitur

- Simpan broker summary harian ke SQLite
- Hitung Buy Lot, Sell Lot, Net Lot per broker
- Estimasi weighted average buy / sell
- Scan 5 / 10 / 20 hari
- Score 0–100 untuk kekuatan accumulation/distribution
- Bandingkan harga terakhir terhadap average buy broker
- Ranking saham watchlist
- Format pesan Telegram + fungsi send Telegram
- Database tetap tersimpan sehingga scan berikutnya bisa melanjutkan histori

## Setup

1. Buat virtual environment:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

2. Install dependency:

```bash
pip install -r requirements.txt
```

3. Salin `.env.example` menjadi `.env` lalu isi:

```env
ZPI_API_KEY=zpi_xxxxxxxxxxxxxxxxxxxxxxxx
DB_PATH=broker_tracker.db
```

4. Jalankan notebook `broker_tracker.ipynb`.

## Alur yang direkomendasikan

### 1. Inisialisasi database

```python
from tracker import *
init_db()
```

### 2. Isi histori broker harian

Contoh:

```python
watchlist = ["BBRI", "BBCA", "BMRI", "TLKM", "ADRO"]

collect_watchlist(
    watchlist,
    start_date="2026-08-20",
    end_date="2026-09-18"
)
```

### 3. Scan akumulasi

```python
result = scan_watchlist(
    watchlist,
    end_date="2026-09-18",
    lookbacks=(5, 10, 20)
)

result
```

### 4. Broker dominan saham tertentu

```python
top_brokers("BBRI", end_date="2026-09-18", lookback_calendar_days=20)
```

## Arti kolom utama

- `net_lots`: total lot buy minus sell dari broker yang terlihat.
- `avg_buy_price`: weighted average berdasarkan total buy value / buy shares.
- `accumulation_ratio`: `net_lots / (buy_lots + sell_lots)`, rentang -1 sampai +1.
- `score`: skor indikatif, bukan sinyal beli otomatis.
- `price_vs_avg_buy_pct`: posisi harga sekarang relatif terhadap average buy broker.

## Label score

- `>= 70` Strong Accumulation
- `57–69.99` Accumulation
- `43.01–56.99` Neutral
- `30.01–43` Distribution
- `<= 30` Strong Distribution

Ambang ini sengaja dibuat configurable; sebaiknya divalidasi dengan backtest sebelum dipakai sebagai sinyal entry.

## Catatan penting

1 lot saham IDX = 100 saham.

API dapat membatasi broker summary pada broker teratas, sehingga jangan menafsirkan cumulative net lot sebagai saldo custody sesungguhnya.

Untuk mengurangi bias:
- gunakan saham likuid,
- bandingkan 5/10/20 hari,
- lihat konsistensi broker dominan,
- kombinasikan dengan harga, volume, frequency, foreign flow, dan trend,
- backtest score sebelum dipakai untuk keputusan trading.
