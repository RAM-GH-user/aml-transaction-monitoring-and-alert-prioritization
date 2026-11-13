"""
STEP 4 - FROM A BROAD CUTOFF TO SEGMENTATION + LOGISTIC REGRESSION
==================================================================
Problem: R01 (cash >= $10,000) is a classic HIGH-VOLUME, LOW-CONVERSION rule.
Most alerts are legitimate businesses or one-off personal cash, yet every alert
costs investigator time.

Approach (the same one used in transaction-monitoring optimization work):
  1. SEGMENT the alerted population and measure conversion per segment.
  2. Build a LOGISTIC REGRESSION (logit) model on alert-level features to
     score how likely each alert is to be productive.
  3. Choose a score cutoff on the TRAINING period that keeps recall >= 95%,
     then test it on a LATER period (out-of-time validation).
  4. Report odds ratios so every driver is explainable to model validation.

Label: an alert is "productive" if the customer is a planted bad actor
(a stand-in for "led to a SAR" in a real bank).

Run (from the repo root):  python python/alert_scoring.py      (needs: pip install scikit-learn)
Outputs: logit_segments.csv, logit_odds_ratios.csv, logit_results.csv
"""
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.metrics import roc_auc_score, average_precision_score
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]      # repo root
DATA, OUT, SQL = ROOT / "data", ROOT / "outputs", ROOT / "sql"
DATA.mkdir(exist_ok=True); OUT.mkdir(exist_ok=True)


SPLIT_DATE = "2026-08-01"        # train: Jan-Jul, test: Aug-Oct (out of time)
TARGET_RECALL = 0.95

alerts = pd.read_csv(OUT / "alerts.csv", parse_dates=["alert_date"])
cust = pd.read_csv(DATA / "customers.csv", parse_dates=["onboard_date"])
tx = pd.read_csv(DATA / "transactions.csv", parse_dates=["txn_datetime"])
truth = pd.read_csv(DATA / "ground_truth.csv")

a = alerts[alerts.rule_id == "R01_LARGE_CASH"].merge(cust, on="customer_id")
a["productive"] = a.customer_id.isin(truth.customer_id).astype(int)

# ------------------------------------------------------------------ 1. features
# Behaviour in the 90 days BEFORE the alert day (no look-ahead).
tx["day"] = tx.txn_datetime.dt.normalize()
cash_in = tx[tx.txn_type == "CASH_DEPOSIT"]
inflow = tx[tx.direction == "IN"]


def lookback(row):
    end, start = row.alert_date, row.alert_date - pd.Timedelta(days=90)
    c = cash_in[(cash_in.customer_id == row.customer_id) & (cash_in.day >= start) & (cash_in.day < end)]
    i = inflow[(inflow.customer_id == row.customer_id) & (inflow.day >= start) & (inflow.day < end)]
    return pd.Series({
        "prior_large_cash_90d": int((c.amount >= 10000).sum()),
        "cash_share_90d": c.amount.sum() / i.amount.sum() if i.amount.sum() > 0 else 0.0,
    })


a = pd.concat([a, a.apply(lookback, axis=1)], axis=1)
a["is_business"] = (a.customer_type == "Business").astype(int)
a["log_amount"] = np.log10(a.trigger_amount)
a["cash_to_income"] = a.trigger_amount / a.declared_monthly_income
a["tenure_years"] = (a.alert_date - a.onboard_date).dt.days / 365.25
FEATURES = ["is_business", "log_amount", "cash_to_income", "prior_large_cash_90d",
            "cash_share_90d", "tenure_years"]

# ------------------------------------------------------------------ 2. segmentation
a["income_band"] = pd.cut(a.cash_to_income, [0, 0.5, 2, 5, np.inf],
                          labels=["<0.5x income", "0.5-2x", "2-5x", ">5x income"])
a["repeat_band"] = np.where(a.prior_large_cash_90d >= 2, "repeat large cash (2+ in 90d)",
                            "first or second large cash")
seg = (a.groupby(["customer_type", "repeat_band"], observed=True)
         .agg(alerts=("productive", "size"), productive=("productive", "sum"))
         .assign(conversion_pct=lambda d: (100 * d.productive / d.alerts).round(1))
         .reset_index())
seg.to_csv(OUT / "logit_segments.csv", index=False)
print(f"R01 large-cash alerts: {len(a)}  productive: {a.productive.sum()}  "
      f"conversion: {100 * a.productive.mean():.1f}%\n")
print("Conversion by segment:")
print(seg.to_string(index=False))

# ------------------------------------------------------------------ 3. logit model
train, test = a[a.alert_date < SPLIT_DATE], a[a.alert_date >= SPLIT_DATE]
model = make_pipeline(StandardScaler(), LogisticRegression(class_weight="balanced", max_iter=1000))
model.fit(train[FEATURES], train.productive)

train_p = model.predict_proba(train[FEATURES])[:, 1]
test_p = model.predict_proba(test[FEATURES])[:, 1]

# cutoff = highest score that still keeps >= 95% of productive TRAIN alerts
pos_scores = np.sort(train_p[train.productive == 1])
cutoff = pos_scores[int(np.floor((1 - TARGET_RECALL) * len(pos_scores)))]


def summarise(df, p, name):
    keep = p >= cutoff
    tp = int(((df.productive == 1) & keep).sum())
    return {
        "period": name,
        "alerts_before": len(df),
        "productive_alerts": int(df.productive.sum()),
        "alerts_after": int(keep.sum()),
        "productive_kept": tp,
        "recall_pct": round(100 * tp / max(df.productive.sum(), 1), 1),
        "alert_reduction_pct": round(100 * (1 - keep.sum() / len(df)), 1),
        "conversion_before_pct": round(100 * df.productive.mean(), 1),
        "conversion_after_pct": round(100 * tp / max(keep.sum(), 1), 1),
        "roc_auc": round(roc_auc_score(df.productive, p), 3),
        "pr_auc": round(average_precision_score(df.productive, p), 3),
    }


res = pd.DataFrame([summarise(train, train_p, "train (Jan-Jul)"),
                    summarise(test, test_p, "test (Aug-Oct, out of time)")])
res.to_csv(OUT / "logit_results.csv", index=False)
print(f"\nScore cutoff (set on train for {TARGET_RECALL:.0%} recall): {cutoff:.3f}")
print(res.T.to_string(header=False))

# ------------------------------------------------------------------ 4. explainability
lr = model.named_steps["logisticregression"]
odds = pd.DataFrame({
    "feature": FEATURES,
    "coefficient_std": lr.coef_[0].round(3),
    "odds_ratio_per_1sd": np.exp(lr.coef_[0]).round(2),
}).sort_values("odds_ratio_per_1sd", ascending=False)
odds.to_csv(OUT / "logit_odds_ratios.csv", index=False)
print("\nDrivers (odds ratio per 1 standard deviation increase):")
print(odds.to_string(index=False))
print("\nSaved: logit_segments.csv, logit_results.csv, logit_odds_ratios.csv")
