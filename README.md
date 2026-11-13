# SAS programs

Add SAS programs here as you implement them.

Planned translations of the Python/SQL pipeline:

| Planned file | Mirrors | SAS technique |
|---|---|---|
| `01_import_data.sas` | `data/*.csv` | `PROC IMPORT` into a `WORK` or permanent library |
| `02_aml_rules.sas` | `sql/02_aml_rules.sql` | `PROC SQL` (the rules are standard SQL; swap `julianday` for SAS date arithmetic) |
| `03_dormant_rule.sas` | Rule R09 | DATA step with `BY customer_id`, `RETAIN` and `LAG` |
| `04_alert_scoring.sas` | `python/alert_scoring.py` | `PROC LOGISTIC` with an out-of-time split |
| `05_reconcile.sas` | `outputs/alerts.csv` | Compare SAS alerts with the Python/SQL alerts (`PROC COMPARE`) |

SAS OnDemand for Academics is a free way to run these without a licence.
