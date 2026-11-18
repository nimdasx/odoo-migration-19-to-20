# Odoo 19.0 -> 20.0 Accounting Migration Studio (Pure API JSON-2)

Alat migrasi mandiri (*standalone migration studio*) untuk memigrasikan seluruh riwayat data akuntansi (**Accounting Enterprise**) dari **Odoo 19.0** ke **Odoo 20.0** secara bertahap, aman, dan terverifikasi menggunakan **100% Pure External API JSON-2** (`/json/2`).

---

## 1. Latar Belakang & Keputusan Arsitektur

* **Source (Odoo 19.0 Enterprise):** Database kotor penuh residu modul rusak dan testing. *In-place upgrade* dihindari agar versi baru tetap bersih dan stabil.
* **Target (Odoo 20.0 Enterprise):** Fresh clean installation (database baru dan steril).
* **Cakupan Penuh (Full Historical Migration):** Bukan sekadar cut-off saldo awal, melainkan memigrasikan seluruh riwayat jurnal masa lalu, rekening koran/mutasi kas & bank, hingga status rekonsiliasinya.
* **100% Pure API JSON-2:** Seluruh proses baca-tulis menggunakan endpoint resmi Odoo `/json/2` (tidak menggunakan XML-RPC/JSON-RPC lama, dan tanpa manipulasi SQL langsung ke database PostgreSQL).
* **Universal & Standalone:** Dapat memigrasikan data antar server mana pun (Cloud/VPS ke Cloud/VPS, On-Premise/LAN, maupun simulasi lokal).
* **Tooling Python:** Wajib menggunakan **`uv`** untuk eksekusi, environment terisolasi, dan dependensi proyek.

---

## 2. Cakupan Model & Alur Migrasi (5 Fase)

| Fase | Model Odoo | Entitas Data | Keterangan & Solusi Teknis |
| :---: | :--- | :--- | :--- |
| **1** | Setup & Mapping | `IdMapper` (SQLite) | Pemetaan otomatis relasi ID lama -> ID baru ke database unik SQLite (`<host_src>_<port_src>_<db_src>__<host_tgt>_<port_tgt>_<db_tgt>.db`). |
| **2** | `res.currency`<br>`account.tax`<br>`account.account`<br>`account.journal`<br>`res.partner` | Master Data Akuntansi | Sinkronisasi mata uang, aturan pajak, bagan akun (CoA), jurnal keuangan, dan kontak rekanan. |
| **3 & 4** | `account.move`<br>`account.bank.statement.line` | Transaksi Dual-Route | **Rute A (Kas/Bank):** Dibuat via `account.bank.statement.line` agar mutasi rekening koran dan nomor sequence jurnal kas/bank langsung sinkron di Odoo 20.<br>**Rute B (Jurnal Umum):** Dibuat via `account.move` dengan status posted dan nomor dokumen asli terjaga. |
| **5** | `account.partial.reconcile` | Rekonsiliasi & Audit | Menautkan baris debit-kredit via method native Odoo API dan memverifikasi keseimbangan neraca (Trial Balance diff = Rp 0,00). |

---

## 3. Cara Menjalankan

### Opsi A: Web Migration Studio (Docker Compose - Rekomendasi)
Studio web interaktif dilengkapi *live log streaming (SSE)*, pengujian koneksi instan, kontrol eksekusi per-fase, dan panel audit saldo *real-time*.

```bash
# 1. Jalankan kontainer di latar belakang
docker compose up -d

# 2. Pantau log kontainer
docker compose logs -f

# 3. Matikan kontainer
docker compose down
```

* **URL Akses:** `http://192.168.0.76:8088` *(atau `http://localhost:8088`)*
* **Proteksi Login (Default):**
  * **Username:** `admin`
  * **Password:** `odoomigrate2026`
  *(Dapat diubah pada blok `web_auth` di file `config.json`)*

---

### Opsi B: CLI Eksekusi Langsung (via `uv`)
Dapat dijalankan langsung dari terminal tanpa Docker:

```bash
# Jalankan seluruh fase migrasi sekaligus (All-in-One):
uv run python src/migrate_pure_api.py --phase all

# Atau jalankan bertahap per fase:
uv run python src/migrate_pure_api.py --phase master        # Fase 2: Master data
uv run python src/migrate_pure_api.py --phase transactions  # Fase 3 & 4: Transaksi dual-route
uv run python src/migrate_pure_api.py --phase reconcile     # Fase 5: Rekonsiliasi pembayaran
uv run python src/migrate_pure_api.py --phase verify        # Audit & Verifikasi Trial Balance

# Audit perbandingan saldo akun Odoo 19 vs Odoo 20:
uv run python src/verify_trial_balance.py
```

---

## 4. Konfigurasi (`config.json`)

```json
{
  "source_odoo_19": {
    "url": "http://localhost:1969",
    "db": "odoo19_production",
    "api_key": "YOUR_SOURCE_API_KEY",
    "endpoint": "/json/2"
  },
  "target_odoo_20": {
    "url": "http://localhost:2069",
    "db": "odoo20_clean",
    "api_key": "YOUR_TARGET_API_KEY",
    "endpoint": "/json/2"
  },
  "web_auth": {
    "enabled": true,
    "username": "admin",
    "password": "odoomigrate2026"
  }
}
```

> [!TIP]
> Saat dijalankan di dalam Docker, URL `localhost` atau `127.0.0.1` otomatis dialihkan ke `host.docker.internal` untuk menjangkau server Odoo di host machine. Untuk remote server (VPS/Cloud/LAN), gunakan URL domain atau IP publik/lokal secara langsung.

---

## 5. Struktur Folder Proyek

```text
odoo-migration-19-to-20/
├── README.md                          # Dokumentasi utama proyek (arsitektur & panduan)
├── Dockerfile                         # Build image Web Migration Studio
├── docker-compose.yml                 # Orkestrasi Docker container (Port 8088)
├── config.json                        # Konfigurasi aktif Odoo & proteksi web
├── config.example.json                # Template konfigurasi contoh
├── pyproject.toml                     # Definisi proyek & dependensi uv
├── data/                              # Direktori database pemetaan relasi SQLite (*.db)
├── logs/                              # Log riwayat eksekusi migrasi
└── src/
    ├── web/                           # Aplikasi Web Studio (FastAPI + SSE Stream)
    │   ├── app.py                     # Backend API, Proteksi Auth & Event Broadcaster
    │   └── templates/
    │       ├── index.html             # Antarmuka Dashboard & Console Studio
    │       └── login.html             # Halaman Login Studio
    ├── odoo_client.py                 # Klien resmi Odoo External API JSON-2
    ├── id_mapper.py                   # Modul pemetaan ID SQLite dinamis per database
    ├── migrate_pure_api.py            # Engine migrasi 100% Pure API JSON-2 (All-in-One)
    ├── migrate_master_data.py         # Modul migrasi master data (CoA, Tax, Journal, Partner)
    ├── migrate_transactions.py        # Modul migrasi entri jurnal transaksi
    └── verify_trial_balance.py        # Skrip audit perbandingan saldo buku besar
```
