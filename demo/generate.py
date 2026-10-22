"""Generate realistic dummy data for the demo companies.

Each company's data is shaped differently (file formats, column names, date formats, currencies,
delimiters, a SQL database) and contains planted business situations the rules should catch, a
few invalid rows the platform should skip, and everything else healthy so nothing else fires.
Dates are relative to today in each company's timezone, so the scenarios are always current."""
import csv
import math
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DAYS = 60

# What the rules must find in each company, as (work type, group). Nothing else may fire.
EXPECTED = {
    "homemonde": {("STOCKOUT_RISK", "HM-204"), ("PRODUCTION_SHORTFALL", "STITCH-1"), ("ROAS_DROP", "meta"),
                  ("PROFIT_DROP", "None"), ("CARRIER_DELAYS", "Delhivery"), ("ABSENTEEISM", "stitching"),
                  ("CHANNEL_GROWTH", "d2c"), ("PO_OVERDUE", "PO-7731"), ("INVOICE_MISMATCH", "RLC-2291"),
                  ("DUPLICATE_INVOICE", "V-10462")},
    "kavya": {("EXPIRY_RISK", "KF-MANGO-850"), ("STOCKOUT_RISK", "KF-POHA-1KG")},
    "atlas": {("DATA_CONFLICT", "BRK-1042@CHI"), ("SUPPLIER_DELAY", "Midwest Friction Co"), ("STOCKOUT_RISK", "ALT-2210")},
    "bloom": {("RETURN_SPIKE", "BL-DRESS-0412"), ("STOCKOUT_RISK", "BL-TEE-0107")},
}
# Sources that are deliberately broken, to show visible failure handling.
FAILING_SOURCES = {"bloom": {"ads"}}


def _today(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()


def _daily(rng: random.Random, base: float, day_index: int) -> int:
    """A demand curve with weekly seasonality and noise, never negative."""
    weekly = 1 + 0.2 * math.sin(2 * math.pi * (day_index % 7) / 7)
    return max(0, round(base * weekly + rng.gauss(0, base * 0.15)))


def _write_csv(path: Path, rows: list[dict], delimiter: str = ","):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=delimiter)
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- HomeMonde (CSV, ISO dates, INR)

def homemonde(out: Path) -> dict:
    rng, t = random.Random(101), _today("Asia/Kolkata")
    products = ["Cushion cover", "Table runner", "Bedsheet set", "Curtain panel", "Cotton rug", "Throw blanket",
                "Napkin set", "Quilt", "Pillow cover", "Bath towel"]
    prints = ["Indigo", "Sanganeri", "Bagru", "Ikat", "Kantha", "Dabu"]
    channels = {"amazon": 0.4, "d2c": 0.3, "flipkart": 0.15, "myntra": 0.1, "wholesale": 0.05}
    skus = {f"HM-{100 + i}": (f"{rng.choice(prints)} {rng.choice(products).lower()}", rng.uniform(4, 45)) for i in range(1, 41)}
    inventory, lines, n = [], [], 0
    for sku, (name, base) in skus.items():
        inventory.append({"sku": sku, "location": "JAIPUR-WH", "product_name": name, "available": round(base * rng.uniform(18, 55)),
                          "reserved": rng.randint(0, 30), "incoming": rng.choice([0, 0, 200, 500])})
        for d in range(DAYS):
            for ch, share in channels.items():
                q = _daily(rng, base * share * (1.4 if ch == "d2c" and d < 7 else 1), d)  # planted: D2C is growing
                if q:
                    n += 1
                    lines.append({"order_id": f"HMO{n:06d}", "line_no": 1, "order_date": (t - timedelta(days=d)).isoformat(),
                                  "channel": ch, "sku": sku, "quantity": q, "net_amount": q * rng.choice([449, 699, 899, 1299]),
                                  "status": rng.choices(["delivered", "shipped", "confirmed", "cancelled"], [80, 10, 6, 4])[0],
                                  "customer_email": f"buyer{n}@example.com"})
    # planted: HM-204 sells 95/day this week (71/day over 30 days) with only 280 sellable
    inventory.append({"sku": "HM-204", "location": "JAIPUR-WH", "product_name": "Indigo block-print cushion cover",
                      "available": 320, "reserved": 40, "incoming": 0})
    for d in range(DAYS):
        split = {"amazon": 44, "d2c": 38, "flipkart": 13} if d < 7 else {"amazon": 30, "d2c": 27, "flipkart": 7 if d != 7 else 0}
        if d >= 30:
            split = {"amazon": 30, "d2c": 26, "flipkart": 9}
        for ch, q in split.items():
            if q:
                n += 1
                lines.append({"order_id": f"HMO{n:06d}", "line_no": 1, "order_date": (t - timedelta(days=d)).isoformat(), "channel": ch,
                              "sku": "HM-204", "quantity": q, "net_amount": q * 849, "status": "delivered", "customer_email": f"buyer{n}@example.com"})
    # data quality: a few rows the platform must reject without losing the batch
    lines += [{**lines[0], "order_id": "HMOBAD1", "quantity": "two"}, {**lines[1], "order_id": "HMOBAD2", "sku": ""},
              {**lines[2], "order_id": "HMOBAD3", "order_date": "yesterday"}]
    batches = [{"batch_id": f"PB-{90 + i}", "sku": sku, "quantity": rng.choice([500, 800, 1200]), "planned_start": (t + timedelta(days=rng.randint(1, 20))).isoformat(),
                "run_days": rng.choice([1, 2, 3]), "status": rng.choice(["PLANNED", "PLANNED", "RUNNING"]), "blocked_reason": "",
                "material_code": rng.choice(["FAB-CT-01", "FAB-LN-03", "FAB-CT-04"]), "material_required": rng.choice([300, 600, 900])}
               for i, sku in enumerate(rng.sample(sorted(skus), 8))]
    batches.append({"batch_id": "PB-118", "sku": "HM-204", "quantity": 1500, "planned_start": (t + timedelta(days=1)).isoformat(), "run_days": 2,
                    "status": "BLOCKED", "blocked_reason": "waiting for fabric FAB-CT-02", "material_code": "FAB-CT-02", "material_required": 800})
    pos = [{"po_number": f"PO-77{10 + i}", "material_code": m, "supplier": s, "quantity": q, "unit": "m", "due_date": (t + timedelta(days=rng.randint(2, 15))).isoformat(),
            "revised_eta": "", "status": "OPEN", "unit_price": p}
           for i, (m, s, q, p) in enumerate([("FAB-CT-01", "Sanganer Textiles", 1500, 150), ("FAB-LN-03", "Rajasthan Linen Co", 900, 240),
                                             ("FAB-CT-04", "Bagru Cotton Mills", 1200, 170)])]
    pos.append({"po_number": "PO-7731", "material_code": "FAB-CT-02", "supplier": "Sanganer Textiles", "quantity": 2000, "unit": "m",
                "due_date": (t - timedelta(days=4)).isoformat(), "revised_eta": (t + timedelta(days=4)).isoformat(), "status": "LATE", "unit_price": 158})
    # Accounts payable: PO-7711 was short-delivered (810 of 900 m) but billed in full; Bagru's BCM-0457 was entered twice.
    receipts = [{"grn_number": "GRN-5501", "po_number": "PO-7710", "material_code": "FAB-CT-01", "quantity": 600, "received_date": (t - timedelta(days=12)).isoformat()},
                {"grn_number": "GRN-5507", "po_number": "PO-7711", "material_code": "FAB-LN-03", "quantity": 810, "received_date": (t - timedelta(days=6)).isoformat()},
                {"grn_number": "GRN-5509", "po_number": "PO-7712", "material_code": "FAB-CT-04", "quantity": 1200, "received_date": (t - timedelta(days=5)).isoformat()}]
    invoices = [{"voucher_id": vid, "invoice_number": num, "supplier": sup, "po_number": po, "material_code": mat, "quantity": q, "unit_price": up,
                 "total_amount": q * up, "invoice_date": (t - timedelta(days=ago)).isoformat(), "status": st}
                for vid, num, sup, po, mat, q, up, ago, st in [
                    ("V-10431", "ST-1188", "Sanganer Textiles", "PO-7710", "FAB-CT-01", 600, 150, 11, "PAID"),
                    ("V-10448", "RLC-2291", "Rajasthan Linen Co", "PO-7711", "FAB-LN-03", 900, 240, 5, "OPEN"),
                    ("V-10455", "BCM-0457", "Bagru Cotton Mills", "PO-7712", "FAB-CT-04", 1200, 170, 4, "OPEN"),
                    ("V-10462", "BCM-0457", "Bagru Cotton Mills", "PO-7712", "FAB-CT-04", 1200, 170, 2, "OPEN")]]
    scorecard = [{"supplier": s, "material_code": m, "on_time_rate": r, "pos_last_90d": c, "approved": "yes", "lead_time_days": lt, "unit_price": p}
                 for s, m, r, c, lt, p in [("Sanganer Textiles", "FAB-CT-02", 0.71, 17, 12, 158), ("Bagru Cotton Mills", "FAB-CT-02", 0.94, 17, 2, 162),
                                           ("Sanganer Textiles", "FAB-CT-01", 0.83, 12, 10, 150), ("Rajasthan Linen Co", "FAB-LN-03", 0.9, 9, 8, 240),
                                           ("Bagru Cotton Mills", "FAB-CT-04", 0.95, 14, 3, 170)]]
    d = out / "homemonde"
    _write_csv(d / "inventory.csv", inventory)
    _write_csv(d / "order_lines.csv", lines)
    _write_csv(d / "production_batches.csv", batches)
    _write_csv(d / "purchase_orders.csv", pos)
    _write_csv(d / "goods_receipts.csv", receipts)
    _write_csv(d / "supplier_invoices.csv", invoices)
    _write_csv(d / "supplier_scorecard.csv", scorecard)
    sold = {}  # the accounts book the revenue the order system recorded, as a real ledger would
    for ln in lines:
        if ln["status"] in ("delivered", "shipped", "confirmed"):
            sold[ln["order_date"]] = sold.get(ln["order_date"], 0) + ln["net_amount"]
    _homemonde_operations(d, rng, t, sold)
    return {"HOMEMONDE_EXPORTS_DIR": str(d)}


def _homemonde_operations(d: Path, rng: random.Random, t: date, sold: dict[str, float]):
    """Factory, marketing, ledger, courier and HRMS exports. One connected story is planted: absences in
    stitching hold back the STITCH-1 line; profit falls because marketplace fees and Meta ad spend rose while
    Meta ads stopped paying back; and one courier delivers late. Everything else is healthy."""
    staff = {"stitching": 40, "printing": 25, "warehouse": 20, "office": 15}
    attendance = []
    for dd in range(DAYS):
        day = (t - timedelta(days=dd)).isoformat()
        for dept, n in staff.items():
            absent_rate = 0.3 if dept == "stitching" and dd < 7 else 0.05
            for i in range(n):
                status = "absent" if rng.random() < absent_rate else ("leave" if rng.random() < 0.02 else "present")
                attendance.append({"att_date": day, "employee_id": f"E{dept[:2].upper()}{i:03d}", "department": dept, "status": status})
    output = []
    for dd in range(DAYS):
        day = (t - timedelta(days=dd)).isoformat()
        for line, plan in (("PRINT-1", 500), ("CUT-1", 600), ("STITCH-1", 400)):
            hit = line == "STITCH-1" and dd < 7
            produced = round(plan * (rng.uniform(0.66, 0.74) if hit else rng.uniform(0.95, 1.02)))
            reason = rng.choice(["staff shortage", "staff shortage", "machine breakdown"]) if hit else rng.choice(["", "", "changeover", "power cut"])
            output.append({"log_date": day, "line": line, "planned_units": plan, "produced_units": produced,
                           "downtime_minutes": rng.randint(90, 160) if hit else rng.randint(0, 40), "downtime_reason": reason})
    spend = []
    for dd in range(DAYS):
        day = (t - timedelta(days=dd)).isoformat()
        for channel, campaigns in (("meta", ["prospecting", "retargeting"]), ("google", ["search", "shopping"]), ("amazon_ads", ["sponsored"])):
            for c in campaigns:
                base = 10000 / len(campaigns) if channel == "meta" and dd < 7 else 8000 / len(campaigns)
                cost = round(base * rng.uniform(0.9, 1.1))
                roas = 1.8 if channel == "meta" and dd < 7 else rng.uniform(3.6, 4.4)
                spend.append({"spend_date": day, "channel": channel, "campaign": c, "spend": cost, "attributed_revenue": round(cost * roas)})
    ledger, n = [], 0
    for dd in range(DAYS):
        day = (t - timedelta(days=dd)).isoformat()
        recent = dd < 30
        rng.uniform(0.95, 1.05)  # drawn as before, so the rest of the generated data stays the same
        revenue = sold.get(day, 0.0)
        shares = {"cogs": 0.45, "marketplace_fees": 0.18 if recent else 0.12, "ad_spend": 0.12 if recent else 0.08,
                  "shipping": 0.05, "returns": 0.02}
        lines = [("revenue", revenue)] + [(k, -revenue * v) for k, v in shares.items()] + [("salaries", -22000), ("overheads", -8000)]
        for account, amount in lines:
            n += 1
            ledger.append({"entry_id": f"JV{n:06d}", "entry_date": day, "account": account, "channel": "all", "amount": round(amount, 2)})
    ships, n = [], 0
    for dd in range(DAYS):
        ship_day = t - timedelta(days=dd)
        for carrier, per_day in (("Delhivery", 40), ("BlueDart", 30), ("Ekart", 35), ("Shadowfax", 20)):
            for _ in range(per_day):
                n += 1
                late = rng.random() < (0.45 if carrier == "Delhivery" and dd < 14 else 0.06)
                transit = rng.randint(4, 6) if late else rng.randint(1, 3)
                promised, delivered = ship_day + timedelta(days=3), ship_day + timedelta(days=transit)
                rto = rng.random() < 0.02
                status = "rto" if rto else ("delivered" if delivered <= t else "in_transit")
                ships.append({"shipment_id": f"SH{n:07d}", "order_id": f"HMO{n:06d}", "channel": rng.choice(["amazon", "d2c", "flipkart"]),
                              "carrier": carrier, "ship_date": ship_day.isoformat(), "promised_date": promised.isoformat(),
                              "delivered_date": delivered.isoformat() if status == "delivered" else "", "status": status})
    _write_csv(d / "attendance.csv", attendance)
    _write_csv(d / "production_output.csv", output)
    _write_csv(d / "marketing_spend.csv", spend)
    _write_csv(d / "ledger.csv", ledger)
    _write_csv(d / "shipments.csv", ships)


# ---------------------------------------------------------------- Kavya Foods (SQL database)

def kavya(out: Path) -> dict:
    rng, t = random.Random(202), _today("Asia/Kolkata")
    names = ["POHA-500", "BESAN-1KG", "IDLI-MIX", "DOSA-MIX", "UPMA-MIX", "MAKHANA-90", "CHIPS-MASALA", "CHIPS-SALTED", "BHUJIA-400",
             "MIXTURE-400", "JAGGERY-1KG", "RAVA-1KG", "SAMBAR-MIX", "RASAM-MIX", "PICKLE-MANGO", "PICKLE-LIME", "PAPAD-200", "VERMICELLI",
             "GULAB-JAMUN-MIX", "KHEER-MIX", "COCONUT-POWDER", "TAMARIND-PASTE", "GARAM-MASALA", "CHAI-MASALA", "PEANUT-CHIKKI", "MURUKKU"]
    items = {f"KF-{n}": rng.uniform(15, 160) for n in names}
    channels = {"GT": 0.55, "MT": 0.3, "ECOM": 0.15}
    db = out / "kavya" / "kavya_erp.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE inv_batch (item_code TEXT, batch_no TEXT, wh_code TEXT, qty_on_hand INTEGER, exp_dt TEXT);
        CREATE TABLE sales_line (inv_no TEXT, line_no INTEGER, inv_date TEXT, item_code TEXT, qty INTEGER, amount REAL, channel_code TEXT, status TEXT);
        CREATE TABLE mfg_order (mo_no TEXT, item_code TEXT, planned_qty INTEGER, start_dt TEXT, status TEXT, remarks TEXT);""")
    batches, sales, n = [], [], 0
    for item, base in items.items():
        total, k = base * rng.uniform(18, 45), rng.randint(2, 4)
        for b in range(k):
            batches.append((item, f"B{rng.randint(1000, 9999)}", rng.choice(["NGP-01", "NGP-02"]), round(total / k), (t + timedelta(days=rng.randint(45, 300))).isoformat()))
    for item, base in {**items, "KF-MANGO-850": 6, "KF-POHA-1KG": 120}.items():
        for d in range(DAYS):
            for ch, share in channels.items():
                q = _daily(rng, base * share, d) if item != "KF-POHA-1KG" or d < 7 else _daily(rng, 95 * share, d)
                if q:
                    n += 1
                    sales.append((f"SI{n:07d}", 1, (t - timedelta(days=d)).isoformat(), item, q, q * rng.choice([42.0, 65.0, 120.0]), ch,
                                  rng.choices(["INVOICED", "DELIVERED", "CANCELLED"], [60, 37, 3])[0]))
    # planted: mango pulp has 900 units expiring in 18 days but sells ~6/day; poha is selling far faster than stock
    batches += [("KF-MANGO-850", "B7781", "NGP-01", 900, (t + timedelta(days=18)).isoformat()),
                ("KF-MANGO-850", "B7902", "NGP-02", 400, (t + timedelta(days=200)).isoformat()),
                ("KF-POHA-1KG", "B8120", "NGP-01", 300, (t + timedelta(days=120)).isoformat())]
    sales.append(("SIBAD1", 1, t.isoformat(), "KF-RAVA-1KG", None, 0.0, "GT", "INVOICED"))  # missing quantity: rejected
    orders = [(f"MO-{500 + i}", item, rng.choice([2000, 3000, 5000]), (t + timedelta(days=rng.randint(1, 12))).isoformat(), "PLANNED", "")
              for i, item in enumerate(rng.sample(sorted(items), 6))]
    orders.append(("MO-561", "KF-POHA-1KG", 4000, (t + timedelta(days=2)).isoformat(), "ON_HOLD", "awaiting printed packaging film from vendor"))
    con.executemany("INSERT INTO inv_batch VALUES (?,?,?,?,?)", batches)
    con.executemany("INSERT INTO sales_line VALUES (?,?,?,?,?,?,?,?)", sales)
    con.executemany("INSERT INTO mfg_order VALUES (?,?,?,?,?,?)", orders)
    con.commit()
    con.close()
    return {"KAVYA_ERP_URL": f"sqlite:///{db}"}


# ---------------------------------------------------------------- Atlas Auto Parts (US formats)

def atlas(out: Path) -> dict:
    rng, t = random.Random(303), _today("America/Chicago")
    us = lambda d: d.strftime("%m/%d/%Y")  # noqa: E731
    parts = {f"BRK-{1000 + i}": rng.uniform(4, 30) for i in [*range(1, 16), 42]}
    parts.update({f"ALT-{2200 + i}": rng.uniform(2, 12) for i in range(1, 9)})
    parts.update({f"FLT-{3000 + i}": rng.uniform(10, 50) for i in range(1, 13)})
    customers = ["Main St Auto", "Fleetline Logistics", "QuickFix Garage", "Lakeside Motors", "Prairie Truck Service", "Tex Brake & Tire"]
    wms, erp, sales, n = [], [], [], 0
    for part, base in {**parts, "ALT-2210": 18}.items():
        total = 70 if part == "ALT-2210" else round(base * rng.uniform(18, 50))
        for dc, share in (("CHI", 0.6), ("DAL", 0.4)):
            q, key = round(total * share), f"{part}@{dc}"
            wms.append({"Stock Key": key, "Part Number": part, "DC": dc, "Bin": f"{dc}-{rng.randint(1, 40):02d}", "Qty On Hand": f"{q:,}"})
            erp.append({"ITEM_LOC": key, "ONHAND_QTY": q, "LAST_COUNT": us(t - timedelta(days=rng.randint(1, 30)))})
        for d in range(DAYS):
            q = _daily(rng, base, d)
            if q:
                n += 1
                price = q * rng.choice([24.5, 89.99, 145.0, 312.75])
                sales.append({"Order #": f"SO-{40000 + n}", "Line": 1, "Order Date": us(t - timedelta(days=d)), "Customer": rng.choice(customers),
                              "Part Number": part, "DC": rng.choice(["CHI", "DAL"]), "Qty": q, "Ext Price": f"${price:,.2f}",
                              "Status": rng.choices(["Invoiced", "Shipped", "Open", "Cancelled"], [70, 15, 10, 5])[0]})
    # planted: the WMS and ERP disagree for one stock key; a 1-unit difference elsewhere is within tolerance
    for row in wms:
        if row["Stock Key"] == "BRK-1042@CHI":
            row["Qty On Hand"] = "1,200"
    for row in erp:
        if row["ITEM_LOC"] == "BRK-1042@CHI":
            row["ONHAND_QTY"] = 1050
        if row["ITEM_LOC"] == "FLT-3005@DAL":
            row["ONHAND_QTY"] += 1
    sales += [{**sales[3], "Order #": "SO-BAD1", "Order Date": "13/45/2026"}, {**sales[4], "Order #": "SO-BAD2", "Qty": ""}]
    vendors = ["Midwest Friction Co", "Lone Star Electric", "Great Lakes Filtration", "Ohio Valley Components"]
    pos = []
    for i in range(14):
        v = vendors[i % 4]
        pos.append({"PO": f"PO-{88100 + i}", "Vendor": v, "Part Number": rng.choice(sorted(parts)), "Qty Ordered": rng.choice([50, 100, 250]),
                    "Promise Date": us(t + timedelta(days=rng.randint(3, 25))), "Status": "Open"})
    # planted: three Midwest Friction POs are past their promise date; Lone Star has only one (below the rule threshold)
    late = [("PO-88201", "Midwest Friction Co", "BRK-1042", 9), ("PO-88202", "Midwest Friction Co", "BRK-1007", 6),
            ("PO-88203", "Midwest Friction Co", "BRK-1011", 3), ("PO-88204", "Lone Star Electric", "ALT-2210", 2)]
    pos += [{"PO": po, "Vendor": v, "Part Number": p, "Qty Ordered": 200, "Promise Date": us(t - timedelta(days=d)), "Status": "Open"} for po, v, p, d in late]
    pos.append({"PO": "PO-87990", "Vendor": "Midwest Friction Co", "Part Number": "BRK-1003", "Qty Ordered": 100, "Promise Date": us(t - timedelta(days=20)), "Status": "Received"})
    scorecard = [{"Vendor": v, "On-Time %": f"{p}%", "Lead Time (days)": lt, "Preferred": pref}
                 for v, p, lt, pref in [("Midwest Friction Co", 64, 14, "N"), ("Lone Star Electric", 86, 10, "Y"),
                                        ("Great Lakes Filtration", 95, 7, "Y"), ("Ohio Valley Components", 91, 9, "Y")]]
    d = out / "atlas"
    _write_csv(d / "wms_stock.csv", wms)
    _write_csv(d / "erp_stock.csv", erp)
    _write_csv(d / "sales_orders.csv", sales)
    _write_csv(d / "open_pos.csv", pos)
    _write_csv(d / "vendor_scorecard.csv", scorecard)
    return {"ATLAS_DATA_DIR": str(d)}


# ---------------------------------------------------------------- Bloom Apparel (semicolons, UK dates)

def bloom(out: Path) -> dict:
    rng, t = random.Random(404), _today("Europe/London")
    uk = lambda d: d.strftime("%d/%m/%Y")  # noqa: E731
    sizes = {"XS": 0.1, "S": 0.25, "M": 0.3, "L": 0.22, "XL": 0.13}
    styles = {f"BL-DRESS-{400 + i:04d}": rng.uniform(3, 10) for i in range(1, 9)}
    styles.update({f"BL-TEE-{100 + i:04d}": rng.uniform(6, 18) for i in range(1, 7)})
    styles.update({f"BL-JEAN-{200 + i:04d}": rng.uniform(2, 8) for i in range(1, 7)})
    reasons = ["Too small", "Too large", "Changed mind", "Not as pictured", "Faulty"]
    orders, returns, stock, n, r = [], [], [], 0, 0
    plan = {**styles, "BL-DRESS-0412": 9, "BL-TEE-0107": 20}
    for style, base in plan.items():
        for d in range(DAYS):
            for size, share in sizes.items():
                q = _daily(rng, base * share, d)
                if not q:
                    continue
                n += 1
                day = t - timedelta(days=d)
                orders.append({"order_ref": f"W{n:06d}", "line": 1, "order_date": uk(day), "style_code": style, "size": size, "qty": q,
                               "price_gbp": f"£{rng.choice([29, 45, 59, 79]):.2f}", "customer_email": f"shopper{n}@example.co.uk"})
                rate = 0.42 if style == "BL-DRESS-0412" and d < 14 else rng.uniform(0.04, 0.12)
                back = sum(1 for _ in range(q) if rng.random() < rate)
                if back:
                    r += 1
                    reason = (rng.choice(["Too small", "Too small", "Too small", "Faulty"]) if style == "BL-DRESS-0412" and d < 14 and size in ("S", "M")
                              else rng.choice(reasons))
                    rday = max(day + timedelta(days=rng.randint(2, 6)), t - timedelta(days=13)) if style == "BL-DRESS-0412" and d < 14 else day + timedelta(days=rng.randint(3, 10))
                    if rday <= t:
                        returns.append({"return_ref": f"R{r:06d}", "return_date": uk(rday), "style_code": style, "size": size, "qty": back, "reason": reason})
        total = 60 if style == "BL-TEE-0107" else round(base * rng.uniform(20, 50))
        for size, share in sizes.items():
            stock.append({"style_code": style, "size": size, "available": max(1, round(total * share))})
    orders.append({**orders[5], "order_ref": "WBAD1", "qty": ""})  # blank quantity: rejected
    d = out / "bloom"
    _write_csv(d / "orders_export.csv", orders, ";")
    _write_csv(d / "returns_export.csv", returns, ";")
    _write_csv(d / "stock_levels.csv", stock, ";")
    # The ads feed folder is intentionally not created: its source fails visibly and its data is stale.
    return {"BLOOM_DATA_DIR": str(d)}


GENERATORS = {"homemonde": homemonde, "kavya": kavya, "atlas": atlas, "bloom": bloom}


def generate(out: Path) -> dict:
    """Write every company's data under `out`; return the environment variables the tenant packs reference."""
    env = {}
    for fn in GENERATORS.values():
        env.update(fn(out))
    return env
