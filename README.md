
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


## FOREIGN ENTRY — foreign accumulation + entry score

Perintah Telegram baru:

| Perintah | Hasil |
| --- | --- |
| `ADRO FOREIGN ENTRY` | Pemeriksaan satu saham, default SWING |
| `ADRO FOREIGN ENTRY DAYTRADE` | Pemeriksaan dengan parameter DAYTRADE existing, tetap data harian |
| `FOREIGN ENTRY` | Periksa semua saham watchlist, tampilkan maksimal 10 saham lolos dengan skor tertinggi |
| `FOREIGN ENTRY DAYTRADE` | Scan watchlist dengan parameter DAYTRADE |

Syarat lolos bersamaan:

1. Nilai net foreign aktual 5 observasi harga harian terakhir > 0.
2. Minimal 3 dari 5 observasi tersebut net buy dan observasi terakhir net buy.
3. Skor entry existing >= 70, filter entry `PASS`, R:R TP1 >= 1,50, ATR positif.
4. Minimal 50 observasi harga valid; tanggal terakhir sama dengan tanggal aman
   existing (cutoff 18:00 WIB, mundur akhir pekan). Broker/foreign/harga memakai
   tanggal acuan yang sama. Foreign harus tersedia untuk kelima observasi.

`FOREIGN_ENTRY_MIN_SCORE` dapat diatur melalui environment (default 70).
Nilai foreign memakai `foreignBuyValue`/`foreignSellValue` atau `netForeignValue`
(dan alias PascalCase). Jika pasangan buy/sell dan net sama-sama ada, nilainya
harus konsisten. Field kosong tidak dianggap nol. Shares × close tidak dipakai
sebagai konfirmasi. Jika upstream hanya menyediakan saham/estimasi, status tidak
lolos sampai data nilai aktual tersedia.

Status lolos menyertakan rencana entry, invalidasi/stop acuan, TP1/TP2 dan R:R.
Level masih indikatif dari entry engine, belum dibulatkan ke fraksi harga; biaya
transaksi belum dihitung. Status belum lolos menyebutkan penyebabnya. Scan
melaporkan jumlah gagal diperiksa dan ticker error secara terpisah.

Fitur ini dipanggil melalui query; tidak menambahkan jadwal kiriman atau eksekusi
order. Satu request stock-history per ticker digunakan bersama oleh foreign dan
entry engine. Scan watchlist membutuhkan satu request tersebut untuk setiap
saham, dengan tambahan retry bila koneksi upstream terganggu. Broker menggunakan
histori SQLite existing; bila belum lengkap, lakukan refresh lewat perintah
existing sebelum mengecek kembali.

Ambang ini adalah aturan penyaringan awal, belum hasil backtest. “Confirmed”
berarti lolos aturan bot, bukan jaminan kenaikan. Hari libur bursa resmi belum
terintegrasi: pemeriksaan konservatif dapat menahan sinyal saat tanggal acuan
jatuh pada hari libur. Lima observasi terakhir juga belum diaudit terhadap
kalender bursa lengkap. Jangan memperlakukan mode DAYTRADE berbasis candle
harian sebagai konfirmasi harga atau volume real-time.


## Gabungan aturan Bot 2

- `FOREIGN ENTRY BOT2`: scan universe stock-summary API pada tanggal aman.
- `ADRO FOREIGN ENTRY BOT2`: periksa ranking/koreksi satu saham, lalu filter entry.
- `FOREIGN ENTRY BOT2 REFRESH`: paksa ambil ulang snapshot tanggal aman dari Zapi.
- Tambahkan `DAYTRADE` jika ingin parameter entry DAYTRADE existing.

Aturan berdasarkan konfigurasi Bot 2 terakhir dalam pembahasan: Top 50 frequency,
Top 30 net foreign buy positif, koreksi open→close antara -5% dan -0,3% (inklusif).
Koreksi dihitung `(close / open - 1) * 100`, bukan dari previous close atau high.
Ranking dihitung sebelum filter koreksi terhadap universe API pada tanggal sama.
Tie diurutkan alfabetis sehingga jumlah Top N tetap N.

Semua syarat FOREIGN ENTRY tetap wajib: foreign 5D aktual terkonfirmasi menurut
aturan bot, skor >=70 secara default, entry PASS, R:R >=1,5, dan data valid.
Ranking dapat memakai estimasi `(ForeignBuy - ForeignSell) * close` bila summary
hanya menyediakan shares; ini ditandai di pesan. Estimasi ranking tidak mengganti
syarat nilai foreign aktual 5D dari stock-history. Harga summary dan history harus
cocok. Jika tanggal, pagination, frequency atau foreign universe tidak valid,
ranking ditahan. Universe API tidak diklaim identik dengan ranking Stockbit.

Scan meminta hingga 5.000 baris stock-summary per request (batas endpoint),
berhenti setelah total baris yang dilaporkan API tercapai atau halaman pendek.
Batas pengaman tetap 20 halaman. Snapshot lengkap disimpan secara atomik di
SQLite (`stock_summary_snapshots` dan `stock_summary_daily`) bersama payload asli
serta kolom harga, volume, value, frequency, dan foreign yang dinormalisasi.
Tabel `stock_daily` existing juga diperbarui dari respons yang sama. Pemanggilan
ulang untuk tanggal yang sama membaca snapshot database sehingga tidak memakai
request stock-summary Zapi tambahan. Gunakan `REFRESH` hanya saat ingin memaksa
pengambilan ulang; respons baru mengganti snapshot tanggal tersebut hanya setelah
seluruh baris lolos validasi. Snapshot mulai terkumpul setelah fitur ini aktif dan
tidak melakukan backfill tanggal lama secara otomatis. Setelah itu bot memanggil
stock-history hanya untuk irisan kandidat Top50/Top30/koreksi (maksimal 30 saham).
Data broker kandidat tetap membutuhkan histori SQLite existing. Kandidat baru
bisa belum lolos karena histori broker belum dikumpulkan. Hasil menampilkan
maksimal 10 saham dengan skor tertinggi; error tidak dianggap sinyal kosong.
Bot 2 terpisah tidak diubah. Tidak menambah jadwal kiriman maupun auto-order.
Belum diverifikasi dengan payload API live; validasi ketat bisa menahan hasil
jika upstream tidak menyediakan field tanggal atau nilai yang diperlukan.


## Broker YTD Matrix — Top 3 dua arah (Telegram)

Perintah baru Bot 1 (read-only, memakai `broker_tracker.db`):

| Command | Hasil |
| --- | --- |
| `BROKER TOP BBRI YTD` | 3 broker net-buy terbesar dan 3 broker net-sell terbesar di BBRI sepanjang tahun berjalan, total nominal dan kontribusi broker |
| `CC NI TOP YTD` | 3 saham dengan net-buy value terbesar oleh broker NI sepanjang YTD |
| `CC NI TOP SELL YTD` | 3 saham dengan net-sell value terbesar oleh broker NI sepanjang YTD |

Hitungan: `net_value = SUM(BUY.value) - SUM(SELL.value)` per pasangan saham–broker,
dengan satuan rupiah dan periode Januari 1 sampai tanggal terakhir yang aman
(`safe_end_date` Bot 1). Top Buy hanya nilai **positif**, Top Sell hanya nilai
**negatif**. Total Top 3 adalah penjumlahan nilai net masing-masing anggota
ranking, **bukan** penjumlahan gross buy. Ties diurutkan berdasarkan kode.

Ranking berangkat dari **semua ticker yang sudah ada di `broker_daily`**, bukan
hanya watchlist, dan **bukan** seluruh ticker IDX kecuali data seluruh ticker
sudah dikoleksi. Cakupan YTD ditampilkan terhadap tanggal referensi yang
*terlihat di cache*, termasuk snapshot EOD lokal jika ada; referensi tersebut
bukan kalender perdagangan resmi. Status `PARTIAL` menandakan gap/stale/awal
tahun tidak tercakup; `UNVERIFIED` bukan klaim data lengkap.

Dataset broker-summary upstream hanya menampilkan broker teratas sehingga
broker yang tidak muncul di suatu snapshot **tidak otomatis berarti nol transaksi**.
Akumulasi YTD **tidak setara** posisi saham yang masih disimpan broker.
Command tidak membaca atau menyinkronkan data broker dari Neon secara implisit;
arsip EOD `stock-summary` Neon saja tidak cukup untuk menghasilkan broker
history YTD. Jika data kosong, periksa `/status TICKER` dan pertimbangkan
`/refresh TICKER` (perintah refresh dapat memakai kuota ZAPI).

### Weighted Avg Price di semua ranking Top 3

Setiap baris ranking sekarang menyertakan **WAvg Buy** pada sisi Net Buy dan
**WAvg Sell** pada sisi Net Sell. Per broker-saham, rata-rata *per lembar*
berasal dari YTD sisi transaksi yang sesuai, bukan rata-rata sederhana harian:

- WAvg Buy = total nilai BUY terlihat ÷ (total lot BUY terlihat × 100).
- WAvg Sell = total nilai SELL terlihat ÷ (total lot SELL terlihat × 100).

Contoh: bila broker melakukan BUY Rp 115 juta atas 500 lot, WAvg Buy
Rp 2.300 per saham. Transaksi SELL selama periode itu tetap mengurangi
**ranking Net Buy Value**, tetapi tidak dipakai untuk menghitung WAvg Buy.
Karena itu WAvg Buy **bukan** harga pokok posisi net saham yang masih dimiliki
broker, dan tidak otomatis sama dengan harga rata-rata net akumulasi.
Jika nilai/lot sisi relevan tidak valid atau nol, tampilkan `N/A`, bukan
angka 0 atau pembagian dengan nol. Sumber broker-summary upstream tidak
selalu mencakup semua broker, maka hasil dibatasi pada aktivitas terlihat.

Pengiriman command baru dilewatkan melalui `broker_ytd_dispatch.py` di
workflow `telegram_query.yml`; command existing tetap memakai handler lama,
termasuk authorization dan de-duplication. Query Top 3 sendiri tidak membuat
request ZAPI maupun jadwal scanning tambahan. Setiap command Telegram tetap
menggunakan satu run GitHub Actions seperti perintah Bot 1 lainnya.

## Persistensi broker 5/10/20 sesi

Command `BROKER PERSIST BBRI` membaca cache SQLite dan menampilkan maksimal tiga
broker net buy terbesar pada jendela 20 sesi, beserta jumlah hari net buy/net
sell, rentetan net buy terakhir, net value, perbandingan aktivitas terbaru dengan
YTD, dan intensitas terhadap nilai transaksi saham pada tanggal yang sama.

Broker yang tidak muncul pada broker summary harian diperlakukan sebagai data
tidak diketahui, bukan nol transaksi. Jendela yang kurang lengkap diberi status
`PARTIAL`, sedangkan intensitas hanya dihitung jika seluruh tanggal dan turnover
tersedia. Fitur ini terinspirasi penelitian persistensi order flow Tóth dkk.
(2015), tetapi merupakan adaptasi harian dan bukan replikasi penelitian intraday.
Command hanya membaca cache: tidak menambah panggilan ZAPI, jadwal, skor entry,
atau pengiriman otomatis.

Baris historis legacy yang memiliki nilai, lot, atau average price negatif/tidak
valid dikarantina saat membaca. Seluruh pasangan broker-tanggal yang terdampak
dikeluarkan agar sisi transaksi yang tersisa tidak menciptakan net palsu. Laporan
tetap ditampilkan dengan peringatan `DATA QUALITY` dan `YTD PARTIAL`; command
tidak mengubah atau menghapus database sumber.

Output `BROKER PERSIST` menampilkan `Avg Buy` YTD dan 5/10/20 sesi. Nilainya
adalah weighted average harga beli yang terlihat (`buy value / buy lots / 100`),
bukan harga modal dari posisi saham yang masih dimiliki broker. Ringkasan
Telegram dibuat singkat: hari net buy/net sell, net value, Avg Buy, cakupan data,
dan status kualitas.
