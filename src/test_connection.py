"""
Skrip Uji Koneksi & Smoke Test Odoo 19 & Odoo 20 JSON-2 API
Membaca konfigurasi dari config.json dan melakukan validasi auth & search_read.
"""
import json
import os
import sys

# Tambahkan src ke sys.path jika dijalankan langsung
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from odoo_client import OdooClient, OdooAPIError


def load_config(config_path: str = "config.json") -> dict:
    if not os.path.exists(config_path):
        print(f"❌ File {config_path} tidak ditemukan!")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_instance(name: str, cfg: dict) -> bool:
    print(f"\n{'='*55}")
    print(f"🔍 Testing {name}")
    print(f"   URL: {cfg.get('url')} | Database: {cfg.get('db')}")
    print(f"{'='*55}")

    api_key = cfg.get("api_key")
    if not api_key or "PUT_YOUR" in api_key or "YOUR_ODOO" in api_key:
        print(f"⚠️  API Key belum diisi untuk {name} di config.json!")
        return False

    client = OdooClient(
        url=cfg["url"],
        db=cfg["db"],
        api_key=api_key,
        endpoint=cfg.get("endpoint", "/json/2"),
        timeout=15
    )

    try:
        # Test 1: Baca res.company
        companies = client.search_read(
            "res.company",
            domain=[],
            fields=["id", "name", "currency_id"],
            limit=3
        )
        print(f"✅ [1/2] Berhasil query res.company! Ditemukan {len(companies)} company:")
        for c in companies:
            print(f"       - ID {c['id']}: {c['name']} (Currency: {c.get('currency_id')})")

        # Test 2: Baca res.users
        users = client.search_read(
            "res.users",
            domain=[("active", "=", True)],
            fields=["id", "name", "login"],
            limit=3
        )
        print(f"✅ [2/2] Berhasil query res.users! User aktif:")
        for u in users:
            print(f"       - ID {u['id']}: {u['name']} ({u['login']})")

        print(f"🎉 Koneksi ke {name} BERHASIL & SIAP PAKAI!")
        return True

    except OdooAPIError as e:
        print(f"❌ Odoo API Error (HTTP {e.status_code}): {e}")
        if e.details:
            print(f"   Detail respons: {e.details}")
        return False
    except Exception as e:
        print(f"❌ Error Tak Terduga: {e}")
        return False


def main():
    config = load_config()
    results = {}

    if "source_odoo_19" in config:
        results["Odoo 19"] = test_instance("Source Odoo 19.0", config["source_odoo_19"])

    if "target_odoo_20" in config:
        results["Odoo 20"] = test_instance("Target Odoo 20.0", config["target_odoo_20"])

    print(f"\n{'='*55}")
    print("📋 RINGKASAN HASIL TEST KONEKSI")
    print(f"{'='*55}")
    all_ok = True
    for inst, ok in results.items():
        status = "✅ SIAP" if ok else "❌ GAGAL / BELUM SIAP"
        print(f"  {inst:15} : {status}")
        if not ok:
            all_ok = False

    print(f"{'='*55}")
    if all_ok and len(results) >= 2:
        print("🚀 Semua server siap! Kita bisa melangkah ke Fase 2 (Master Data).")
    else:
        print("💡 Periksa pesan error di atas sebelum melanjutkan.")


if __name__ == "__main__":
    main()
