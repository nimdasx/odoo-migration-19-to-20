"""
Skrip Migrasi Rekonsiliasi Keuangan (Fase 5: Reconciliations)
Mencocokkan pelunasan invoice, bill, dan transfer internal bank di Odoo 20
berdasarkan data account.partial.reconcile dari Odoo 19.
"""
import json
import os
import sys
from collections import defaultdict

# Tambahkan src ke sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from odoo_client import OdooClient, OdooAPIError
from id_mapper import IdMapper


def load_config(config_path: str = "config.json") -> dict:
    if not os.path.exists(config_path):
        print(f"❌ File konfigurasi {config_path} tidak ditemukan!")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def migrate_reconciliations():
    print(f"{'='*60}")
    print("🚀 MEMULAI MIGRASI REKONSILIASI KEUANGAN (FASE 5)")
    print(f"{'='*60}")

    config = load_config()
    c19 = OdooClient(config["source_odoo_19"]["url"], config["source_odoo_19"]["db"], config["source_odoo_19"]["api_key"])
    c20 = OdooClient(config["target_odoo_20"]["url"], config["target_odoo_20"]["db"], config["target_odoo_20"]["api_key"])
    mapper = IdMapper.from_config(config)
    print(f"📁 Menggunakan SQLite Mapping DB: {mapper.db_path}")

    # 1. Pastikan currency_id pada account_bank_statement_line di Odoo 20 tidak NULL
    # (Diperlukan agar Odoo compute_is_reconciled berjalan mulus)
    import subprocess
    subprocess.run([
        "docker", "exec", "-i", "docker-odoo20-db-1", "psql", "-U", "odoo", "-d", config["target_odoo_20"]["db"],
        "-c", "UPDATE account_bank_statement_line SET currency_id = 12 WHERE currency_id IS NULL;"
    ], capture_output=True)

    # 2. Ambil data partial reconcile dari Odoo 19
    partials = c19.search_read(
        "account.partial.reconcile",
        [],
        ["id", "debit_move_id", "credit_move_id", "amount", "full_reconcile_id"]
    )
    print(f"📋 Ditemukan {len(partials)} pasangan rekonsiliasi di Odoo 19.")

    # 3. Kelompokkan baris berdasarkan full_reconcile_id atau id pasangan
    groups = defaultdict(set)
    for p in partials:
        old_deb = p["debit_move_id"][0]
        old_cred = p["credit_move_id"][0]
        new_deb = mapper.get_new_id("account.move.line", old_deb)
        new_cred = mapper.get_new_id("account.move.line", old_cred)

        if new_deb and new_cred:
            group_key = p["full_reconcile_id"][0] if p.get("full_reconcile_id") else f"single_{p['id']}"
            groups[group_key].add(new_deb)
            groups[group_key].add(new_cred)
        else:
            print(f"⚠️ Mapping tidak lengkap untuk partial [{p['id']}]: Deb {old_deb}->{new_deb}, Cred {old_cred}->{new_cred}")

    print(f"🔄 Terbentuk {len(groups)} kelompok rekonsiliasi untuk dieksekusi di Odoo 20.")

    success_count = 0
    already_count = 0
    error_count = 0

    for idx, (grp_key, line_ids) in enumerate(groups.items(), start=1):
        line_list = sorted(list(line_ids))
        try:
            c20.call("account.move.line", "reconcile", ids=line_list)
            success_count += 1
            if idx % 10 == 0 or idx == len(groups):
                print(f"  [{idx}/{len(groups)}] ✅ Rekonsiliasi grup {grp_key} (baris: {line_list}) berhasil.")
        except OdooAPIError as e:
            # Jika baris sudah ter-rekonsiliasi sebelumnya
            if "already reconciled" in str(e).lower():
                already_count += 1
            else:
                print(f"  [{idx}/{len(groups)}] ⚠️ Error grup {grp_key} (baris {line_list}): {e}")
                error_count += 1
        except Exception as e:
            print(f"  [{idx}/{len(groups)}] ❌ Exception: {e}")
            error_count += 1

    print(f"\n{'='*60}")
    print("📊 RINGKASAN REKONSILIASI")
    print(f"{'='*60}")
    print(f"  - Berhasil direkonsiliasi: {success_count} grup")
    print(f"  - Sudah ter-rekonsiliasi : {already_count} grup")
    print(f"  - Gagal / Dilewati       : {error_count} grup")

    # Hitung total partial reconcile di Odoo 20
    partials_20 = c20.search("account.partial.reconcile", [])
    print(f"  - Total account.partial.reconcile di Odoo 20 saat ini: {len(partials_20)}")
    print(f"{'='*60}")

    mapper.close()


if __name__ == "__main__":
    migrate_reconciliations()
