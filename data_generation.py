"""
STEP 1 - GENERATE SYNTHETIC BANK DATA
=====================================
Real AML data is confidential, so we create realistic fake data:
  * customers.csv     - KYC profile (type, occupation, declared income, country)
  * transactions.csv  - 10 months of activity (Jan-Oct 2026)
  * ground_truth.csv  - which customers we secretly planted as "bad actors"
                        (in real life you never have this - investigators
                        and SAR outcomes give you the labels later)

Most customers behave normally. A small group is injected with one of the
10 money-laundering typologies our rules are designed to catch.

Run (from the repo root):  python python/data_generation.py
"""
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]      # repo root
DATA, OUT, SQL = ROOT / "data", ROOT / "outputs", ROOT / "sql"
DATA.mkdir(exist_ok=True); OUT.mkdir(exist_ok=True)


rng = np.random.default_rng(42)          # fixed seed -> same data every run
START = datetime(2026, 1, 1)
END = datetime(2026, 10, 31)
N_NORMAL = 800
N_PER_TYPOLOGY = 8

HIGH_RISK_COUNTRIES = ["IR", "KP", "MM"]          # sample list - update from FATF
NORMAL_FOREIGN = ["CA", "GB", "IN", "MX", "DE"]
OCCUPATIONS_IND = ["Teacher", "Nurse", "Engineer", "Driver", "Student",
                   "Retail Associate", "Accountant", "Retired"]
OCCUPATIONS_BUS = ["Restaurant", "Grocery Store", "Consulting", "Auto Repair",
                   "Import/Export"]

customers, txns, truth = [], [], []
txn_counter = 0


def rand_dt(start=START, end=END):
    """Random datetime between start and end."""
    seconds = int((end - start).total_seconds())
    return start + timedelta(seconds=int(rng.integers(0, seconds)))


def add_txn(cid, dt, txn_type, amount, cp_id, cp_country="US"):
    global txn_counter
    txn_counter += 1
    direction = "IN" if txn_type in ("CASH_DEPOSIT", "WIRE_IN", "ACH_IN") else "OUT"
    txns.append({
        "txn_id": f"T{txn_counter:07d}",
        "customer_id": cid,
        "txn_datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "txn_type": txn_type,
        "direction": direction,
        "amount": round(float(amount), 2),
        "counterparty_id": cp_id,
        "counterparty_country": cp_country,
    })


def messy(amount):
    """Make an amount look natural (not round) - real spending has cents."""
    return amount + rng.uniform(0.01, 0.99) + rng.integers(1, 9)


def normal_activity(cid, ctype, income, start=START, end=END):
    """Typical month-to-month behaviour for a legitimate customer."""
    month = datetime(start.year, start.month, 1)
    employer = f"EMP{rng.integers(1, 300):04d}"
    while month <= end:
        m_end = (month + timedelta(days=32)).replace(day=1) - timedelta(seconds=1)
        lo, hi = max(month, start), min(m_end, end)
        if lo > hi:
            month = (month + timedelta(days=32)).replace(day=1)
            continue
        if ctype == "Individual":
            add_txn(cid, rand_dt(lo, hi), "ACH_IN", messy(income * rng.uniform(.95, 1.05)), employer)
            for _ in range(rng.integers(10, 30)):                      # card spend
                add_txn(cid, rand_dt(lo, hi), "CARD", rng.uniform(5, 300), f"MER{rng.integers(1, 2000):05d}")
            for _ in range(rng.integers(3, 6)):                        # bills
                add_txn(cid, rand_dt(lo, hi), "ACH_OUT", messy(rng.uniform(80, 1800)), f"BILL{rng.integers(1, 50):03d}")
            if rng.random() < 0.3:
                add_txn(cid, rand_dt(lo, hi), "CASH_WITHDRAWAL", messy(rng.uniform(40, 500)), "ATM")
            if rng.random() < 0.15:
                add_txn(cid, rand_dt(lo, hi), "CASH_DEPOSIT", messy(rng.uniform(50, 2000)), "BRANCH")
            if rng.random() < 0.05:                                    # family remittance
                add_txn(cid, rand_dt(lo, hi), "WIRE_OUT", messy(rng.uniform(200, 3000)),
                        f"CP{rng.integers(1, 9999):05d}", rng.choice(NORMAL_FOREIGN))
        else:  # Business
            for _ in range(rng.integers(15, 40)):                      # customer payments
                add_txn(cid, rand_dt(lo, hi), "ACH_IN", messy(income / 25 * rng.uniform(.5, 1.5)),
                        f"CUS{rng.integers(1, 5000):05d}")
            for _ in range(rng.integers(4, 12)):                       # retail cash takings
                add_txn(cid, rand_dt(lo, hi), "CASH_DEPOSIT", messy(rng.uniform(300, 6000)), "BRANCH")
            for _ in range(rng.integers(5, 15)):                       # suppliers / payroll
                add_txn(cid, rand_dt(lo, hi), "ACH_OUT", messy(income / 12 * rng.uniform(.3, 1.2)),
                        f"SUP{rng.integers(1, 800):04d}")
            if rng.random() < 0.1:                                     # legit large cash day -> CTR
                add_txn(cid, rand_dt(lo, hi), "CASH_DEPOSIT", messy(rng.uniform(10000, 15000)), "BRANCH")
        month = (month + timedelta(days=32)).replace(day=1)


def new_customer(ctype, income=None, occupation=None, onboard=None):
    cid = f"C{len(customers) + 1:05d}"
    if income is None:
        income = rng.uniform(3000, 15000) if ctype == "Individual" else rng.uniform(20000, 200000)
    customers.append({
        "customer_id": cid,
        "customer_type": ctype,
        "occupation": occupation or rng.choice(OCCUPATIONS_IND if ctype == "Individual" else OCCUPATIONS_BUS),
        "declared_monthly_income": round(income, 2),
        "country": "US",
        "onboard_date": (onboard or START - timedelta(days=int(rng.integers(30, 3000)))).strftime("%Y-%m-%d"),
    })
    return cid, income


# ---------------------------------------------------------------- normal customers
for _ in range(N_NORMAL):
    ctype = "Individual" if rng.random() < 0.8 else "Business"
    cid, inc = new_customer(ctype)
    normal_activity(cid, ctype, inc)

# ---------------------------------------------------------------- bad actors
def inject(typology, fn, ctype="Individual", **kw):
    for _ in range(N_PER_TYPOLOGY):
        cid, inc = new_customer(ctype, **kw)
        fn(cid, inc)
        truth.append({"customer_id": cid, "injected_typology": typology})


def t_large_cash(cid, inc):                         # 1. Cash >= $10K
    normal_activity(cid, "Individual", inc)
    for _ in range(rng.integers(1, 3)):
        add_txn(cid, rand_dt(), "CASH_DEPOSIT", messy(rng.uniform(10000, 45000)), "BRANCH")


def t_structuring(cid, inc):                        # 2. Just-below-$10K deposits
    normal_activity(cid, "Individual", inc)
    base = rand_dt(START, END - timedelta(days=10))
    for i in range(rng.integers(3, 7)):
        add_txn(cid, base + timedelta(days=int(rng.integers(0, 6)), hours=int(rng.integers(0, 23))),
                "CASH_DEPOSIT", rng.uniform(8500, 9950), f"BRANCH{rng.integers(1, 20):02d}")


def t_high_risk_geo(cid, inc):                      # 3. High-risk countries
    normal_activity(cid, "Individual", inc)
    for _ in range(rng.integers(2, 5)):
        add_txn(cid, rand_dt(), rng.choice(["WIRE_IN", "WIRE_OUT"]), messy(rng.uniform(3000, 40000)),
                f"CP{rng.integers(1, 9999):05d}", rng.choice(HIGH_RISK_COUNTRIES))


def t_sudden_wealth(cid, inc):                      # 4. Inflows far above income
    normal_activity(cid, "Individual", inc)
    month = rand_dt(datetime(2026, 5, 1), datetime(2026, 9, 30))
    for _ in range(rng.integers(3, 6)):
        add_txn(cid, month + timedelta(days=int(rng.integers(0, 25))), "WIRE_IN",
                messy(rng.uniform(15000, 60000)), f"CP{rng.integers(1, 9999):05d}")


def t_velocity(cid, inc):                           # 5. Burst of transactions
    normal_activity(cid, "Individual", inc)
    day = rand_dt(START, END - timedelta(days=1)).replace(hour=0, minute=0, second=0)
    for _ in range(rng.integers(20, 40)):
        add_txn(cid, day + timedelta(minutes=int(rng.integers(0, 1439))),
                rng.choice(["ACH_OUT", "ACH_IN", "CARD"]), messy(rng.uniform(100, 2500)),
                f"CP{rng.integers(1, 9999):05d}")


def t_intl_wires(cid, inc):                         # 6. Repeated large intl wires
    normal_activity(cid, "Individual", inc)
    base = rand_dt(START, END - timedelta(days=30))
    for _ in range(rng.integers(3, 6)):
        add_txn(cid, base + timedelta(days=int(rng.integers(0, 25))), "WIRE_OUT",
                messy(rng.uniform(10000, 50000)), f"CP{rng.integers(1, 9999):05d}",
                rng.choice(["AE", "HK", "PA", "CY", "TR"]))


def t_rapid_movement(cid, inc):                     # 7. In-and-out (pass-through)
    normal_activity(cid, "Individual", inc)
    for _ in range(rng.integers(1, 3)):
        t_in = rand_dt(START, END - timedelta(days=3))
        amt = rng.uniform(25000, 100000)
        add_txn(cid, t_in, "WIRE_IN", messy(amt), f"CP{rng.integers(1, 9999):05d}")
        add_txn(cid, t_in + timedelta(hours=int(rng.integers(4, 48))), "WIRE_OUT",
                amt * rng.uniform(0.90, 0.98), f"CP{rng.integers(1, 9999):05d}")


def t_round_amounts(cid, inc):                      # 8. Round-dollar amounts
    normal_activity(cid, "Individual", inc)
    base = rand_dt(START, END - timedelta(days=30))
    for _ in range(rng.integers(5, 9)):
        add_txn(cid, base + timedelta(days=int(rng.integers(0, 25))),
                rng.choice(["WIRE_OUT", "ACH_OUT", "CASH_DEPOSIT"]),
                float(rng.choice([1000, 2000, 3000, 5000, 7000])), f"CP{rng.integers(1, 9999):05d}")


def t_dormant(cid, inc):                            # 9. Dormant then suddenly active
    normal_activity(cid, "Individual", inc, START, datetime(2026, 1, 31))
    wake = rand_dt(datetime(2026, 8, 15), datetime(2026, 10, 20))
    for _ in range(rng.integers(2, 5)):
        add_txn(cid, wake + timedelta(days=int(rng.integers(0, 8))),
                rng.choice(["WIRE_IN", "WIRE_OUT"]), messy(rng.uniform(5000, 30000)),
                f"CP{rng.integers(1, 9999):05d}")


def t_funnel(cid, inc):                             # 10. Many senders -> one account (mule)
    normal_activity(cid, "Individual", inc)
    base = rand_dt(START, END - timedelta(days=30))
    total = 0
    for _ in range(rng.integers(12, 25)):
        amt = messy(rng.uniform(500, 3000)); total += amt
        add_txn(cid, base + timedelta(days=int(rng.integers(0, 20))),
                rng.choice(["ACH_IN", "CASH_DEPOSIT"]), amt, f"SND{rng.integers(1, 99999):05d}")
    add_txn(cid, base + timedelta(days=22), "WIRE_OUT", total * 0.95,
            f"CP{rng.integers(1, 9999):05d}", rng.choice(NORMAL_FOREIGN))


inject("LARGE_CASH", t_large_cash)
inject("STRUCTURING", t_structuring)
inject("HIGH_RISK_GEO", t_high_risk_geo)
inject("SUDDEN_WEALTH", t_sudden_wealth, income=2500, occupation="Student")
inject("VELOCITY", t_velocity)
inject("INTL_WIRES", t_intl_wires)
inject("RAPID_MOVEMENT", t_rapid_movement)
inject("ROUND_AMOUNTS", t_round_amounts)
inject("DORMANT", t_dormant)
inject("FUNNEL", t_funnel)

# ---------------------------------------------------------------- realism for the cash rule
# Real large-cash alerts are mostly legitimate (low conversion), and the
# suspicious ones are not trivially separable. Added with a separate random
# stream so every customer above stays exactly the same.
rng = np.random.default_rng(7)

def t_legit_one_off_cash(cid, inc):                 # car sale, family gift, cash savings
    normal_activity(cid, "Individual", inc)
    add_txn(cid, rand_dt(), "CASH_DEPOSIT", messy(rng.uniform(10000, 24000)), "BRANCH")

for _ in range(60):                                 # legitimate, NOT added to ground truth
    cid, inc = new_customer("Individual")
    t_legit_one_off_cash(cid, inc)

def t_cash_front_business(cid, inc):                # business used to place illicit cash
    normal_activity(cid, "Business", inc)
    for _ in range(rng.integers(6, 14)):
        add_txn(cid, rand_dt(), "CASH_DEPOSIT", messy(rng.uniform(10000, 30000)), "BRANCH")

inject("CASH_FRONT_BUSINESS", t_cash_front_business, ctype="Business")

# ---------------------------------------------------------------- save
pd.DataFrame(customers).to_csv(DATA / "customers.csv", index=False)
tx = pd.DataFrame(txns).sort_values("txn_datetime")
tx.to_csv(DATA / "transactions.csv", index=False)
pd.DataFrame(truth).to_csv(DATA / "ground_truth.csv", index=False)

print(f"Customers:    {len(customers):,}  ({len(truth)} planted bad actors)")
print(f"Transactions: {len(tx):,}")
print("Files written: customers.csv, transactions.csv, ground_truth.csv")
