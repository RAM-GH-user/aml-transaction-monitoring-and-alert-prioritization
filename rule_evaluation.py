"""
STEP 3 - RUN THE RULES, SCORE CUSTOMERS, MEASURE PERFORMANCE
============================================================
1. Loads the CSVs into a SQLite database (aml.db) - open it later in
   DB Browser for SQLite / DBeaver to practise your own SQL.
2. Runs each of the 10 SQL rules from 02_aml_rules.sql.
3. Stacks the results into one ALERTS table.
4. Builds a customer RISK SCORE (weighted sum of rules hit) and tier.
5. Compares alerts with the planted ground truth -> how good is each rule?
   (This is how analysts TUNE thresholds: too many false positives =
   wasted investigator time; missed cases = regulatory risk.)

Run (from the repo root):  python python/rule_evaluation.py
Outputs: alerts.csv, customer_risk_scores.csv, rule_performance.csv, aml.db
"""
import re
import sqlite3
import pandas as pd
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]      # repo root
DATA, OUT, SQL = ROOT / "data", ROOT / "outputs", ROOT / "sql"
DATA.mkdir(exist_ok=True); OUT.mkdir(exist_ok=True)


# ------------------------------------------------------------------ settings
RULE_WEIGHTS = {                  # how serious each red flag is (tune these)
    "R01_LARGE_CASH": 10,          # reporting rule - weak suspicion on its own
    "R02_STRUCTURING": 30,         # deliberate evasion of reporting - strong
    "R03_HIGH_RISK_GEO": 25,
    "R04_SUDDEN_WEALTH": 20,
    "R05_VELOCITY": 15,
    "R06_INTL_WIRES": 15,
    "R07_RAPID_MOVEMENT": 30,
    "R08_ROUND_AMOUNTS": 10,
    "R09_DORMANT_REACTIVATION": 20,
    "R10_FUNNEL_ACCOUNT": 25,
}
# which planted typology each rule is meant to catch
RULE_TARGET = {
    "R01_LARGE_CASH": "LARGE_CASH", "R02_STRUCTURING": "STRUCTURING",
    "R03_HIGH_RISK_GEO": "HIGH_RISK_GEO", "R04_SUDDEN_WEALTH": "SUDDEN_WEALTH",
    "R05_VELOCITY": "VELOCITY", "R06_INTL_WIRES": "INTL_WIRES",
    "R07_RAPID_MOVEMENT": "RAPID_MOVEMENT", "R08_ROUND_AMOUNTS": "ROUND_AMOUNTS",
    "R09_DORMANT_REACTIVATION": "DORMANT", "R10_FUNNEL_ACCOUNT": "FUNNEL",
}
# R01 also targets cash-front businesses (placement through a business)
EXTRA_TARGETS = {"R01_LARGE_CASH": ["CASH_FRONT_BUSINESS"]}


def tier(score):
    if score >= 50:
        return "HIGH"       # escalate to investigator / consider SAR
    if score >= 25:
        return "MEDIUM"     # enhanced review
    return "LOW"            # monitor


# ------------------------------------------------------------------ 1. load data
customers = pd.read_csv(DATA / "customers.csv")
transactions = pd.read_csv(DATA / "transactions.csv")
truth = pd.read_csv(DATA / "ground_truth.csv")

con = sqlite3.connect(DATA / "aml.db")
customers.to_sql("customers", con, if_exists="replace", index=False)
transactions.to_sql("transactions", con, if_exists="replace", index=False)
truth.to_sql("ground_truth", con, if_exists="replace", index=False)
# indexes make the self-joins fast
con.execute("CREATE INDEX IF NOT EXISTS ix_txn_cust_dt ON transactions(customer_id, txn_datetime)")
con.execute("CREATE INDEX IF NOT EXISTS ix_txn_type ON transactions(txn_type)")
print(f"Loaded {len(customers):,} customers and {len(transactions):,} transactions into aml.db\n")

# ------------------------------------------------------------------ 2. run rules
sql_text = open(SQL / "02_aml_rules.sql", encoding="utf-8").read()
blocks = re.split(r"^-- name:\s*(\S+)\s*$", sql_text, flags=re.M)[1:]
rules = dict(zip(blocks[0::2], blocks[1::2]))      # {rule_id: sql}

results = []
for rule_id, query in rules.items():
    df = pd.read_sql_query(query.strip().rstrip(";"), con)
    results.append(df)
    print(f"  {rule_id:<27} {len(df):>4} alerts  {df.customer_id.nunique():>4} customers")

# ------------------------------------------------------------------ 3. alerts table
alerts = pd.concat(results, ignore_index=True)
alerts["trigger_amount"] = alerts["trigger_amount"].round(2)
alerts = alerts.sort_values(["alert_date", "customer_id"]).reset_index(drop=True)
alerts.insert(0, "alert_id", [f"A{i + 1:05d}" for i in range(len(alerts))])
alerts["rule_weight"] = alerts["rule_id"].map(RULE_WEIGHTS)
alerts.to_sql("alerts", con, if_exists="replace", index=False)
alerts.to_csv(OUT / "alerts.csv", index=False)
print(f"\nTotal alerts: {len(alerts):,}")

# ------------------------------------------------------------------ 4. risk score
# Score = sum of weights of DISTINCT rules hit (hitting the same rule many
# times shouldn't outweigh hitting several different red flags).
distinct_hits = alerts.drop_duplicates(["customer_id", "rule_id"])
scores = (distinct_hits.groupby("customer_id")
          .agg(risk_score=("rule_weight", "sum"),
               rules_hit=("rule_id", lambda s: ", ".join(sorted(s))),
               distinct_rules=("rule_id", "nunique"))
          .reset_index())
alert_counts = alerts.groupby("customer_id").agg(total_alerts=("alert_id", "count"),
                                                 total_flagged_amount=("trigger_amount", "sum"))
scores = scores.merge(alert_counts, on="customer_id")
scores["risk_tier"] = scores["risk_score"].apply(tier)
scores = scores.merge(customers[["customer_id", "customer_type", "occupation",
                                 "declared_monthly_income"]], on="customer_id")
scores = scores.sort_values(["risk_score", "total_flagged_amount"], ascending=False)
scores.to_sql("customer_risk_scores", con, if_exists="replace", index=False)
scores.to_csv(OUT / "customer_risk_scores.csv", index=False)

print("\nCustomers by risk tier:")
print(scores["risk_tier"].value_counts().reindex(["HIGH", "MEDIUM", "LOW"]).fillna(0).astype(int).to_string())

# ------------------------------------------------------------------ 5. evaluate
bad = set(truth.customer_id)
perf = []
for rule_id, target in RULE_TARGET.items():
    flagged = set(alerts.loc[alerts.rule_id == rule_id, "customer_id"])
    target_list = [target] + EXTRA_TARGETS.get(rule_id, [])
    targets = set(truth.loc[truth.injected_typology.isin(target_list), "customer_id"])
    target = " + ".join(target_list)
    tp = len(flagged & bad)
    perf.append({
        "rule_id": rule_id,
        "customers_flagged": len(flagged),
        "true_positives": tp,
        "false_positives": len(flagged) - tp,
        "precision_%": round(100 * tp / len(flagged), 1) if flagged else None,
        "target_typology": target,
        "target_caught": f"{len(flagged & targets)}/{len(targets)}",
    })
perf = pd.DataFrame(perf)
perf.to_csv(OUT / "rule_performance.csv", index=False)

flagged_all = set(scores.customer_id)
high = set(scores.loc[scores.risk_tier != "LOW", "customer_id"])
print("\nRule performance vs planted bad actors:")
print(perf.to_string(index=False))
print(f"\nOverall: {len(flagged_all & bad)}/{len(bad)} bad actors flagged by at least one rule; "
      f"{len(flagged_all - bad)} legitimate customers also flagged (false positives).")
print(f"MEDIUM+HIGH tier: {len(high & bad)} bad actors, {len(high - bad)} false positives.")

print("\nTop 10 riskiest customers:")
print(scores.head(10)[["customer_id", "customer_type", "occupation", "risk_score",
                       "risk_tier", "rules_hit"]].to_string(index=False))
con.close()
print("\nSaved: alerts.csv, customer_risk_scores.csv, rule_performance.csv, aml.db")
