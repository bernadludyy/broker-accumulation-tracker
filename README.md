
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

3. Buat file `.env` (jangan commit API key) lalu isi:

```env
ZPI_API_KEY=zpi_xxxxxxxxxxxxxxxxxxxxxxxx
DB_PATH=broker_tracker.db
```

4. Jalankan `python daily_runner.py` untuk update harian.
   Untuk query, jalankan `python telegram_query_bot.py --text "ADRO VALUE"`.
   Kedua jalur dapat mengirim pesan ke tujuan Telegram yang dikonfigurasi; isi
   `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID` melalui `.env` atau GitHub Secrets.

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



## Perbaikan integritas data dan entry scanner

- Snapshot broker harian divalidasi lalu diganti dalam satu transaksi. Broker yang
  tidak lagi muncul pada snapshot pengganti tidak tertinggal sebagai data lama.
  Respons kosong mempertahankan data yang ada; respons rusak ditolak. Snapshot
  rentang beberapa hari ditolak agar tidak dihitung sebagai satu hari.
- Entry scanner menggunakan satu pengambilan histori harga per analisis.
  Broker flow dibatasi sampai tanggal harga, menampilkan tanggal sebenarnya,
  dan menandai `NO DATA`, `INCOMPLETE`, atau `STALE` bila perlu. Kondisi tersebut
  tidak mendapat poin broker dan memaksa status tunggu. Filter tunggu juga berlaku
  pada breakout; skor tinggi tidak melewati filter ini.
- GET API mencoba ulang maksimal tiga kali untuk timeout/koneksi terputus,
  HTTP 429 dan HTTP 500/502/503/504. HTTP 401/403 langsung gagal.
- Pesan Telegram daily dan query dipecah menjadi bagian maksimal 3.500 unit UTF-16,
  termasuk baris panjang dan emoji. Setiap bagian harus dikonfirmasi `ok=true`.
  Pengiriman tidak otomatis diulang setelah timeout, untuk mengurangi duplikasi.
- Tanggal aman tidak melewati hari ini dan melewati akhir pekan, termasuk setelah
  cutoff. Ini belum merupakan kalender libur bursa resmi.

Pengujian offline (tanpa API key, tanpa kirim Telegram):

```bash
pip install -r requirements_github.txt
python -m unittest discover -s tests -v
```

Workflow **Bot regression tests** menjalankan tes pada pull request/perubahan kode.
Workflow produksi, tujuan Telegram, bobot skor, dan database historis tidak diubah
oleh paket perbaikan ini. Setelah merge, Actions berikutnya memakai kode baru.

### Batasan yang masih perlu diperhatikan

- Validasi kelengkapan entry scanner membandingkan jumlah observasi dan tanggal
  akhir; belum membuktikan tidak ada lubang tanggal di tengah periode. Data harga
  yang sama-sama lama dengan broker juga belum terdeteksi sebagai kedaluwarsa.
- Broker summary berisi broker teratas dan koleksi existing memakai `net=True`.
  Rasio value/lot dari data tersebut belum boleh diasumsikan sebagai harga beli
  bruto atau modal kepemilikan aktual tanpa verifikasi definisi upstream.
- Snapshot lama yang sudah terlanjur tercampur tidak otomatis diperbaiki; perlu
  pengambilan ulang tanggal terkait. Perbaikan ini berlaku saat snapshot disimpan lagi.
- Bila sebagian pesan berhasil terkirim sebelum bagian berikutnya gagal, pengulangan
  manual dapat mengirim ulang bagian awal. Belum ada antrean delivery per bagian.
- Proteksi duplikat query existing mengklaim update sebelum command selesai;
  kegagalan perlu perintah baru. Antrean query dan penyimpanan SQLite di GitHub
  perlu peningkatan terpisah untuk beban paralel lebih besar.

Tes regresi memverifikasi perilaku kode dengan data sintetis. Tes ini belum
mengukur win rate atau profitabilitas strategi dan belum merupakan uji API live.
