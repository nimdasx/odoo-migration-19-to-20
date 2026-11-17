"""
Skrip Migrasi Transaksi Jurnal (Fase 3: account.move & account.move.line)
Mendukung:
  - Dry run (--limit N)
  - Resume-able (melewati move yang sudah ada di SQLite mapping)
  - Pencatatan pemetaan ID move dan ID move line ke SQLite
  - Eksekusi action_post() otomatis
"""
import argparse
import json
import os
import sys
from typing import Dict, List, Any

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


def pair_lines(old_lines: List[Dict[str, Any]], created_lines_20: List[Dict[str, Any]], mapper: IdMapper) -> List[tuple]:
    """
    Memasangkan baris lama (Odoo 19) dengan baris baru (Odoo 20).
    Mengembalikan list tuple (old_line_id, new_line_id).
    """
    pairs = []
    # Jika jumlahnya persis sama, cek kecocokan berurutan
    if len(old_lines) == len(created_lines_20):
        for o_line, n_line in zip(old_lines, created_lines_20):
            pairs.append((o_line["id"], n_line["id"], o_line.get("name") or "/"))
        return pairs

    # Fallback pencocokan jika ada perbedaan urutan
    available_20 = list(created_lines_20)
    for o_line in old_lines:
        o_acc_mapped = mapper.get_new_id("account.account", o_line["account_id"][0])
        matched_idx = None
        for idx, n_line in enumerate(available_20):
            n_acc = n_line["account_id"][0] if isinstance(n_line["account_id"], (list, tuple)) else n_line["account_id"]
            if (n_acc == o_acc_mapped and
                abs(n_line["debit"] - o_line["debit"]) < 0.01 and
                abs(n_line["credit"] - o_line["credit"]) < 0.01):
                matched_idx = idx
                break

        if matched_idx is not None:
            n_line = available_20.pop(matched_idx)
            pairs.append((o_line["id"], n_line["id"], o_line.get("name") or "/"))
        else:
            # Fallback jika tidak ketemu persis
            if available_20:
                n_line = available_20.pop(0)
                pairs.append((o_line["id"], n_line["id"], o_line.get("name") or "/"))

    return pairs


def migrate_move(m19: Dict[str, Any], c19: OdooClient, c20: OdooClient, mapper: IdMapper) -> int:
    old_move_id = m19["id"]
    move_name = m19["name"]
    move_type = m19["move_type"]
    date = m19["date"]
    old_state = m19.get("state", "draft")

    # 1. Cek Mapping Journal
    old_journal_id = m19["journal_id"][0]
    new_journal_id = mapper.get_new_id("account.journal", old_journal_id)
    if not new_journal_id:
        raise ValueError(f"Jurnal lama ID {old_journal_id} ({m19['journal_id'][1]}) belum terpetakan di SQLite!")

    # 2. Cek Mapping Partner Move
    new_partner_id = False
    if m19.get("partner_id"):
        old_partner_id = m19["partner_id"][0]
        new_partner_id = mapper.get_new_id("res.partner", old_partner_id) or False

    # 3. Ambil seluruh line dari Odoo 19
    line_fields = [
        "id", "name", "account_id", "partner_id", "debit", "credit",
        "amount_currency", "currency_id", "tax_ids", "tax_line_id", "date_maturity"
    ]
    old_lines = c19.search_read(
        "account.move.line",
        [("move_id", "=", old_move_id)],
        fields=line_fields,
        order="id asc"
    )

    if not old_lines:
        print(f"⚠️ Move [{old_move_id}] {move_name} tidak memiliki baris jurnal, dilewati.")
        return 0

    # 4. Bangun payload baris untuk Odoo 20
    new_lines_payload = []
    for l in old_lines:
        old_acc_id = l["account_id"][0]
        new_acc_id = mapper.get_new_id("account.account", old_acc_id)
        if not new_acc_id:
            raise ValueError(f"Akun lama ID {old_acc_id} ({l['account_id'][1]}) belum terpetakan di SQLite!")

        l_partner_id = False
        if l.get("partner_id"):
            l_partner_id = mapper.get_new_id("res.partner", l["partner_id"][0]) or False

        line_vals = {
            "name": l.get("name") or "/",
            "account_id": new_acc_id,
            "partner_id": l_partner_id,
            "debit": l["debit"],
            "credit": l["credit"],
        }

        # Mata uang jika beda
        if l.get("currency_id"):
            curr_old = l["currency_id"][0] if isinstance(l["currency_id"], (list, tuple)) else l["currency_id"]
            curr_new = mapper.get_new_id("res.currency", curr_old)
            if curr_new:
                line_vals["currency_id"] = curr_new
                if "amount_currency" in l:
                    line_vals["amount_currency"] = l["amount_currency"]

        # Pajak jika ada
        if l.get("tax_ids"):
            mapped_taxes = []
            for t_old in l["tax_ids"]:
                t_new = mapper.get_new_id("account.tax", t_old)
                if t_new:
                    mapped_taxes.append(t_new)
            if mapped_taxes:
                line_vals["tax_ids"] = [(6, 0, mapped_taxes)]

        if l.get("tax_line_id"):
            t_line_old = l["tax_line_id"][0]
            t_line_new = mapper.get_new_id("account.tax", t_line_old)
            if t_line_new:
                line_vals["tax_line_id"] = t_line_new

        if l.get("date_maturity"):
            line_vals["date_maturity"] = l["date_maturity"]

        new_lines_payload.append((0, 0, line_vals))

    # 5. Bangun payload move utama
    move_payload = {
        "name": move_name,
        "date": date,
        "ref": m19.get("ref") or False,
        "move_type": move_type,
        "journal_id": new_journal_id,
        "partner_id": new_partner_id,
        "line_ids": new_lines_payload,
    }

    if m19.get("invoice_date"):
        move_payload["invoice_date"] = m19["invoice_date"]
    if m19.get("invoice_date_due"):
        move_payload["invoice_date_due"] = m19["invoice_date_due"]

    # 6. Create move di Odoo 20
    create_res = c20.create("account.move", move_payload)
    new_move_id = create_res[0] if isinstance(create_res, (list, tuple)) else create_res

    # 7. Simpan mapping move
    mapper.set_mapping("account.move", old_move_id, new_move_id, notes=move_name)

    # 8. Ambil lines yang baru terbentuk di Odoo 20 untuk mapping line_ids
    m20_info = c20.search_read(
        "account.move.line",
        [("move_id", "=", new_move_id)],
        fields=["id", "account_id", "debit", "credit", "name"],
        order="id asc"
    )
    line_pairs = pair_lines(old_lines, m20_info, mapper)
    mapper.set_mappings_batch("account.move.line", line_pairs)

    # 9. Posting jika status aslinya 'posted'
    if old_state == "posted":
        try:
            c20.execute_method("account.move", "action_post", [new_move_id])
        except OdooAPIError as e:
            print(f"⚠️ Gagal action_post() pada [{new_move_id}] {move_name}: {e}")

    return new_move_id


def run_migration(limit: int = 5, dry_run: bool = False):
    print(f"{'='*60}")
    action_title = "DRY RUN MIGRASI TRANSAKSI JURNAL (FASE 3)" if dry_run else "MIGRASI TRANSAKSI JURNAL (FASE 3)"
    print(f"🚀 {action_title}")
    if limit:
        print(f"   Target limit: {limit} transaksi pertama")
    print(f"{'='*60}")

    config = load_config()
    c19 = OdooClient(config["source_odoo_19"]["url"], config["source_odoo_19"]["db"], config["source_odoo_19"]["api_key"])
    c20 = OdooClient(config["target_odoo_20"]["url"], config["target_odoo_20"]["db"], config["target_odoo_20"]["api_key"])
    mapper = IdMapper.from_config(config)
    print(f"📁 Menggunakan SQLite Mapping DB: {mapper.db_path}")

    # Ambil move dari Odoo 19 urut tanggal paling lampau -> terbaru
    kwargs = {
        "domain": [],
        "fields": ["id", "name", "date", "ref", "move_type", "journal_id", "partner_id", "state", "invoice_date", "invoice_date_due"],
        "order": "date asc, id asc",
    }
    if limit:
        kwargs["limit"] = limit

    moves_19 = c19.search_read("account.move", **kwargs)
    print(f"📋 Ditemukan {len(moves_19)} transaksi untuk diproses.")

    success_count = 0
    skipped_count = 0
    error_count = 0

    for idx, m in enumerate(moves_19, start=1):
        old_id = m["id"]
        name = m["name"]
        date = m["date"]

        # Cek apakah sudah pernah dimigrasikan
        if mapper.has_mapping("account.move", old_id):
            existing_new_id = mapper.get_new_id("account.move", old_id)
            print(f"[{idx}/{len(moves_19)}] ⏩ Lewati [{old_id}] {name} (sudah ada di Odoo 20: ID {existing_new_id})")
            skipped_count += 1
            continue

        try:
            new_id = migrate_move(m, c19, c20, mapper)
            print(f"[{idx}/{len(moves_19)}] ✅ Berhasil: [{old_id}] {name} ({date}) -> Odoo 20 ID {new_id}")
            success_count += 1
        except Exception as e:
            print(f"[{idx}/{len(moves_19)}] ❌ Gagal [{old_id}] {name}: {e}")
            error_count += 1

    print(f"\n{'='*60}")
    print(f"📊 RINGKASAN {action_title}")
    print(f"{'='*60}")
    print(f"  - Berhasil diproses : {success_count} moves")
    print(f"  - Dilewati (Existing): {skipped_count} moves")
    print(f"  - Gagal             : {error_count} moves")
    print(f"  - Total Mapping Move: {mapper.count('account.move')} di SQLite")
    print(f"  - Total Mapping Line: {mapper.count('account.move.line')} di SQLite")
    print(f"{'='*60}")

    mapper.close()


def main():
    parser = argparse.ArgumentParser(description="Migrasi account.move Odoo 19 ke 20")
    parser.add_argument("--limit", type=int, default=5, help="Jumlah transaksi yang diproses (default 5 untuk dry run)")
    parser.add_argument("--all", action="store_true", help="Proses seluruh transaksi tanpa limit")
    args = parser.parse_args()

    limit = None if args.all else args.limit
    is_dry_run = not args.all and (args.limit == 5)
    run_migration(limit=limit, dry_run=is_dry_run)


if __name__ == "__main__":
    main()
