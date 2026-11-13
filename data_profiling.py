"""
DATA PROFILING - know the data before writing a single rule
===========================================================
The first step of any transaction-monitoring engagement is profiling the
historical data: volumes, mix, trends, data quality, and (once rules run)
alert volume and conversion per rule.

Run (from the repo root):  python python/data_profiling.py
  - before rule_evaluation.py -> data profile only
  - after rule_evaluation.py  -> also profiles alert volume and conversion
Outputs: outputs/profile_*.csv
"""
import pandas as pd
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]      # repo root
DATA, OUT = ROOT / "data", ROOT / "outputs"
OUT.mkdir(exist_ok=True)

cust = pd.read_csv(DATA / "customers.csv")
tx = pd.read_csv(DATA / "transactions.csv", parse_dates=["txn_datetime"])


def section(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# ------------------------------------------------------------------ 1. data quality
section("1. DATA QUALITY")
dq = pd.DataFrame({
    "table": ["customers", "transactions"],
    "rows": [len(cust), len(tx)],
    "duplicate_ids": [cust.customer_id.duplicated().sum(), tx.txn_id.duplicated().sum()],
    "null_cells": [int(cust.isna().sum().sum()), int(tx.isna().sum().sum())],
    "orphan_txns_no_customer": [None, int((~tx.customer_id.isin(cust.customer_id)).sum())],
    "non_positive_amounts": [None, int((tx.amount <= 0).sum())],
})
print(dq.to_string(index=False))
print(f"Date range: {tx.txn_datetime.min():%Y-%m-%d} to {tx.txn_datetime.max():%Y-%m-%d}")
dq.to_csv(OUT / "profile_data_quality.csv", index=False)

# ------------------------------------------------------------------ 2. customer mix
section("2. CUSTOMER MIX")
mix = (cust.groupby("customer_type")
           .agg(customers=("customer_id", "count"),
                median_monthly_income=("declared_monthly_income", "median"))
           .round(0))
print(mix.to_string())

# ------------------------------------------------------------------ 3. transaction mix
section("3. TRANSACTION MIX BY TYPE")
by_type = (tx.groupby("txn_type")
             .agg(txns=("txn_id", "count"), total_usd=("amount", "sum"),
                  median_usd=("amount", "median"), p99_usd=("amount", lambda s: s.quantile(.99)))
             .round(0).sort_values("total_usd", ascending=False))
print(by_type.to_string())
by_type.to_csv(OUT / "profile_txn_types.csv")

# ------------------------------------------------------------------ 4. monthly trend
section("4. MONTHLY VOLUME")
monthly = (tx.assign(month=tx.txn_datetime.dt.to_period("M").astype(str))
             .groupby("month").agg(txns=("txn_id", "count"), total_usd=("amount", "sum")).round(0))
print(monthly.to_string())
monthly.to_csv(OUT / "profile_monthly.csv")

# ------------------------------------------------------------------ 5. threshold views
section("5. ACTIVITY NEAR KEY THRESHOLDS")
cash = tx[tx.txn_type == "CASH_DEPOSIT"]
bands = pd.cut(cash.amount, [0, 3000, 8000, 10000, 20000, float("inf")],
               labels=["<$3K", "$3K-8K", "$8K-10K (structuring zone)", "$10K-20K (CTR)", ">$20K"])
print("Cash deposits by amount band:")
print(bands.value_counts().sort_index().to_string())
intl = tx[(tx.txn_type == "WIRE_OUT") & (tx.counterparty_country != "US")]
print(f"\nOutbound international wires: {len(intl):,}  to {intl.counterparty_country.nunique()} countries")

# ------------------------------------------------------------------ 6. alert profile
alerts_path = OUT / "alerts.csv"
if alerts_path.exists():
    section("6. ALERT VOLUME AND CONVERSION PER RULE")
    alerts = pd.read_csv(alerts_path)
    truth = pd.read_csv(DATA / "ground_truth.csv")
    alerts["productive"] = alerts.customer_id.isin(truth.customer_id)
    conv = (alerts.groupby("rule_id")
                  .agg(alerts=("alert_id", "count"),
                       customers=("customer_id", "nunique"),
                       productive_alerts=("productive", "sum"))
                  .assign(conversion_pct=lambda d: (100 * d.productive_alerts / d.alerts).round(1))
                  .sort_values("alerts", ascending=False))
    print(conv.to_string())
    print("\nHigh-volume, low-conversion rules are the first candidates for segmentation.")
    conv.to_csv(OUT / "profile_alert_conversion.csv")
else:
    print("\n(Run rule_evaluation.py, then re-run this script to profile alerts.)")

print("\nSaved: outputs/profile_*.csv")
