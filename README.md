# Odoo 19.0 -> 20.0 Accounting Migration Studio (Pure API JSON-2)

Alat migrasi mandiri (*standalone migration studio*) untuk memigrasikan seluruh riwayat data akuntansi (**Accounting Enterprise**) dari **Odoo 19.0** ke **Odoo 20.0** secara bertahap, aman, dan terverifikasi menggunakan **100% Pure External API JSON-2** (`/json/2`).

Tersedia dua mode eksekusi yang memakai **engine yang sama** (`src/migrate_pure_api.py`):

* **Web Migration Studio** (FastAPI + SSE) — antarmuka interaktif dengan live log, progress bar, dan panel audit.
* **CLI** — eksekusi langsung per fase via `uv run`.

---

## 1. Latar Belakang & Keputusan Arsitektur

* **Source (Odoo 19.0 Enterprise):** Database kotor penuh residu modul rusak dan testing. *In-place upgrade* dihindari agar versi baru tetap bersih dan stabil.
* **Target (Odoo 20.0 Enterprise):** Fresh clean installation (database baru dan steril).
* **Cakupan Penuh (Full Historical Migration):** Bukan sekadar cut-off saldo awal, melainkan memigrasikan seluruh riwayat jurnal masa lalu, rekening koran/mutasi kas & bank, hingga status rekonsiliasinya.
* **100% Pure API JSON-2:** Seluruh proses baca-tulis menggunakan endpoint resmi Odoo `/json/2` (tanpa XML-RPC/JSON-RPC lama, dan tanpa manipulasi SQL langsung ke database PostgreSQL).
* **Universal & Standalone:** Dapat memigrasikan data antar server mana pun (Cloud/VPS ke Cloud/VPS, On-Premise/LAN, maupun simulasi lokal).
* **Idempoten & Tahan Gagal:** Setiap record yang berhasil dipetakan dicatat di SQLite, sehingga fase dapat dijalankan ulang tanpa duplikasi. Kegagalan satu record tidak menghentikan fase — dicatat per-record untuk diaudit.
* **Tooling Python:** Wajib menggunakan **`uv`** (Python >= 3.12) untuk eksekusi, environment terisolasi, dan dependensi proyek (`fastapi`, `uvicorn`, `jinja2`, `requests`).

---

## 2. Cakupan Model & Alur Migrasi

Penomoran fase internal engine adalah 2–5 (Fase 1 = setup & mapping). Di UI Web Studio, tombol eksekusi diberi label 1–4 sesuai urutan operasional.

| Fase internal | Tombol UI | Model Odoo | Keterangan & Solusi Teknis |
| :---: | :---: | :--- | :--- |
| **1** | — | Setup & Mapping | `IdMapper` (SQLite) menyimpan relasi ID lama -> ID baru per model, plus tabel `migration_errors`. Nama file database dibuat otomatis dari kombinasi host/port/db source & target. |
| **2** | **1. Master Data** | `res.company`<br>`res.currency`<br>`account.tax.group`<br>`account.tax`<br>`account.account`<br>`account.journal`<br>`res.partner`<br>`account.analytic.plan`<br>`account.analytic.account` | Sinkronisasi nama perusahaan & mata uang (termasuk mengaktifkan mata uang nonaktif di target), grup pajak, aturan pajak, bagan akun (CoA) + nama akun, jurnal, kontak rekanan, serta plan & akun analitik. Jurnal placeholder bawaan (bank/cash tak terpakai) dinonaktifkan agar dashboard bersih. |
| **3 & 4** | **2. Transaksi Move** | `account.bank.statement.line`<br>`account.move`<br>`account.move.line` | **Rute A (Kas/Bank):** Dibuat via `account.bank.statement.line` agar mutasi rekening koran, `internal_index`, `sequence`, dan status rekonsiliasi dihitung native oleh Odoo 20. Nama dokumen asli ditulis ulang setelah move terbentuk.<br>**Rute B (Jurnal Umum):** Dibuat via `account.move`, lalu `action_post` bila sumber berstatus posted, dan nama dokumen asli dipulihkan.<br>Relasi `tax_ids`, `tax_line_id`, `date_maturity`, `currency`/`amount_currency`, dan `analytic_distribution` ikut dipulihkan per baris. |
| **5** | **3. Rekonsiliasi** | `account.partial.reconcile` -> `account.move.line.reconcile` | Partial reconcile dikelompokkan per `full_reconcile_id` (mendukung rekonsiliasi 3+ baris, mis. satu invoice dibayar bertahap), lalu direkonsiliasi sekali per grup via method native Odoo. Status "already reconciled" dihitung terpisah, bukan error. |
| — | **4. Audit & Verifikasi** | `account.move.line`<br>`account.journal` | Hanya baris berstatus `posted`, diagregasi **per kode akun** (bukan total tenant) agar selisih antar-akun tidak saling meniadakan. Kode akun diambil dari `account.account` (bukan parsing `display_name`, karena Odoo 20 tidak menampilkan kode di nama). Toleransi selisih `Rp 0,01`. |

### Pemulihan otomatis (auto-adoption)

Sebelum membuat dokumen baru, engine memeriksa apakah nomor dokumen sudah ada di Odoo 20:

* **Diadopsi** (tidak dibuat ulang) hanya bila **nama + jurnal + tanggal** identik dan **total debit/credit** cocok dengan sumber.
* Bila nama sama tetapi jurnal/tanggal berbeda, dokumen diperlakukan sebagai move baru (dengan peringatan di log).
* Bila total debit/credit hasil adopsi berbeda dari sumber, adopsi **ditolak** dan dicatat sebagai error (tidak menebak mapping baris).

### Ketahanan terhadap rate limit & gangguan jaringan

`OdooClient` melakukan retry otomatis hingga **6 percobaan**:

* **HTTP 429** — mengikuti header `Retry-After` bila ada, jika tidak memakai exponential backoff (maks. 60 detik).
* **HTTP 502/503/504** dan error koneksi — backoff linear lalu dicoba ulang.
* **Adaptive pacing** — bila target terdeteksi sebagai Cloud/remote (bukan `localhost`/`127.0.0.1`), setiap move diberi jeda ±0,12 detik.

---

## 3. Struktur Folder Proyek

```text
odoo-migration-19-to-20/
├── README.md                          # Dokumentasi utama proyek (arsitektur & panduan)
├── Dockerfile                         # Build image Web Migration Studio (uv + Python 3.12)
├── docker-compose.yml                 # Orkestrasi container Web Studio (host port 8088 -> 8080)
├── config.json                        # Konfigurasi aktif Odoo & proteksi web (di-ignore git)
├── config.example.json                # Template konfigurasi contoh
├── pyproject.toml                     # Definisi proyek & dependensi uv
├── uv.lock                            # Lockfile dependensi (dipakai saat build Docker)
├── data/                              # Database pemetaan relasi SQLite (*.db, di-ignore git)
├── logs/                              # Output log CLI bila diarahkan ke file (tidak ditulis otomatis oleh app)
└── src/
    ├── web/
    │   ├── app.py                     # Backend FastAPI: auth, config, migrate, SSE, audit
    │   ├── static/                    # Direktori aset statis (opsional, di-mount di /static)
    │   └── templates/
    │       ├── index.html             # Dashboard: konfigurasi, kontrol fase, konsol SSE, audit
    │       └── login.html             # Halaman login studio
    ├── odoo_client.py                 # Klien External API JSON-2 + retry/backoff
    ├── id_mapper.py                   # Pemetaan ID SQLite + log error per-record
    ├── migrate_pure_api.py            # Engine migrasi (master, transaksi, rekonsiliasi, verifikasi)
    ├── test_connection.py             # Smoke test koneksi & kredensial Odoo 19/20
    └── verify_trial_balance.py        # Audit trial balance per akun (tabel CLI)
```

> [!NOTE]
> Folder `docker-odoo19/` dan `docker-odoo20/` (sandbox Odoo lokal, port 1969 & 2069) tidak ikut di-commit — sudah masuk `.gitignore`. `config.json` beserta semua varian backup-nya (`config.json.*`), file `data/*.db`, dan `logs/*.log` juga di-ignore.

---

## 4. Cara Menjalankan

### Opsi A: Web Migration Studio (Docker Compose - Rekomendasi)

```bash
# 1. Siapkan konfigurasi (sekali saja)
cp config.example.json config.json
# lalu isi url/db/api_key pada config.json

# 2. Build & jalankan kontainer di latar belakang
docker compose up -d --build

# 3. Pantau log kontainer
docker compose logs -f

# 4. Matikan kontainer
docker compose down
```

* **URL Akses:** `http://localhost:8088` *(atau `http://<IP-host>:8088`)*
* **Port:** host `8088` -> container `8080`
* **Volume:** `./data` (SQLite mapping), `./config.json` (konfigurasi, read-write), `./logs`
* **Proteksi Login (default di source):**
  * **Username:** `admin`
  * **Password:** `odoomigrate2026`
  * Ubah pada blok `web_auth` di `config.json`, atau lewat environment variable (lihat bagian 6). Ganti password default sebelum dipakai di jaringan terbuka.

### Opsi B: CLI Eksekusi Langsung (via `uv`)

```bash
# Uji koneksi ke Odoo 19 & Odoo 20:
uv run python src/test_connection.py

# Jalankan seluruh fase migrasi sekaligus:
uv run python src/migrate_pure_api.py --phase all

# Atau jalankan bertahap per fase:
uv run python src/migrate_pure_api.py --phase master        # Fase 2: Master data
uv run python src/migrate_pure_api.py --phase transactions  # Fase 3 & 4: Transaksi dual-route
uv run python src/migrate_pure_api.py --phase reconcile     # Fase 5: Rekonsiliasi pembayaran
uv run python src/migrate_pure_api.py --phase verify        # Audit trial balance (default)

# Uji coba terbatas (mis. 100 move pertama) sebelum migrasi penuh:
uv run python src/migrate_pure_api.py --phase transactions --limit 100

# Audit perbandingan saldo akun Odoo 19 vs Odoo 20 (tabel per akun):
uv run python src/verify_trial_balance.py
```

| Argumen | Nilai | Keterangan |
| :--- | :--- | :--- |
| `--phase` | `all`, `master`, `transactions`, `reconcile`, `verify` | Fase yang dijalankan. Default `verify`. |
| `--limit` | bilangan bulat | Membatasi jumlah `account.move` yang diproses (untuk uji coba). |

> [!TIP]
> Saat dijalankan di dalam Docker dan URL berisi `localhost`/`127.0.0.1`, klien otomatis mengalihkannya ke `host.docker.internal` (didukung `extra_hosts` pada compose) agar bisa menjangkau Odoo di host machine. Untuk remote server (VPS/Cloud/LAN), gunakan URL domain atau IP secara langsung.

---

## 5. Web Migration Studio

### Panel antarmuka (`/`)

1. **Konfigurasi Server Odoo** — kartu Source (Odoo 19) dan Target (Odoo 20) berisi URL, nama database, dan API Key (Bearer), lengkap dengan tombol **Test Koneksi** per server (menampilkan nama company, mata uang, dan user `res.users` ID 2).
2. **Panel Eksekusi** — tombol **Jalankan Full Migrasi (All-in-One)** dan eksekusi modular: `1. Master Data`, `2. Transaksi Move`, `3. Rekonsiliasi`, `4. Audit & Verifikasi`. Progress bar menampilkan persentase dan tahap berjalan; tombol dinonaktifkan selama migrasi berlangsung.
3. **Live Migration Console (SSE)** — log streaming real-time dengan pewarnaan per level, opsi auto-scroll, bersihkan, dan salin log. Riwayat log disimpan di memori maksimal 1000 baris (150 baris terakhir dikirim ulang saat klien terhubung).
4. **Hasil Audit & Verifikasi Buku Besar** — kartu Trial Balance Debit/Credit (beserta selisih), badge status keseimbangan, dan tabel perbandingan saldo jurnal kas & bank Odoo 19 vs 20. Panel otomatis dimuat ulang setelah migrasi selesai.

### Endpoint API

| Method | Path | Fungsi |
| :--- | :--- | :--- |
| `GET` | `/login` | Halaman login (redirect ke `/` bila sudah terautentikasi). |
| `POST` | `/api/login` | Login, mengeset cookie `odoo_migration_token` (HttpOnly, SameSite=Lax, 7 hari). |
| `POST` | `/api/logout` | Menghapus cookie sesi. |
| `GET` | `/` | Dashboard studio. |
| `GET` | `/api/config` | Konfigurasi aktif (API key **disamarkan**) + path/nama file SQLite mapping. |
| `POST` | `/api/config` | Simpan konfigurasi koneksi. API key yang dikirim kosong/samaran akan mempertahankan nilai tersimpan. |
| `POST` | `/api/test-connection` | Uji kredensial satu server dan kembalikan info company/user. |
| `POST` | `/api/migrate` | Mulai migrasi di background thread (`phase`, opsional `limit`). Ditolak (400) bila masih ada proses berjalan. |
| `GET` | `/api/status` | Status: `is_running`, `state`, `stage`, `progress`, `log_count`. |
| `GET` | `/api/stream` | Stream Server-Sent Events (init state + log/progress/status). |
| `GET` | `/api/verify` | Data audit: trial balance per akun, saldo jurnal kas/bank, jumlah move & statement line. |

---

## 6. Konfigurasi & Environment Variable

### `config.json`

```json
{
  "source_odoo_19": {
    "url": "http://localhost:1969",
    "db": "odoo19_production",
    "api_key": "YOUR_ODOO_19_API_KEY",
    "endpoint": "/json/2"
  },
  "target_odoo_20": {
    "url": "http://localhost:2069",
    "db": "odoo20_clean",
    "api_key": "YOUR_ODOO_20_API_KEY",
    "endpoint": "/json/2"
  },
  "web_auth": {
    "enabled": true,
    "username": "admin",
    "password": "CHANGE_ME"
  },
  "migration_map_db": "migration_map_custom.db"
}
```

| Key | Wajib | Keterangan |
| :--- | :---: | :--- |
| `source_odoo_19` / `target_odoo_20` | Ya | `url`, `db`, `api_key` (Bearer token). `endpoint` default `/json/2`. |
| `web_auth.enabled` | Tidak | Default `true`. Set `false` untuk menonaktifkan login (hanya untuk lingkungan lokal/terpercaya). |
| `web_auth.username` / `password` | Tidak | Default `admin` / `odoomigrate2026`. Bisa ditimpa environment variable. |
| `migration_map_db` | Tidak | Nama file SQLite kustom. Bila kosong, nama dibuat otomatis: `<host_src>_<port_src>_<db_src>__<host_tgt>_<port_tgt>_<db_tgt>.db` di folder `data/`. |

### Environment variable

| Variable | Fungsi |
| :--- | :--- |
| `WEB_USERNAME` | Menimpa username login web. |
| `WEB_PASSWORD` | Menimpa password login web. |
| `WEB_AUTH_SECRET` | Secret untuk menandatangani token cookie. Bila tidak diset, secret acak dibuat per-proses sehingga sesi login hangus setiap restart. |
| `RUNNING_IN_DOCKER` | Menandai proses berjalan di Docker agar `localhost`/`127.0.0.1` dialihkan ke `host.docker.internal`. Sudah diset `1` pada `docker-compose.yml`. |
| `PORT` | Port internal container (default compose: `8080`). |

> [!IMPORTANT]
> `config.example.json` masih berisi placeholder `YOUR_ODOO_19_API_KEY` / `YOUR_ODOO_20_API_KEY`. `src/test_connection.py` akan menolak key yang masih mengandung `PUT_YOUR`/`YOUR_ODOO`, jadi isi key asli terlebih dahulu.

---

## 7. Keamanan Akses Web

* **Login wajib** — semua path dilindungi middleware kecuali `/login`, `/api/login`, dan `/static/*`. Request ke `/api/*` tanpa sesi mengembalikan `401`, request halaman diarahkan ke `/login`.
* **Token cookie** — SHA-256 dari `username:password:secret`, dibandingkan dengan `hmac.compare_digest`, diset `HttpOnly`, `SameSite=Lax`, dan `Secure` bila diakses via HTTPS.
* **Anti brute-force** — maksimal 5 kegagalan per IP dalam 5 menit; IP terkunci 60 detik dan menerima HTTP `429`.
* **Basic Auth fallback** — endpoint API dapat diakses tool/`curl` dengan header `Authorization: Basic ...` memakai kredensial web yang sama.
* **API key tidak bocor ke browser** — key yang ditampilkan di UI selalu disamarkan (`••••••••••••`); nilai samaran tidak pernah disimpan sebagai key asli.

---

## 8. Idempotensi, Pemulihan & Penanganan Error

Setiap database mapping SQLite berisi dua tabel:

* **`id_mapping`** — `(model, old_id) -> new_id` beserta `notes` dan `created_at`. Record yang sudah punya mapping dilewati pada eksekusi ulang, sehingga fase aman dijalankan berulang.
* **`migration_errors`** — `phase`, `model`, `old_id`, `message`, `created_at`. Error per-record dicatat lalu proses **lanjut** ke record berikutnya.

Inspeksi hasil migrasi:

```bash
# Jumlah record terpetakan per model
sqlite3 data/<nama-file>.db \
  "SELECT model, COUNT(*) FROM id_mapping GROUP BY model ORDER BY model;"

# Daftar kegagalan yang perlu ditindaklanjuti
sqlite3 data/<nama-file>.db \
  "SELECT phase, model, old_id, substr(message,1,120), created_at FROM migration_errors ORDER BY id;"
```

Catatan perilaku:

* Data master dicocokkan berdasarkan **kode akun**, **kode jurnal**, **nama grup pajak**, **nama + `type_tax_use` pajak**, dan **nama partner** (case-insensitive).
* Perubahan mata uang perusahaan di target dapat ditolak Odoo bila sudah ada jurnal; kondisi ini hanya memunculkan peringatan dan tidak menggagalkan fase.
* Kegagalan pemetaan baris jurnal (`pair_lines`) sengaja **menggagalkan move tersebut** (bukan menebak), karena mapping baris yang salah akan merusak rekonsiliasi.
* Rekonsiliasi bersifat idempoten: grup yang sudah terekonsiliasi dihitung sebagai `already`, bukan `failed`.

---

## 9. Audit & Verifikasi

**Di CLI (`--phase verify` / `src/verify_trial_balance.py`):**

* Hanya membaca `account.move.line` dengan `parent_state = 'posted'`.
* Mengagregasi debit/credit **per kode akun** pada kedua database, lalu membandingkannya satu per satu (toleransi `0,01`).
* Status `100% IDENTIK & SEIMBANG` hanya diberikan bila total debit, total credit, **dan** seluruh akun tidak memiliki selisih.

**Di Web Studio (`GET /api/verify`):**

* Trial Balance Debit/Credit + selisih, plus daftar kode akun yang tidak cocok.
* Saldo jurnal kas & bank dari `kanban_dashboard` masing-masing jurnal (dibandingkan sebagai angka, sehingga perbedaan simbol mata uang tidak dianggap selisih).
* Jumlah `account.move` dan `account.bank.statement.line` di kedua database.

---

## 10. Troubleshooting

| Gejala | Penyebab & Solusi |
| :--- | :--- |
| `API Error (401)` saat uji koneksi | API key salah/kedaluwarsa atau nama database tidak cocok dengan header `X-Odoo-Database`. |
| Log penuh `Rate Limit 429` | Server target membatasi laju request. Klien sudah retry otomatis; kurangi beban dengan `--limit` atau jalankan per fase. |
| `Mata uang perusahaan tidak bisa diubah` | Target sudah memiliki jurnal sehingga Odoo mengunci mata uang company. Peringatan ini aman diabaikan bila mata uang memang sudah sesuai. |
| Field API key di UI berisi titik-titik | Nilai sengaja disamarkan. Biarkan apa adanya bila tidak ingin mengubah key; mengetik nilai baru akan menyimpannya. |
| Login hangus setiap restart container | Set `WEB_AUTH_SECRET` agar token cookie stabil antar-restart. |
| Container tidak bisa menjangkau Odoo di host | Pastikan `extra_hosts: host.docker.internal:host-gateway` aktif (sudah ada di compose) dan `RUNNING_IN_DOCKER=1` terpasang. |
| Dokumen bernomor sama sudah ada di target | Engine mencoba adopsi bila nama + jurnal + tanggal cocok dan nominal sama; jika tidak, move dibuat baru dan peringatan dicatat. |
| Audit melaporkan selisih per akun | Periksa `migration_errors`, lalu pastikan akun/jurnal terkait sudah terpetakan di fase master sebelum transaksi dijalankan ulang. |
