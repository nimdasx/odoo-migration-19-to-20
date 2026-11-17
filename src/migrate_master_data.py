"""
Skrip Migrasi Master Data Akuntansi Odoo 19 -> 20 (Fase 2)
Prinsip: Match Existing First -> Create If Missing -> Map to SQLite
Model yang dimigrasikan:
  1. res.company
  2. res.currency
  3. account.tax.group & account.tax
  4. account.account (COA)
  5. account.journal
  6. res.partner
"""
import json
import os
import sys

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


def migrate_company(c19: OdooClient, c20: OdooClient, mapper: IdMapper):
    print("\n--- [1/6] Sinkronisasi Perusahaan (res.company) ---")
    comp19 = c19.search_read("res.company", [], ["id", "name", "currency_id"], limit=1)
    comp20 = c20.search_read("res.company", [], ["id", "name", "currency_id"], limit=1)

    if not comp19 or not comp20:
        print("⚠️ Gagal membaca data company!")
        return

    c19_data = comp19[0]
    c20_data = comp20[0]

    old_id = c19_data["id"]
    new_id = c20_data["id"]

    # Samakan nama company di Odoo 20 jika masih 'My Company'
    if c20_data["name"] != c19_data["name"]:
        print(f"🔄 Memperbarui nama company di Odoo 20 dari '{c20_data['name']}' menjadi '{c19_data['name']}'...")
        c20.write("res.company", [new_id], {"name": c19_data["name"]})

    mapper.set_mapping("res.company", old_id, new_id, notes=c19_data["name"])
    print(f"✅ Company terpetakan: [{old_id}] {c19_data['name']} -> [{new_id}]")


def migrate_currencies(c19: OdooClient, c20: OdooClient, mapper: IdMapper):
    print("\n--- [2/6] Sinkronisasi Mata Uang (res.currency) ---")
    curr19 = c19.search_read("res.currency", [], ["id", "name", "symbol", "active"])
    curr20 = c20.search_read("res.currency", [], ["id", "name", "symbol", "active"])

    map20 = {c["name"]: c for c in curr20}
    mapped_count = 0

    for c in curr19:
        name = c["name"]
        old_id = c["id"]
        if name in map20:
            new_id = map20[name]["id"]
            # Pastikan aktif di Odoo 20 jika di Odoo 19 aktif
            if c.get("active") and not map20[name].get("active"):
                c20.write("res.currency", [new_id], {"active": True})
            mapper.set_mapping("res.currency", old_id, new_id, notes=name)
            mapped_count += 1
        else:
            # Jika ada mata uang kustom yang belum ada di 20
            new_id = c20.create("res.currency", {
                "name": name,
                "symbol": c.get("symbol", name),
                "active": c.get("active", True),
            })[0]
            mapper.set_mapping("res.currency", old_id, new_id, notes=name)
            mapped_count += 1

    print(f"✅ {mapped_count} mata uang berhasil dipetakan ke SQLite.")


def migrate_taxes(c19: OdooClient, c20: OdooClient, mapper: IdMapper):
    print("\n--- [3/6] Sinkronisasi Grup Pajak & Master Pajak (account.tax) ---")
    
    # 1. Tax Groups
    tg19 = c19.search_read("account.tax.group", [], ["id", "name"])
    tg20 = c20.search_read("account.tax.group", [], ["id", "name"])
    tg_map20 = {g["name"]: g for g in tg20}

    for g in tg19:
        old_id = g["id"]
        name = g["name"]
        if name in tg_map20:
            new_id = tg_map20[name]["id"]
        else:
            new_id = c20.create("account.tax.group", {"name": name})[0]
            tg_map20[name] = {"id": new_id, "name": name}
        mapper.set_mapping("account.tax.group", old_id, new_id, notes=name)

    print(f"✅ {len(tg19)} tax group berhasil dipetakan.")

    # 2. Taxes
    t19 = c19.search_read("account.tax", [], [
        "id", "name", "type_tax_use", "amount", "amount_type", "tax_group_id", "description", "active"
    ])
    t20 = c20.search_read("account.tax", [], [
        "id", "name", "type_tax_use", "amount", "amount_type", "tax_group_id", "description", "active"
    ])

    # Kunci pencocokan: (name, type_tax_use)
    tax_by_name = {(t["name"], t["type_tax_use"]): t for t in t20}
    # Fallback pencocokan: (type_tax_use, round(amount, 2))
    tax_by_rate = {(t["type_tax_use"], round(t["amount"], 2)): t for t in t20}

    mapped_tax = 0
    created_tax = 0

    for t in t19:
        old_id = t["id"]
        name = t["name"]
        use = t["type_tax_use"]
        amt = round(t["amount"], 2)

        key_name = (name, use)
        key_rate = (use, amt)

        new_id = None
        if key_name in tax_by_name:
            new_id = tax_by_name[key_name]["id"]
            matched_via = "exact name"
        elif key_rate in tax_by_rate:
            new_id = tax_by_rate[key_rate]["id"]
            matched_via = f"rate {amt}%"
        else:
            # Buat pajak baru di Odoo 20
            old_tg_id = t["tax_group_id"][0] if isinstance(t.get("tax_group_id"), (list, tuple)) else t.get("tax_group_id")
            new_tg_id = mapper.get_new_id("account.tax.group", old_tg_id)

            vals = {
                "name": name,
                "type_tax_use": use,
                "amount": t["amount"],
                "amount_type": t.get("amount_type", "percent"),
                "active": t.get("active", True),
            }
            if new_tg_id:
                vals["tax_group_id"] = new_tg_id
            if t.get("description"):
                vals["description"] = t["description"]

            new_id = c20.create("account.tax", vals)[0]
            created_tax += 1
            matched_via = "created new"

        mapper.set_mapping("account.tax", old_id, new_id, notes=f"{name} ({use}) via {matched_via}")
        mapped_tax += 1

    print(f"✅ {mapped_tax} pajak terpetakan (Baru dibuat: {created_tax}).")


def migrate_accounts(c19: OdooClient, c20: OdooClient, mapper: IdMapper):
    print("\n--- [4/6] Sinkronisasi Bagan Akun (account.account) ---")
    accs19 = c19.search_read("account.account", [], [
        "id", "code", "name", "account_type", "reconcile", "currency_id"
    ])
    accs20 = c20.search_read("account.account", [], [
        "id", "code", "name", "account_type"
    ])

    map20 = {a["code"]: a for a in accs20}
    created_count = 0
    matched_count = 0

    new_comp_id = mapper.get_new_id("res.company", 1) or 1

    for a in accs19:
        old_id = a["id"]
        code = a["code"]
        name = a["name"]
        acc_type = a["account_type"]

        if code in map20:
            new_id = map20[code]["id"]
            if map20[code].get("name") != name:
                c20.write("account.account", [new_id], {"name": name})
            matched_count += 1
        else:
            # Buat akun baru di Odoo 20
            vals = {
                "code": code,
                "name": name,
                "account_type": acc_type,
                "company_ids": [(6, 0, [new_comp_id])],
            }
            if a.get("reconcile"):
                vals["reconcile"] = True
            if a.get("currency_id"):
                old_curr = a["currency_id"][0] if isinstance(a["currency_id"], (list, tuple)) else a["currency_id"]
                new_curr = mapper.get_new_id("res.currency", old_curr)
                if new_curr:
                    vals["currency_id"] = new_curr

            new_id = c20.create("account.account", vals)[0]
            map20[code] = {"id": new_id, "code": code, "name": name, "account_type": acc_type}
            created_count += 1

        mapper.set_mapping("account.account", old_id, new_id, notes=f"{code} - {name}")

    print(f"✅ Selesai: {len(accs19)} akun terpetakan ({matched_count} cocok eksisting, {created_count} akun kustom dibuat).")


def migrate_journals(c19: OdooClient, c20: OdooClient, mapper: IdMapper):
    print("\n--- [5/6] Sinkronisasi Jurnal Akuntansi (account.journal) ---")
    j19 = c19.search_read("account.journal", [], [
        "id", "code", "name", "type", "default_account_id", "currency_id"
    ])
    j20 = c20.search_read("account.journal", [], [
        "id", "code", "name", "type", "default_account_id"
    ])

    map20 = {j["code"]: j for j in j20}
    created_count = 0
    matched_count = 0
    new_comp_id = mapper.get_new_id("res.company", 1) or 1

    for j in j19:
        old_id = j["id"]
        code = j["code"]
        name = j["name"]
        j_type = j["type"]

        if code in map20:
            new_id = map20[code]["id"]
            if map20[code].get("name") != name:
                c20.write("account.journal", [new_id], {"name": name})
            matched_count += 1
        else:
            # Cari mapped default_account_id
            new_acc_id = None
            if j.get("default_account_id"):
                old_acc_id = j["default_account_id"][0] if isinstance(j["default_account_id"], (list, tuple)) else j["default_account_id"]
                new_acc_id = mapper.get_new_id("account.account", old_acc_id)

            vals = {
                "name": name,
                "code": code,
                "type": j_type,
                "company_id": new_comp_id,
            }
            if new_acc_id:
                vals["default_account_id"] = new_acc_id

            if j.get("currency_id"):
                old_curr = j["currency_id"][0] if isinstance(j["currency_id"], (list, tuple)) else j["currency_id"]
                new_curr = mapper.get_new_id("res.currency", old_curr)
                if new_curr:
                    vals["currency_id"] = new_curr

            new_id = c20.create("account.journal", vals)[0]
            map20[code] = {"id": new_id, "code": code, "name": name, "type": j_type}
            created_count += 1

        mapper.set_mapping("account.journal", old_id, new_id, notes=f"{code} - {name} ({j_type})")

    print(f"✅ Selesai: {len(j19)} jurnal terpetakan ({matched_count} cocok eksisting, {created_count} jurnal baru dibuat).")


def migrate_partners(c19: OdooClient, c20: OdooClient, mapper: IdMapper):
    print("\n--- [6/6] Sinkronisasi Kontak & Partner (res.partner) ---")
    p19 = c19.search_read("res.partner", [], [
        "id", "name", "email", "phone", "vat", "is_company", "street", "city", "website"
    ])
    p20 = c20.search_read("res.partner", [], [
        "id", "name", "email", "phone", "vat", "is_company"
    ])

    # Index partner Odoo 20 untuk pencocokan cepat
    by_vat = {p["vat"].strip(): p for p in p20 if p.get("vat")}
    by_email = {p["email"].strip().lower(): p for p in p20 if p.get("email")}
    by_name = {p["name"].strip().lower(): p for p in p20 if p.get("name")}

    created_count = 0
    matched_count = 0

    for p in p19:
        old_id = p["id"]
        name = (p["name"] or "").strip()
        vat = (p.get("vat") or "").strip()
        email = (p.get("email") or "").strip().lower()

        new_id = None
        # 1. Match by VAT (NPWP)
        if vat and vat in by_vat:
            new_id = by_vat[vat]["id"]
            matched_count += 1
        # 2. Match by Email
        elif email and email in by_email:
            new_id = by_email[email]["id"]
            matched_count += 1
        # 3. Match by Exact Name
        elif name.lower() in by_name:
            new_id = by_name[name.lower()]["id"]
            matched_count += 1
        else:
            # Create partner baru di Odoo 20
            vals = {
                "name": name or "-",
                "is_company": p.get("is_company", False),
            }
            if email: vals["email"] = email
            if p.get("phone"): vals["phone"] = p["phone"]
            if vat: vals["vat"] = vat
            if p.get("street"): vals["street"] = p["street"]
            if p.get("city"): vals["city"] = p["city"]
            if p.get("website"): vals["website"] = p["website"]

            new_id = c20.create("res.partner", vals)[0]
            # Update index lokal
            p_obj = {"id": new_id, "name": name, "email": email, "vat": vat}
            if vat: by_vat[vat] = p_obj
            if email: by_email[email] = p_obj
            if name: by_name[name.lower()] = p_obj
            created_count += 1

        mapper.set_mapping("res.partner", old_id, new_id, notes=name)

    print(f"✅ Selesai: {len(p19)} partner terpetakan ({matched_count} cocok eksisting, {created_count} baru dibuat).")


def main():
    print(f"{'='*60}")
    print("🚀 MEMULAI MIGRASI MASTER DATA AKUNTANSI (FASE 2)")
    print(f"{'='*60}")

    config = load_config()
    c19 = OdooClient(config["source_odoo_19"]["url"], config["source_odoo_19"]["db"], config["source_odoo_19"]["api_key"])
    c20 = OdooClient(config["target_odoo_20"]["url"], config["target_odoo_20"]["db"], config["target_odoo_20"]["api_key"])
    mapper = IdMapper.from_config(config)
    print(f"📁 Menggunakan SQLite Mapping DB: {mapper.db_path}")

    try:
        migrate_company(c19, c20, mapper)
        migrate_currencies(c19, c20, mapper)
        migrate_taxes(c19, c20, mapper)
        migrate_accounts(c19, c20, mapper)
        migrate_journals(c19, c20, mapper)
        migrate_partners(c19, c20, mapper)

        print(f"\n{'='*60}")
        print("🎉 FASE 2 SELESAI DENGAN SUKSES!")
        print(f"{'='*60}")
        print(f"Total Mapping Tercatat di SQLite:")
        for model in ["res.company", "res.currency", "account.tax.group", "account.tax", "account.account", "account.journal", "res.partner"]:
            cnt = mapper.count(model)
            print(f"  - {model:25}: {cnt:>4} records")
        print(f"{'='*60}")
        print("Kamus relasi ID sudah lengkap dan siap untuk migrasi Transaksi Jurnal (Fase 3).")

    except OdooAPIError as e:
        print(f"\n❌ Error API Odoo (HTTP {e.status_code}): {e}")
        if e.details:
            print(f"   Detail: {e.details}")
    except Exception as e:
        print(f"\n❌ Terjadi kesalahan tak terduga: {e}")
        import traceback
        traceback.print_exc()
    finally:
        mapper.close()


if __name__ == "__main__":
    main()
