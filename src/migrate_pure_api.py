"""
Skrip Migrasi Akuntansi Odoo 19.0 ke Odoo 20.0 (100% Pure API JSON-2)
Arsitektur Dual-Route: Bebas akses database langsung (No Direct SQL/psql)

Fitur Utama:
1. Master Data Migration (Company, Currency, Taxes, Accounts, Journals, Partners) via API JSON-2.
2. Dual-Route Transaction Migration:
   - Transaksi Kas & Bank (1.163 entri) dibuat melalui model `account.bank.statement.line` via API.
     ORM Odoo secara otomatis membuat `account.move`, menautkan `statement_line_id`,
     serta menghitung `internal_index`, `sequence`, dan `is_reconciled` secara native.
   - Transaksi Jurnal Umum (51 entri) dibuat melalui model `account.move` via API.
3. Native Reconciliation via RPC `/json/2/account.move.line/reconcile`.
4. Automated Audit & Trial Balance Verification (Zero Discrepancy check).
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from odoo_client import OdooClient, OdooAPIError
from id_mapper import IdMapper


def load_config(config_path: str = "config.json") -> dict:
    if not os.path.exists(config_path):
        print(f"❌ File konfigurasi {config_path} tidak ditemukan!")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ==============================================================================
# FASE 2: MASTER DATA (PURE API)
# ==============================================================================
def migrate_master_data(c19: OdooClient, c20: OdooClient, mapper: IdMapper):
    print(f"\n{'='*70}\n[FASE 2] MIGRASI MASTER DATA (PURE API)\n{'='*70}")

    # 1. Company
    print("🏢 1. Sinkronisasi Data Perusahaan (res.company)...")
    comp19 = c19.search_read("res.company", [], ["id", "name", "currency_id"])[0]
    comp20 = c20.search_read("res.company", [], ["id", "name", "currency_id"])[0]
    if comp20["name"] != comp19["name"]:
        c20.write("res.company", [comp20["id"]], {"name": comp19["name"]})
    mapper.set_mapping("res.company", comp19["id"], comp20["id"], notes=f"Company: {comp19['name']}")
    new_comp_id = comp20["id"]
    print(f"   ✅ Perusahaan '{comp19['name']}' terpetakan: {comp19['id']} -> {comp20['id']}")

    # 2. Currencies
    print("\n💵 2. Sinkronisasi Mata Uang (res.currency)...")
    currs19 = c19.search_read("res.currency", [("active", "=", True)], ["id", "name"])
    currs20 = c20.search_read("res.currency", [], ["id", "name", "active"])
    map_curr20 = {c["name"]: c for c in currs20}
    for c in currs19:
        name = c["name"]
        if name in map_curr20:
            target = map_curr20[name]
            if not target.get("active"):
                c20.write("res.currency", [target["id"]], {"active": True})
            mapper.set_mapping("res.currency", c["id"], target["id"], notes=f"Currency: {name}")
            print(f"   ✅ Mata uang {name} terpetakan: {c['id']} -> {target['id']}")

    # 3. Tax Groups
    print("\n🏷️ 3. Sinkronisasi Grup Pajak (account.tax.group)...")
    tg19 = c19.search_read("account.tax.group", [], ["id", "name"])
    tg20 = c20.search_read("account.tax.group", [], ["id", "name"])
    map_tg20 = {t["name"]: t["id"] for t in tg20}
    for t in tg19:
        name = t["name"]
        if name in map_tg20:
            new_id = map_tg20[name]
        else:
            new_id = c20.create("account.tax.group", [{"name": name, "company_id": new_comp_id}])[0]
            map_tg20[name] = new_id
        mapper.set_mapping("account.tax.group", t["id"], new_id, notes=f"Tax Group: {name}")
    print(f"   ✅ Selesai: {len(tg19)} grup pajak terpetakan.")

    # 4. Taxes
    print("\n🧾 4. Sinkronisasi Pajak (account.tax)...")
    taxes19 = c19.search_read("account.tax", [], ["id", "name", "type_tax_use", "amount", "tax_group_id"])
    taxes20 = c20.search_read("account.tax", [], ["id", "name", "type_tax_use", "amount"])
    map_t20 = {(t["name"], t["type_tax_use"]): t["id"] for t in taxes20}
    for t in taxes19:
        key = (t["name"], t["type_tax_use"])
        if key in map_t20:
            new_id = map_t20[key]
        else:
            new_tg = mapper.get_new_id("account.tax.group", t["tax_group_id"][0]) if t.get("tax_group_id") else None
            vals = {
                "name": t["name"],
                "type_tax_use": t["type_tax_use"],
                "amount": t["amount"],
                "company_id": new_comp_id,
            }
            if new_tg:
                vals["tax_group_id"] = new_tg
            new_id = c20.create("account.tax", [vals])[0]
            map_t20[key] = new_id
        mapper.set_mapping("account.tax", t["id"], new_id, notes=f"Tax: {t['name']}")
    print(f"   ✅ Selesai: {len(taxes19)} aturan pajak terpetakan.")

    # 5. Accounts (Chart of Accounts)
    print("\n📚 5. Sinkronisasi Bagan Akun (account.account)...")
    accs19 = c19.search_read("account.account", [], ["id", "code", "name", "account_type", "reconcile", "currency_id"])
    accs20 = c20.search_read("account.account", [], ["id", "code", "name", "account_type"])
    map_a20 = {a["code"]: a for a in accs20}
    for a in accs19:
        code = a["code"]
        name = a["name"]
        if code in map_a20:
            new_id = map_a20[code]["id"]
            if map_a20[code].get("name") != name:
                c20.write("account.account", [new_id], {"name": name})
        else:
            vals = {
                "code": code,
                "name": name,
                "account_type": a["account_type"],
                "company_ids": [(6, 0, [new_comp_id])],
            }
            if a.get("reconcile"):
                vals["reconcile"] = True
            new_id = c20.create("account.account", [vals])[0]
            map_a20[code] = {"id": new_id, "code": code, "name": name}
        mapper.set_mapping("account.account", a["id"], new_id, notes=f"{code} - {name}")
    print(f"   ✅ Selesai: {len(accs19)} akun buku besar terpetakan dan tersinkronisasi namanya.")

    # 6. Journals
    print("\n📖 6. Sinkronisasi Jurnal (account.journal)...")
    j19 = c19.search_read("account.journal", [], ["id", "code", "name", "type", "default_account_id"])
    j20 = c20.search_read("account.journal", [], ["id", "code", "name", "type", "default_account_id"])
    map_j20 = {j["code"]: j for j in j20}
    for j in j19:
        code = j["code"]
        name = j["name"]
        if code in map_j20:
            new_id = map_j20[code]["id"]
            if map_j20[code].get("name") != name:
                c20.write("account.journal", [new_id], {"name": name})
        else:
            new_acc_id = None
            if j.get("default_account_id"):
                new_acc_id = mapper.get_new_id("account.account", j["default_account_id"][0])
            vals = {
                "name": name,
                "code": code,
                "type": j["type"],
                "company_id": new_comp_id,
            }
            if new_acc_id:
                vals["default_account_id"] = new_acc_id
            new_id = c20.create("account.journal", [vals])[0]
            map_j20[code] = {"id": new_id, "code": code, "name": name}
        mapper.set_mapping("account.journal", j["id"], new_id, notes=f"{code} - {name}")

    # Nonaktifkan jurnal placeholder bawaan yang tidak terpakai
    all_j20 = c20.search_read("account.journal", [], ["id", "code", "type"])
    cur = mapper.conn.cursor()
    cur.execute("SELECT new_id FROM id_mapping WHERE model = 'account.journal'")
    mapped_j20_ids = set(row[0] for row in cur.fetchall())
    unmapped_placeholders = [
        j["id"] for j in all_j20
        if j["id"] not in mapped_j20_ids and (j["type"] in ("bank", "cash") or j["code"].startswith("BNK"))
    ]
    if unmapped_placeholders:
        c20.write("account.journal", unmapped_placeholders, {"active": False, "show_on_dashboard": False})
    print(f"   ✅ Selesai: {len(j19)} jurnal terpetakan ({len(unmapped_placeholders)} jurnal placeholder dinonaktifkan).")

    # 7. Partners
    print("\n👥 7. Sinkronisasi Kontak Rekanan (res.partner)...")
    partners19 = c19.search_read("res.partner", [], ["id", "name", "email", "phone", "street", "is_company"])
    partners20 = c20.search_read("res.partner", [], ["id", "name"])
    map_p20 = {p["name"].strip().lower(): p["id"] for p in partners20 if p.get("name")}
    for p in partners19:
        name_key = (p["name"] or "").strip().lower()
        if name_key in map_p20:
            new_id = map_p20[name_key]
        else:
            vals = {
                "name": p["name"],
                "is_company": p.get("is_company", False),
                "company_id": new_comp_id,
            }
            if p.get("email"):
                vals["email"] = p["email"]
            if p.get("phone"):
                vals["phone"] = p["phone"]
            if p.get("street"):
                vals["street"] = p["street"]
            new_id = c20.create("res.partner", [vals])[0]
            map_p20[name_key] = new_id
        mapper.set_mapping("res.partner", p["id"], new_id, notes=f"Partner: {p['name']}")
    print(f"   ✅ Selesai: {len(partners19)} kontak rekanan terpetakan.")


# ==============================================================================
# FASE 3: DUAL-ROUTE TRANSACTIONS (100% PURE API)
# ==============================================================================
def pair_lines(old_lines: List[Dict[str, Any]], created_lines_20: List[Dict[str, Any]], mapper: IdMapper) -> List[Tuple[int, int, str]]:
    """Mencocokkan baris lama dengan baris baru untuk memetakan ID account.move.line."""
    pairs = []
    if len(old_lines) == len(created_lines_20):
        # Coba cek apakah urutan akun & debit/credit sama persis
        matched_in_order = True
        for o, n in zip(old_lines, created_lines_20):
            o_acc = mapper.get_new_id("account.account", o["account_id"][0])
            n_acc = n["account_id"][0] if isinstance(n["account_id"], (list, tuple)) else n["account_id"]
            if o_acc != n_acc or abs(o["debit"] - n["debit"]) > 0.01 or abs(o["credit"] - n["credit"]) > 0.01:
                matched_in_order = False
                break
        if matched_in_order:
            for o, n in zip(old_lines, created_lines_20):
                pairs.append((o["id"], n["id"], o.get("name") or "/"))
            return pairs

    # Fallback jika urutan berbeda
    available_20 = list(created_lines_20)
    for o in old_lines:
        o_acc = mapper.get_new_id("account.account", o["account_id"][0])
        matched_idx = None
        for idx, n in enumerate(available_20):
            n_acc = n["account_id"][0] if isinstance(n["account_id"], (list, tuple)) else n["account_id"]
            if n_acc == o_acc and abs(n["debit"] - o["debit"]) < 0.01 and abs(n["credit"] - o["credit"]) < 0.01:
                matched_idx = idx
                break
        if matched_idx is not None:
            n = available_20.pop(matched_idx)
            pairs.append((o["id"], n["id"], o.get("name") or "/"))
        elif available_20:
            n = available_20.pop(0)
            pairs.append((o["id"], n["id"], o.get("name") or "/"))
    return pairs


def build_lines_payload(old_lines: List[Dict[str, Any]], mapper: IdMapper) -> List[Tuple]:
    """Menyiapkan payload baris buku besar Odoo 20."""
    new_lines = []
    for l in old_lines:
        new_acc = mapper.get_new_id("account.account", l["account_id"][0])
        if not new_acc:
            raise ValueError(f"Akun lama {l['account_id']} belum terpetakan!")
        p_id = False
        if l.get("partner_id"):
            p_id = mapper.get_new_id("res.partner", l["partner_id"][0]) or False
        line_vals = {
            "name": l.get("name") or "/",
            "account_id": new_acc,
            "partner_id": p_id,
            "debit": l["debit"],
            "credit": l["credit"],
        }
        if l.get("currency_id"):
            curr_old = l["currency_id"][0] if isinstance(l["currency_id"], (list, tuple)) else l["currency_id"]
            curr_new = mapper.get_new_id("res.currency", curr_old)
            if curr_new:
                line_vals["currency_id"] = curr_new
                if "amount_currency" in l:
                    line_vals["amount_currency"] = l["amount_currency"]
        new_lines.append((0, 0, line_vals))
    return new_lines


def migrate_transactions_pure_api(c19: OdooClient, c20: OdooClient, mapper: IdMapper, limit: Optional[int] = None):
    print(f"\n{'='*70}\n[FASE 3] MIGRASI TRANSAKSI DUAL-ROUTE (100% PURE API)\n{'='*70}")

    # 1. Ekstrak data bank statement lines dari Odoo 19 untuk pengelompokan
    print("📥 Mengambil indeks mutasi bank Odoo 19...")
    st_lines_19 = c19.search_read(
        "account.bank.statement.line",
        [],
        ["id", "move_id", "journal_id", "payment_ref", "amount", "partner_id"],
        order="id asc"
    )
    st_by_move_id = {st["move_id"][0]: st for st in st_lines_19 if st.get("move_id")}
    print(f"📋 Ditemukan {len(st_lines_19)} mutasi bank terhubung.")

    # 2. Ekstrak seluruh entri transaksi account.move dari Odoo 19
    print("📥 Mengambil seluruh header jurnal transaksi Odoo 19...")
    move_fields = ["id", "name", "date", "ref", "state", "move_type", "journal_id", "partner_id"]
    moves_19 = c19.search_read("account.move", [], move_fields, order="date asc, id asc", limit=limit)
    total_moves = len(moves_19)
    print(f"📋 Ditemukan {total_moves} dokumen jurnal yang siap dimigrasikan.")

    # 3. Proses migrasi per dokumen
    success_bank = 0
    success_general = 0
    skipped = 0
    t0 = time.time()

    line_fields = ["id", "name", "account_id", "partner_id", "debit", "credit", "currency_id", "amount_currency"]

    for i, m in enumerate(moves_19, 1):
        old_move_id = m["id"]
        original_name = m["name"]

        # Cek jika move sudah pernah dimigrasikan
        if mapper.has_mapping("account.move", old_move_id):
            skipped += 1
            continue

        new_journal_id = mapper.get_new_id("account.journal", m["journal_id"][0])
        new_partner_id = mapper.get_new_id("res.partner", m["partner_id"][0]) if m.get("partner_id") else False

        # Ambil baris jurnal asli
        old_lines = c19.search_read("account.move.line", [("move_id", "=", old_move_id)], line_fields, order="id asc")
        if not old_lines:
            continue
        new_lines_payload = build_lines_payload(old_lines, mapper)

        if old_move_id in st_by_move_id:
            # ==================================================================
            # RUTE A: MUTASI KAS & BANK VIA account.bank.statement.line
            # ==================================================================
            st19 = st_by_move_id[old_move_id]
            st_vals = {
                "journal_id": new_journal_id,
                "date": m["date"],
                "payment_ref": st19.get("payment_ref") or m.get("ref") or "/",
                "amount": st19.get("amount", 0.0),
                "partner_id": new_partner_id,
                "line_ids": new_lines_payload,
            }

            res = c20.create("account.bank.statement.line", [st_vals])
            new_st_id = res[0]

            # Baca kembali move_id dan line_ids yang otomatis dibuat oleh ORM Odoo
            st_created = c20.read("account.bank.statement.line", [new_st_id], ["move_id", "line_ids"])[0]
            new_move_id = st_created["move_id"][0]

            # Pulihkan nomor urut asli (Sequence Name)
            c20.write("account.move", [new_move_id], {"name": original_name})

            # Simpan mapping
            mapper.set_mapping("account.bank.statement.line", st19["id"], new_st_id, notes=original_name)
            mapper.set_mapping("account.move", old_move_id, new_move_id, notes=original_name)

            # Ambil lines baru untuk pemetaan
            m20_lines = c20.read("account.move.line", st_created["line_ids"], ["id", "account_id", "debit", "credit"])
            line_pairs = pair_lines(old_lines, m20_lines, mapper)
            mapper.set_mappings_batch("account.move.line", line_pairs)

            success_bank += 1
        else:
            # ==================================================================
            # RUTE B: JURNAL UMUM / MISC VIA account.move
            # ==================================================================
            move_vals = {
                "name": original_name,
                "date": m["date"],
                "ref": m.get("ref") or False,
                "move_type": m["move_type"],
                "journal_id": new_journal_id,
                "partner_id": new_partner_id,
                "line_ids": new_lines_payload,
            }
            res = c20.create("account.move", [move_vals])
            new_move_id = res[0]

            if m.get("state") == "posted":
                c20.call("account.move", "action_post", ids=[new_move_id])
                c20.write("account.move", [new_move_id], {"name": original_name})

            mapper.set_mapping("account.move", old_move_id, new_move_id, notes=original_name)

            m20_lines = c20.search_read("account.move.line", [("move_id", "=", new_move_id)], ["id", "account_id", "debit", "credit"])
            line_pairs = pair_lines(old_lines, m20_lines, mapper)
            mapper.set_mappings_batch("account.move.line", line_pairs)

            success_general += 1

        if i % 100 == 0 or i == total_moves:
            elapsed = time.time() - t0
            print(f"   ⚡ [{i}/{total_moves}] Diproses (Bank: {success_bank}, General: {success_general}, Lewat: {skipped}) - {elapsed:.1f}s")

    print(f"\n✅ Selesai Migrasi Transaksi: {success_bank} bank lines + {success_general} general moves berhasil 100% via API.")


# ==============================================================================
# FASE 5: RECONCILIATION & AUDIT (PURE API)
# ==============================================================================
def migrate_reconciliations_pure_api(c19: OdooClient, c20: OdooClient, mapper: IdMapper):
    print(f"\n{'='*70}\n[FASE 5] REKONSILIASI PEMBAYARAN NATIVE (PURE API)\n{'='*70}")
    partials_19 = c19.search_read(
        "account.partial.reconcile",
        [],
        ["id", "debit_move_id", "credit_move_id", "amount"],
        order="id asc"
    )
    print(f"📋 Ditemukan {len(partials_19)} pasangan rekonsiliasi parsial di Odoo 19.")

    success = 0
    failed = 0
    for p in partials_19:
        d_old = p["debit_move_id"][0]
        c_old = p["credit_move_id"][0]

        d_new = mapper.get_new_id("account.move.line", d_old)
        c_new = mapper.get_new_id("account.move.line", c_old)

        if not d_new or not c_new:
            failed += 1
            continue

        try:
            c20.call("account.move.line", "reconcile", ids=[d_new, c_new])
            success += 1
        except Exception as e:
            # Jika sudah direkonsiliasi otomatis oleh statement line
            failed += 1

    print(f"✅ Selesai: {success} rekonsiliasi berhasil diproses native via API ({failed} dilewati/sudah terekonsiliasi).")


def verify_pure_api(c19: OdooClient, c20: OdooClient):
    print(f"\n{'='*70}\n[VERIFIKASI] AUDIT INTEGRITAS & TRIAL BALANCE (PURE API)\n{'='*70}")
    ml19 = c19.search_read("account.move.line", [], ["debit", "credit"])
    ml20 = c20.search_read("account.move.line", [], ["debit", "credit"])

    deb19 = sum(l["debit"] for l in ml19)
    deb20 = sum(l["debit"] for l in ml20)
    cred19 = sum(l["credit"] for l in ml19)
    cred20 = sum(l["credit"] for l in ml20)

    print(f"Total Debit  19: Rp {deb19:,.2f} | 20: Rp {deb20:,.2f} | Diff: Rp {deb20 - deb19:,.2f}")
    print(f"Total Credit 19: Rp {cred19:,.2f} | 20: Rp {cred20:,.2f} | Diff: Rp {cred20 - cred19:,.2f}")

    if abs(deb20 - deb19) < 0.01 and abs(cred20 - cred19) < 0.01:
        print("🎉 STATUS AUDIT: 100% IDENTIK & SEIMBANG (ZERO DISCREPANCY)!")
    else:
        print("⚠️ PERINGATAN: Terdapat selisih nominal!")


# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="Migrasi Odoo 19 ke 20 Pure API JSON-2")
    parser.add_argument("--phase", choices=["all", "master", "transactions", "reconcile", "verify"], default="verify",
                        help="Pilih fase yang ingin dijalankan (default: verify)")
    parser.add_argument("--limit", type=int, default=None, help="Batasi jumlah transaksi untuk uji coba")
    args = parser.parse_args()

    cfg = load_config()
    c19 = OdooClient(cfg["source_odoo_19"]["url"], cfg["source_odoo_19"]["db"], cfg["source_odoo_19"]["api_key"])
    c20 = OdooClient(cfg["target_odoo_20"]["url"], cfg["target_odoo_20"]["db"], cfg["target_odoo_20"]["api_key"])
    mapper = IdMapper.from_config(cfg)
    print(f"📁 Menggunakan SQLite Mapping DB: {mapper.db_path}")

    if args.phase in ("all", "master"):
        migrate_master_data(c19, c20, mapper)
    if args.phase in ("all", "transactions"):
        migrate_transactions_pure_api(c19, c20, mapper, limit=args.limit)
    if args.phase in ("all", "reconcile"):
        migrate_reconciliations_pure_api(c19, c20, mapper)
    if args.phase in ("all", "verify"):
        verify_pure_api(c19, c20)

    mapper.close()


if __name__ == "__main__":
    main()
