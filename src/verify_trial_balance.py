"""
Skrip Verifikasi Neraca Saldo (Trial Balance Audit) Odoo 19 vs Odoo 20
Menghitung total Debit, Credit, dan Saldo per Akun pada transaksi berstatus 'posted'.
Memastikan selisih akhir antar kedua database adalah 0 (Identik 100%).
"""
import json
import os
import sys
from collections import defaultdict
from typing import Dict, Any

# Tambahkan src ke sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from odoo_client import OdooClient


def load_config(config_path: str = "config.json") -> dict:
    if not os.path.exists(config_path):
        print(f"❌ File konfigurasi {config_path} tidak ditemukan!")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_trial_balance(client: OdooClient, name: str) -> Dict[str, Dict[str, Any]]:
    print(f"📊 Menghitung saldo akun dari {name}...")
    
    # Ambil baris jurnal dengan parent_state = 'posted'
    lines = client.search_read(
        "account.move.line",
        [("parent_state", "=", "posted")],
        fields=["id", "account_id", "debit", "credit"],
        order="id asc"
    )

    tb = defaultdict(lambda: {"name": "", "debit": 0.0, "credit": 0.0, "balance": 0.0})

    # display_name akun berbeda antar versi (Odoo 20 tanpa kode di depan),
    # jadi ambil kode & nama asli dari account.account, bukan parsing nama.
    acc_ids = list({l["account_id"][0] for l in lines if l.get("account_id")})
    acc_map = {a["id"]: a for a in client.search_read(
        "account.account", [("id", "in", acc_ids)], ["code", "name"])} if acc_ids else {}

    for l in lines:
        if not l.get("account_id"):
            continue
        acc = acc_map.get(l["account_id"][0])
        if not acc:
            continue
        code = acc["code"]

        tb[code]["name"] = acc["name"]
        tb[code]["debit"] += l["debit"]
        tb[code]["credit"] += l["credit"]
        tb[code]["balance"] += (l["debit"] - l["credit"])

    return dict(tb)


def compare_trial_balance():
    print(f"{'='*75}")
    print("⚖️  AUDIT VERIFIKASI NERACA SALDO (TRIAL BALANCE): ODOO 19 VS ODOO 20")
    print(f"{'='*75}")

    config = load_config()
    c19 = OdooClient(config["source_odoo_19"]["url"], config["source_odoo_19"]["db"], config["source_odoo_19"]["api_key"])
    c20 = OdooClient(config["target_odoo_20"]["url"], config["target_odoo_20"]["db"], config["target_odoo_20"]["api_key"])

    tb19 = get_trial_balance(c19, "Source Odoo 19.0")
    tb20 = get_trial_balance(c20, "Target Odoo 20.0")

    all_codes = sorted(list(set(tb19.keys()) | set(tb20.keys())))

    total_deb19 = sum(v["debit"] for v in tb19.values())
    total_crd19 = sum(v["credit"] for v in tb19.values())
    total_deb20 = sum(v["debit"] for v in tb20.values())
    total_crd20 = sum(v["credit"] for v in tb20.values())

    diff_deb_total = abs(total_deb19 - total_deb20)
    diff_crd_total = abs(total_crd19 - total_crd20)

    print(f"\n{'Kode':<12} | {'Nama Akun':<28} | {'Debit 19':>14} | {'Debit 20':>14} | {'Selisih':>10}")
    print(f"{'-'*75}")

    mismatches = 0
    for code in all_codes:
        d19 = tb19.get(code, {}).get("debit", 0.0)
        crd19 = tb19.get(code, {}).get("credit", 0.0)
        d20 = tb20.get(code, {}).get("debit", 0.0)
        crd20 = tb20.get(code, {}).get("credit", 0.0)

        name = tb19.get(code, {}).get("name") or tb20.get(code, {}).get("name") or "-"
        name_trimmed = name[:28]

        diff_deb = abs(d19 - d20)
        diff_crd = abs(crd19 - crd20)

        if diff_deb > 0.01 or diff_crd > 0.01:
            mismatches += 1
            print(f"{code:<12} | {name_trimmed:<28} | {d19:>14,.2f} | {d20:>14,.2f} | ❌ {diff_deb:>8,.2f}")
        else:
            # Tampilkan beberapa akun sampel jika cocok
            pass

    print(f"{'-'*75}")
    print(f"{'TOTAL DEBIT ':>42} | {total_deb19:>14,.2f} | {total_deb20:>14,.2f} | {diff_deb_total:>10,.2f}")
    print(f"{'TOTAL CREDIT':>42} | {total_crd19:>14,.2f} | {total_crd20:>14,.2f} | {diff_crd_total:>10,.2f}")
    print(f"{'='*75}")

    if mismatches == 0 and diff_deb_total < 0.01 and diff_crd_total < 0.01:
        print("🎉 HASIL AUDIT: NERACA SALDO ODOO 19 & ODOO 20 PERSIS IDENTIK (SELISIH = 0)! 🎉")
        print("Semua akun, mutasi debit, dan mutasi credit seimbang 100% tanpa selisih sepeser pun.")
        return True
    else:
        print(f"⚠️ Ditemukan {mismatches} akun yang memiliki selisih angka.")
        return False


if __name__ == "__main__":
    compare_trial_balance()
