# Jadwal Sholat Kemenag Scraper

Scraper untuk jadwal sholat dari API Bimas Islam Kementerian Agama
(<https://bimasislam.kemenag.dev/jadwal-sholat>). Zero dependency — cukup
Python 3.9+ (`urllib` stdlib saja).

Tiga endpoint API yang dipakai:

| Endpoint | Fungsi |
| --- | --- |
| `GET /api/sihat/provinsi` | daftar provinsi (38) |
| `GET /api/sihat/kabko?prov=<id>` | daftar kabupaten/kota per provinsi |
| `GET /api/sihat/jadwal?prov=&kabko=&thn=&bln=` | jadwal 1 bulan untuk 1 kabko/kota |

`id` provinsi/kabko adalah token terenkripsi yang di-pass-through apa adanya
(dengan percent-encoding yang benar untuk `+`, `/`, `=`).

## Penggunaan

```bash
# Scrap nasional beberapa tahun sekaligus (2026-2070, resumable)
python3 scraper.py scrape --year 2026-2070

# Scrap per provinsi / per kabko / per bulan
python3 scraper.py scrape --year 2026 --prov aceh
python3 scraper.py scrape --year 2026-2070 --prov "dki jakarta" --kabko "kepulauan seribu"
python3 scraper.py scrape --year 2026 --months 1,6,12

# Bangun database SQLite dari cache mentah (tanpa network)
python3 scraper.py db                 # -> data/jadwal.db (upsert)
python3 scraper.py db --rebuild       # drop + bangun ulang

# Lihat provinsi/kabko yang sudah ter-cache
python3 scraper.py list
python3 scraper.py list --kabko bogor

# Export semua jadwal yang sudah ter-scrape jadi satu file
python3 scraper.py export --format csv    # -> data/jadwal.csv
python3 scraper.py export --format jsonl  # -> data/jadwal.jsonl
```

## Opsi `scrape`

| Opsi | Default | Keterangan |
| --- | --- | --- |
| `--year` | tahun sekarang | `2026`, `2026-2070` atau `2026,2030` |
| `--months` | `1-12` | `1-3` atau `1,6,12` |
| `--prov` | semua | filter nama provinsi (substring, case-insensitive) |
| `--kabko` | semua | filter nama kabupaten/kota |
| `--out` | `data` | direktori output |
| `--workers` | 4 | jumlah request paralel |
| `--delay` | 0.25 | jarak minimum antar request (detik) agar sopan ke server |
| `--retries` | 4 | percobaan ulang per request (backoff eksponensial) |
| `--timeout` | 30 | timeout request (detik) |
| `--force` | off | abaikan cache, fetch ulang |

## Layout output

```
data/
  provinsi.json                                  cache daftar provinsi
  kabko/<prov>.json                              cache daftar kabko per provinsi
  raw/<tahun>/<prov>/<kabko>/<MM>.json           respons API mentah per bulan
  schedules/<tahun>/<prov>/<kabko>.json          jadwal gabungan setahun (atau sesuai --months)
  errors-<tahun>.json                            daftar kegagalan run terakhir
  jadwal.db                                      store SQLite (dibangun via `db`)
```

## SQLite (`jadwal.db`)

`python3 scraper.py db` membangun database dari cache mentah — tidak
menyentuh network, idempotent (upsert), dan bisa dijalankan kapan saja
sebelum/selepas/saat scrape berjalan. Skema:

```sql
locations (id, prov, kabko, lat, lon, prov_token, kabko_token, UNIQUE(prov, kabko))
schedules (location_id, date, imsak..isya, PRIMARY KEY (location_id, date))
```

Contoh query:

```sql
-- jadwal hari ini untuk satu kota
SELECT s.* FROM schedules s JOIN locations l ON l.id = s.location_id
WHERE l.kabko = 'KOTA JAKARTA' AND s.date = '2026-01-25';
```

- **Resumable** — response mentah per bulan di-cache; jalankan ulang perintah
  yang sama dan hanya bulan yang belum ada yang di-fetch. Ctrl-C kapan pun,
  lanjutkan dengan menjalankan ulang.
- Setiap file `schedules/*.json` berisi `prov`, `kabko`, `lintang`/`bujur`
  (string asli), `lat`/`lon` (decimal hasil parsing), `months`, dan `data`
  dengan key tanggal `YYYY-MM-DD` berisi `imsak, subuh, terbit, dhuha,
  dzuhur, ashar, maghrib, isya`.
- Catatan: API Kemenag memang hanya menulis 2 entry untuk DKI Jakarta
  (`KOTA JAKARTA` dan `KAB. KEPULAUAN SERIBU`).

## Perkiraan durasi & ukuran

- Nasional 1 tahun (~514 kabko × 12 bulan ≈ 6.2k request): default
  (`--workers 4 --delay 0.25`, ~3 req/detik) ≈ 35–45 menit.
- 2026–2070 (45 tahun, ≈ 278k request): default ≈ 26 jam; dengan
  `--workers 8 --delay 0.1` (≈ 6–7 req/detik) ≈ 11–13 jam.
- Disk: ~2.5–3 GB cache `raw/` JSON + ~600 MB `jadwal.db` untuk 45 tahun.
- Semuanya resumable — Ctrl-C kapan pun, jalankan ulang perintah yang sama
  untuk melanjutkan dari cache.
