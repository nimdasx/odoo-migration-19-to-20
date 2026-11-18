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
    # Sertakan mata uang nonaktif di target (mis. IDR bawaan yang belum diaktifkan);
    # tanpa active_test=False mata uang tsb tidak terlihat dan dilewati diam-diam.
    currs20 = c20.call("res.currency", "search_read", domain=[], fields=["id", "name", "active"],
                       context={"active_test": False})
    map_curr20 = {c["name"]: c for c in currs20}
    for c in currs19:
        name = c["name"]
        if name in map_curr20:
            target = map_curr20[name]
            if not target.get("active"):
                c20.write("res.currency", [target["id"]], {"active": True})
            mapper.set_mapping("res.currency", c["id"], target["id"], notes=f"Currency: {name}")
            print(f"   ✅ Mata uang {name} terpetakan: {c['id']} -> {target['id']}")
        else:
            print(f"   ⚠️ Mata uang {name} tidak ditemukan di target — tidak terpetakan!")

    # Samakan mata uang perusahaan dengan sumber (memengaruhi format tampilan
    # & pembulatan). Bila target sudah punya jurnal, Odoo menolak perubahan
    # mata uang — cukup peringatkan, jangan gagalkan seluruh fase.
    if comp19.get("currency_id"):
        src_curr_name = comp19["currency_id"][1]
        target_curr = map_curr20.get(src_curr_name)
        if target_curr and comp20.get("currency_id") and comp20["currency_id"][0] != target_curr["id"]:
            try:
                c20.write("res.company", [comp20["id"]], {"currency_id": target_curr["id"]})
                print(f"   ✅ Mata uang perusahaan diset ke {src_curr_name}.")
            except OdooAPIError as e:
                print(f"   ⚠️ Mata uang perusahaan tidak bisa diubah (sudah ada jurnal?): {e}")

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

    # 7. Analytic Accounts
    print("   [7/7] Sinkronisasi Akun Analitik (account.analytic.account)...")
    try:
        plans19 = c19.search_read("account.analytic.plan", [], ["id", "name"])
        plans20 = c20.search_read("account.analytic.plan", [], ["id", "name"])
        p20_map = {p["name"].lower(): p["id"] for p in plans20}
        plan_id_map = {}
        for p in plans19:
            p_name = p["name"]
            matched_id = p20_map.get(p_name.lower()) or (plans20[0]["id"] if plans20 else None)
            if not matched_id:
                res = c20.create("account.analytic.plan", [{"name": p_name}])
                matched_id = res[0]
                p20_map[p_name.lower()] = matched_id
            plan_id_map[p["id"]] = matched_id
            mapper.set_mapping("account.analytic.plan", p["id"], matched_id, notes=p_name)

        accs19 = c19.search_read("account.analytic.account", [], ["id", "name", "code", "plan_id", "partner_id"])
        accs20 = c20.search_read("account.analytic.account", [], ["id", "name", "plan_id"])
        a20_map = {a["name"].lower(): a["id"] for a in accs20}
        for a in accs19:
            old_id = a["id"]
            name = a["name"]
            if name.lower() in a20_map:
                new_id = a20_map[name.lower()]
            else:
                old_plan = a["plan_id"][0] if a.get("plan_id") else None
                new_plan = plan_id_map.get(old_plan) or (plans20[0]["id"] if plans20 else 1)
                vals = {"name": name, "plan_id": new_plan, "code": a.get("code") or False}
                new_id = c20.create("account.analytic.account", [vals])[0]
                a20_map[name.lower()] = new_id
            mapper.set_mapping("account.analytic.account", old_id, new_id, notes=name)
        print(f"   ✅ Selesai: {len(accs19)} akun analitik terpetakan.")
    except Exception as e:
        print(f"   ⚠️ Gagal sinkronisasi akun analitik: {e}")


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

    # Fallback jika urutan berbeda: cocokkan berdasarkan akun + debit/credit,
    # lalu akun + name. Jika tetap tidak ketemu, GAGAL KERAS (jangan menebak),
    # karena mapping baris yang salah akan merusak rekonsiliasi di fase berikutnya.
    available_20 = list(created_lines_20)
    for o in old_lines:
        o_acc = mapper.get_new_id("account.account", o["account_id"][0])
        matched_idx = None
        for idx, n in enumerate(available_20):
            n_acc = n["account_id"][0] if isinstance(n["account_id"], (list, tuple)) else n["account_id"]
            if n_acc == o_acc and abs(n["debit"] - o["debit"]) < 0.01 and abs(n["credit"] - o["credit"]) < 0.01:
                matched_idx = idx
                break
        if matched_idx is None:
            for idx, n in enumerate(available_20):
                n_acc = n["account_id"][0] if isinstance(n["account_id"], (list, tuple)) else n["account_id"]
                if n_acc == o_acc and (n.get("name") or "/") == (o.get("name") or "/"):
                    matched_idx = idx
                    break
        if matched_idx is None:
            raise ValueError(
                f"Baris lama ID {o['id']} (akun {o['account_id']}, debit {o['debit']}, credit {o['credit']}) "
                f"tidak menemukan padanan yang sah di Odoo 20. Jumlah baris sumber={len(old_lines)}, "
                f"target={len(created_lines_20)}. Mapping dibatalkan agar tidak salah rekonsiliasi."
            )
        n = available_20.pop(matched_idx)
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
        # Pajak & termin: pulihkan relasi pajak agar laporan PPN/aging di Odoo 20 benar.
        # Aman dari duplikasi baris pajak karena baris hasil salinan tidak memiliki
        # display_type='product' / tax_repartition_line_id, sehingga tidak disinkron
        # ulang oleh ORM Odoo (lihat account.move._sync_tax_lines).
        if l.get("tax_ids"):
            mapped_taxes = [t for t in (mapper.get_new_id("account.tax", t_old) for t_old in l["tax_ids"]) if t]
            if mapped_taxes:
                line_vals["tax_ids"] = [(6, 0, mapped_taxes)]
        if l.get("tax_line_id"):
            tl_old = l["tax_line_id"][0] if isinstance(l["tax_line_id"], (list, tuple)) else l["tax_line_id"]
            tl_new = mapper.get_new_id("account.tax", tl_old)
            if tl_new:
                line_vals["tax_line_id"] = tl_new
        if l.get("date_maturity"):
            line_vals["date_maturity"] = l["date_maturity"]
        if l.get("analytic_distribution") and isinstance(l["analytic_distribution"], dict):
            new_dist = {}
            for old_aa_id_str, pct in l["analytic_distribution"].items():
                try:
                    new_aa_id = mapper.get_new_id("account.analytic.account", int(old_aa_id_str))
                    if new_aa_id:
                        new_dist[str(new_aa_id)] = pct
                except Exception:
                    pass
            if new_dist:
                line_vals["analytic_distribution"] = new_dist
        new_lines.append((0, 0, line_vals))
    return new_lines


def migrate_transactions_pure_api(c19: OdooClient, c20: OdooClient, mapper: IdMapper, limit: Optional[int] = None,
                                  log=None, progress=None):
    _log = log or (lambda text, level="info": print(text))
    _progress = progress or (lambda percent, stage: None)

    _log(f"\n{'='*70}\n[FASE 3] MIGRASI TRANSAKSI DUAL-ROUTE (100% PURE API)\n{'='*70}")

    # 1. Ekstrak data bank statement lines dari Odoo 19 untuk pengelompokan
    _log("📥 Mengambil indeks mutasi bank Odoo 19...")
    st_lines_19 = c19.search_read(
        "account.bank.statement.line",
        [],
        ["id", "move_id", "journal_id", "payment_ref", "amount", "partner_id"],
        order="id asc"
    )
    st_by_move_id = {st["move_id"][0]: st for st in st_lines_19 if st.get("move_id")}
    _log(f"📋 Ditemukan {len(st_lines_19)} mutasi bank terhubung.")

    # 2. Ekstrak seluruh entri transaksi account.move dari Odoo 19
    _log("📥 Mengambil seluruh header jurnal transaksi Odoo 19...")
    move_fields = ["id", "name", "date", "ref", "state", "move_type", "journal_id", "partner_id",
                   "invoice_date", "invoice_date_due"]
    moves_19 = c19.search_read("account.move", [], move_fields, order="date asc, id asc", limit=limit)
    total_moves = len(moves_19)
    _log(f"📋 Ditemukan {total_moves} dokumen jurnal yang siap dimigrasikan.")

    is_remote_target = "odoo.com" in c20.url or c20.url.startswith("https://") or ("localhost" not in c20.url and "127.0.0.1" not in c20.url)
    if is_remote_target:
        _log("🌐 Target terdeteksi sebagai Cloud / Remote Server. Mengaktifkan adaptive pacing & auto-retry rate limit.")

    # 3. Indeks move eksisting di Odoo 20 untuk auto-recovery & pencegahan duplikasi.
    #    Sertakan journal_id & date agar adopsi hanya terjadi bila benar-benar move yang sama.
    existing_moves_20 = c20.search_read("account.move", [], ["id", "name", "journal_id", "date", "statement_line_id"])
    m20_by_name = {m["name"]: m for m in existing_moves_20 if m.get("name") and m["name"] != "/"}

    success_bank = 0
    success_general = 0
    skipped = 0
    failed = 0
    t0 = time.time()

    line_fields = ["id", "name", "account_id", "partner_id", "debit", "credit", "currency_id",
                   "amount_currency", "analytic_distribution", "tax_ids", "tax_line_id", "date_maturity"]
    m20_line_fields = ["id", "name", "account_id", "debit", "credit"]

    for i, m in enumerate(moves_19, 1):
        old_move_id = m["id"]
        original_name = m["name"]

        try:
            # Cek apakah move dengan nomor sequence ini sudah ada di Odoo 20 (auto-recovery).
            # Adopsi HANYA bila jurnal & tanggal cocok; jika tidak, ini move berbeda -> buat baru.
            adopt_candidate = None
            if original_name and original_name != "/" and original_name in m20_by_name:
                cand = m20_by_name[original_name]
                nj = mapper.get_new_id("account.journal", m["journal_id"][0])
                cj = cand["journal_id"][0] if isinstance(cand.get("journal_id"), (list, tuple)) else cand.get("journal_id")
                if nj and cj == nj and cand.get("date") == m["date"]:
                    adopt_candidate = cand
                else:
                    _log(f"⚠️ Nama '{original_name}' sudah ada di Odoo 20 tetapi jurnal/tanggal berbeda; diperlakukan sebagai move baru.", "warning")

            if mapper.has_mapping("account.move", old_move_id):
                skipped += 1
            elif adopt_candidate is not None:
                # ---- Jalur adopsi: petakan move yang sudah ada, dengan validasi nominal ----
                new_move_id = adopt_candidate["id"]
                old_lines = c19.search_read("account.move.line", [("move_id", "=", old_move_id)], line_fields, order="id asc")
                m20_lines = c20.search_read("account.move.line", [("move_id", "=", new_move_id)], m20_line_fields, order="id asc")
                if abs(sum(l["debit"] for l in old_lines) - sum(l["debit"] for l in m20_lines)) > 0.01 or \
                   abs(sum(l["credit"] for l in old_lines) - sum(l["credit"] for l in m20_lines)) > 0.01:
                    raise ValueError(f"Adopsi '{original_name}' ditolak: total debit/credit Odoo 20 berbeda dari sumber.")

                mapper.set_mapping("account.move", old_move_id, new_move_id, notes=original_name)
                if old_move_id in st_by_move_id:
                    st19 = st_by_move_id[old_move_id]
                    st20_id = adopt_candidate["statement_line_id"][0] if adopt_candidate.get("statement_line_id") else None
                    if not st20_id:
                        st20_recs = c20.search_read("account.bank.statement.line", [("move_id", "=", new_move_id)], ["id"], limit=1)
                        if st20_recs:
                            st20_id = st20_recs[0]["id"]
                    if st20_id:
                        mapper.set_mapping("account.bank.statement.line", st19["id"], st20_id, notes=original_name)

                line_pairs = pair_lines(old_lines, m20_lines, mapper)
                mapper.set_mappings_batch("account.move.line", line_pairs)
                skipped += 1
            else:
                new_journal_id = mapper.get_new_id("account.journal", m["journal_id"][0])
                if not new_journal_id:
                    raise ValueError(f"Jurnal lama {m['journal_id']} belum terpetakan!")
                new_partner_id = mapper.get_new_id("res.partner", m["partner_id"][0]) if m.get("partner_id") else False

                old_lines = c19.search_read("account.move.line", [("move_id", "=", old_move_id)], line_fields, order="id asc")
                if not old_lines:
                    skipped += 1
                else:
                    new_lines_payload = build_lines_payload(old_lines, mapper)

                    if old_move_id in st_by_move_id:
                        # ==========================================================
                        # RUTE A: MUTASI KAS & BANK VIA account.bank.statement.line
                        # ==========================================================
                        st19 = st_by_move_id[old_move_id]
                        st_vals = {
                            "journal_id": new_journal_id,
                            "date": m["date"],
                            "payment_ref": st19.get("payment_ref") or m.get("ref") or "/",
                            "amount": st19.get("amount", 0.0),
                            "partner_id": new_partner_id,
                            "line_ids": new_lines_payload,
                        }
                        new_st_id = c20.create("account.bank.statement.line", [st_vals])[0]
                        st_created = c20.read("account.bank.statement.line", [new_st_id], ["move_id", "line_ids"])[0]
                        new_move_id = st_created["move_id"][0]
                        c20.write("account.move", [new_move_id], {"name": original_name})

                        mapper.set_mapping("account.bank.statement.line", st19["id"], new_st_id, notes=original_name)
                        mapper.set_mapping("account.move", old_move_id, new_move_id, notes=original_name)
                        m20_by_name[original_name] = {"id": new_move_id, "journal_id": [new_journal_id],
                                                      "date": m["date"], "statement_line_id": [new_st_id]}

                        m20_lines = c20.read("account.move.line", st_created["line_ids"], m20_line_fields)
                        line_pairs = pair_lines(old_lines, m20_lines, mapper)
                        mapper.set_mappings_batch("account.move.line", line_pairs)
                        success_bank += 1
                    else:
                        # ==========================================================
                        # RUTE B: JURNAL UMUM / MISC VIA account.move
                        # ==========================================================
                        move_vals = {
                            "name": original_name,
                            "date": m["date"],
                            "ref": m.get("ref") or False,
                            "move_type": m["move_type"],
                            "journal_id": new_journal_id,
                            "partner_id": new_partner_id,
                            "line_ids": new_lines_payload,
                        }
                        if m.get("invoice_date"):
                            move_vals["invoice_date"] = m["invoice_date"]
                        if m.get("invoice_date_due"):
                            move_vals["invoice_date_due"] = m["invoice_date_due"]

                        new_move_id = c20.create("account.move", [move_vals])[0]
                        if m.get("state") == "posted":
                            c20.call("account.move", "action_post", ids=[new_move_id])
                            c20.write("account.move", [new_move_id], {"name": original_name})

                        mapper.set_mapping("account.move", old_move_id, new_move_id, notes=original_name)
                        m20_by_name[original_name] = {"id": new_move_id, "journal_id": [new_journal_id],
                                                      "date": m["date"], "statement_line_id": False}

                        m20_lines = c20.search_read("account.move.line", [("move_id", "=", new_move_id)], m20_line_fields, order="id asc")
                        line_pairs = pair_lines(old_lines, m20_lines, mapper)
                        mapper.set_mappings_batch("account.move.line", line_pairs)
                        success_general += 1
        except Exception as e:
            # Isolasi error per-move: catat ke SQLite lalu LANJUT, jangan hentikan seluruh fase.
            failed += 1
            mapper.record_error("transactions", "account.move", old_move_id, str(e))
            _log(f"❌ [{i}/{total_moves}] Gagal move [{old_move_id}] {original_name}: {e}", "error")

        if is_remote_target:
            time.sleep(0.12)

        if i % 25 == 0 or i == total_moves:
            elapsed = time.time() - t0
            pct = 30 + int((i / total_moves) * 45) if total_moves else 75
            _progress(pct, f"Transaksi [{i}/{total_moves}]")
            _log(f"   ⚡ [{i}/{total_moves}] Diproses (Bank: {success_bank}, General: {success_general}, Lewat: {skipped}, Gagal: {failed}) - {elapsed:.1f}s")

    _log(f"\n✅ Selesai Migrasi Transaksi: {success_bank} bank lines + {success_general} general moves via API "
         f"({skipped} dilewati, {failed} gagal tercatat di SQLite).", "success" if failed == 0 else "warning")
    return {"bank": success_bank, "general": success_general, "skipped": skipped, "failed": failed}


# ==============================================================================
# FASE 5: RECONCILIATION & AUDIT (PURE API)
# ==============================================================================
def migrate_reconciliations_pure_api(c19: OdooClient, c20: OdooClient, mapper: IdMapper, log=None):
    _log = log or (lambda text, level="info": print(text))
    _log(f"\n{'='*70}\n[FASE 5] REKONSILIASI PEMBAYARAN NATIVE (PURE API)\n{'='*70}")
    partials_19 = c19.search_read(
        "account.partial.reconcile",
        [],
        ["id", "debit_move_id", "credit_move_id", "amount", "full_reconcile_id"],
        order="id asc"
    )
    _log(f"📋 Ditemukan {len(partials_19)} pasangan rekonsiliasi parsial di Odoo 19.")

    # Kelompokkan baris berdasarkan full_reconcile_id agar rekonsiliasi 3+ baris
    # (mis. satu invoice dibayar beberapa kali) direkonsiliasi sebagai satu grup utuh.
    from collections import defaultdict
    groups = defaultdict(set)
    unmapped = 0
    for p in partials_19:
        d_new = mapper.get_new_id("account.move.line", p["debit_move_id"][0])
        c_new = mapper.get_new_id("account.move.line", p["credit_move_id"][0])
        if not d_new or not c_new:
            unmapped += 1
            continue
        key = p["full_reconcile_id"][0] if p.get("full_reconcile_id") else f"single_{p['id']}"
        groups[key].add(d_new)
        groups[key].add(c_new)

    _log(f"🔄 Terbentuk {len(groups)} kelompok rekonsiliasi ({unmapped} partial dilewati karena mapping baris tidak lengkap).")

    success = 0
    already = 0
    failed = 0
    for idx, (key, line_ids) in enumerate(groups.items(), 1):
        line_list = sorted(line_ids)
        try:
            c20.call("account.move.line", "reconcile", ids=line_list)
            success += 1
        except OdooAPIError as e:
            if "already reconciled" in str(e).lower() or "already been reconciled" in str(e).lower():
                already += 1
            else:
                failed += 1
                mapper.record_error("reconcile", "account.move.line", None, f"grup {key} {line_list}: {e}")
                _log(f"⚠️ Grup {key} gagal direkonsiliasi: {e}", "warning")
        except Exception as e:
            failed += 1
            mapper.record_error("reconcile", "account.move.line", None, f"grup {key} {line_list}: {e}")
            _log(f"❌ Grup {key} exception: {e}", "error")
        if idx % 50 == 0 or idx == len(groups):
            _log(f"   🔗 [{idx}/{len(groups)}] grup diproses (baru: {success}, sudah: {already}, gagal: {failed})")

    _log(f"✅ Selesai: {success} grup direkonsiliasi, {already} sudah terekonsiliasi, {failed} gagal (tercatat di SQLite).",
         "success" if failed == 0 else "warning")
    return {"success": success, "already": already, "failed": failed}


def verify_pure_api(c19: OdooClient, c20: OdooClient, log=None):
    _log = log or (lambda text, level="info": print(text))
    _log(f"\n{'='*70}\n[VERIFIKASI] AUDIT INTEGRITAS & TRIAL BALANCE (PURE API)\n{'='*70}")
    # Hanya baris dari move berstatus posted, dan dibandingkan per kode akun
    # (bukan sekadar total tenant) agar selisih antar-akun tidak saling meniadakan.
    domain = [("parent_state", "=", "posted")]
    fields = ["account_id", "debit", "credit"]
    ml19 = c19.search_read("account.move.line", domain, fields)
    ml20 = c20.search_read("account.move.line", domain, fields)

    def by_code(client, lines):
        # display_name akun berbeda antar versi (Odoo 20 tanpa kode),
        # jadi ambil kode asli dari account.account, bukan parsing nama.
        acc_ids = list({l["account_id"][0] for l in lines if l.get("account_id")})
        code_map = {a["id"]: a["code"] for a in client.search_read(
            "account.account", [("id", "in", acc_ids)], ["code"])} if acc_ids else {}
        agg = {}
        for l in lines:
            if not l.get("account_id"):
                continue
            code = code_map.get(l["account_id"][0])
            if not code:
                continue
            a = agg.setdefault(code, [0.0, 0.0])
            a[0] += l["debit"]
            a[1] += l["credit"]
        return agg

    tb19, tb20 = by_code(c19, ml19), by_code(c20, ml20)
    deb19 = sum(v[0] for v in tb19.values()); deb20 = sum(v[0] for v in tb20.values())
    cred19 = sum(v[1] for v in tb19.values()); cred20 = sum(v[1] for v in tb20.values())

    mismatches = []
    for code in sorted(set(tb19) | set(tb20)):
        d19, c19_ = tb19.get(code, [0.0, 0.0])
        d20, c20_ = tb20.get(code, [0.0, 0.0])
        if abs(d19 - d20) > 0.01 or abs(c19_ - c20_) > 0.01:
            mismatches.append(code)

    _log(f"Total Debit  19: Rp {deb19:,.2f} | 20: Rp {deb20:,.2f} | Diff: Rp {deb20 - deb19:,.2f}")
    _log(f"Total Credit 19: Rp {cred19:,.2f} | 20: Rp {cred20:,.2f} | Diff: Rp {cred20 - cred19:,.2f}")
    _log(f"Akun dengan selisih: {len(mismatches)}" + (f" -> {', '.join(mismatches[:20])}" if mismatches else ""))

    balanced = abs(deb20 - deb19) < 0.01 and abs(cred20 - cred19) < 0.01 and not mismatches
    if balanced:
        _log("🎉 STATUS AUDIT: 100% IDENTIK & SEIMBANG (ZERO DISCREPANCY)!", "success")
    else:
        _log("⚠️ PERINGATAN: Terdapat selisih nominal atau selisih per akun!", "warning")
    return {"balanced": balanced, "mismatches": mismatches,
            "debit_diff": deb20 - deb19, "credit_diff": cred20 - cred19}


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
