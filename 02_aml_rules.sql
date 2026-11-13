/* =====================================================================
   STEP 2 - THE 10 AML TRANSACTION-MONITORING RULES (SQL)
   =====================================================================
   Tables:  customers(customer_id, customer_type, occupation,
                      declared_monthly_income, country, onboard_date)
            transactions(txn_id, customer_id, txn_datetime, txn_type,
                         direction, amount, counterparty_id,
                         counterparty_country)

   Every rule returns the SAME columns so they can be stacked into one
   alerts table:
       customer_id | rule_id | alert_date | trigger_amount | txn_count | details

   Written for SQLite (runs locally, no install). To move to SQL Server /
   Snowflake / Oracle, mainly change the date functions:
       julianday(a) - julianday(b)   ->  DATEDIFF(day, b, a)
       date(x) / strftime('%Y-%m',x) ->  CAST(x AS DATE) / FORMAT / DATE_TRUNC
   Each rule is separated by a line "-- name: <RULE_ID>" so Python can
   read and run them one by one.
   ===================================================================== */


-- name: R01_LARGE_CASH
/* RULE 1 - Cash transaction at or above $10,000
   Why: US Bank Secrecy Act requires a Currency Transaction Report (CTR)
   for cash over $10K. Large cash is also the "placement" stage of laundering.
   Note: many hits are legitimate businesses - this is a reporting rule,
   not proof of crime. */
SELECT  customer_id,
        'R01_LARGE_CASH'                      AS rule_id,
        date(txn_datetime)                    AS alert_date,
        SUM(amount)                           AS trigger_amount,
        COUNT(*)                              AS txn_count,
        'Cash >= $10,000 on ' || date(txn_datetime) AS details
FROM    transactions
WHERE   txn_type IN ('CASH_DEPOSIT', 'CASH_WITHDRAWAL')
  AND   amount >= 10000
GROUP BY customer_id, date(txn_datetime);


-- name: R02_STRUCTURING
/* RULE 2 - Structuring (smurfing): several cash deposits just under $10K
   Logic: 3+ cash deposits between $8,000 and $9,999.99 within 7 days,
   adding up to $10,000 or more.
   Technique: SELF-JOIN - for each deposit (a), look at the same
   customer's deposits (b) in the 7 days that follow. */
SELECT  a.customer_id,
        'R02_STRUCTURING'                     AS rule_id,
        MIN(date(a.txn_datetime))             AS alert_date,
        MAX(window_total)                     AS trigger_amount,
        MAX(window_count)                     AS txn_count,
        'Cash deposits $8K-$9,999 within 7 days' AS details
FROM (
    SELECT  a.customer_id, a.txn_datetime,
            COUNT(b.txn_id)  AS window_count,
            SUM(b.amount)    AS window_total
    FROM    transactions a
    JOIN    transactions b
      ON    b.customer_id = a.customer_id
     AND    b.txn_type    = 'CASH_DEPOSIT'
     AND    b.amount BETWEEN 8000 AND 9999.99
     AND    julianday(b.txn_datetime) - julianday(a.txn_datetime) BETWEEN 0 AND 7
    WHERE   a.txn_type = 'CASH_DEPOSIT'
      AND   a.amount BETWEEN 8000 AND 9999.99
    GROUP BY a.customer_id, a.txn_id
) a
WHERE   window_count >= 3
  AND   window_total >= 10000
GROUP BY a.customer_id, strftime('%Y-%m', a.txn_datetime);


-- name: R03_HIGH_RISK_GEO
/* RULE 3 - Geographic risk: wires to/from high-risk jurisdictions
   Logic: any wire >= $1,000 with a counterparty in a high-risk country.
   In production the list lives in a reference table maintained from
   FATF / OFAC lists - here it is a sample list (IR, KP, MM). */
SELECT  customer_id,
        'R03_HIGH_RISK_GEO'                   AS rule_id,
        MIN(date(txn_datetime))               AS alert_date,
        SUM(amount)                           AS trigger_amount,
        COUNT(*)                              AS txn_count,
        'Wires with high-risk countries: ' || GROUP_CONCAT(DISTINCT counterparty_country) AS details
FROM    transactions
WHERE   txn_type IN ('WIRE_IN', 'WIRE_OUT')
  AND   counterparty_country IN ('IR', 'KP', 'MM')
  AND   amount >= 1000
GROUP BY customer_id, strftime('%Y-%m', txn_datetime);


-- name: R04_SUDDEN_WEALTH
/* RULE 4 - Sudden wealth / profile mismatch
   Logic: an individual's money IN for a month is more than 5x the
   income they declared at onboarding (KYC) AND at least $25,000.
   Why: activity that doesn't fit the customer's profile is a core
   red flag (e.g. a student receiving $150K). */
SELECT  t.customer_id,
        'R04_SUDDEN_WEALTH'                   AS rule_id,
        MIN(date(t.txn_datetime))             AS alert_date,
        SUM(t.amount)                         AS trigger_amount,
        COUNT(*)                              AS txn_count,
        'Monthly inflow ' || CAST(ROUND(SUM(t.amount) / c.declared_monthly_income, 1) AS TEXT)
            || 'x declared income (' || c.occupation || ')' AS details
FROM    transactions t
JOIN    customers c ON c.customer_id = t.customer_id
WHERE   t.direction = 'IN'
  AND   c.customer_type = 'Individual'
GROUP BY t.customer_id, strftime('%Y-%m', t.txn_datetime)
HAVING  SUM(t.amount) > 5 * c.declared_monthly_income
   AND  SUM(t.amount) >= 25000;


-- name: R05_VELOCITY
/* RULE 5 - Velocity: unusually many transactions in one day
   Logic: 15 or more transactions on a single calendar day.
   Why: bursts of activity suggest automated or mule behaviour, or
   attempts to move money before the account is frozen. */
SELECT  customer_id,
        'R05_VELOCITY'                        AS rule_id,
        date(txn_datetime)                    AS alert_date,
        SUM(amount)                           AS trigger_amount,
        COUNT(*)                              AS txn_count,
        CAST(COUNT(*) AS TEXT) || ' transactions in one day' AS details
FROM    transactions
GROUP BY customer_id, date(txn_datetime)
HAVING  COUNT(*) >= 15;


-- name: R06_INTL_WIRES
/* RULE 6 - Repeated large international wires out
   Logic: 3+ outbound international wires within a rolling 30 days,
   totalling $25,000 or more.
   Technique: rolling-window SELF-JOIN (30 days back from each wire). */
SELECT  customer_id,
        'R06_INTL_WIRES'                      AS rule_id,
        MIN(date(txn_datetime))               AS alert_date,
        MAX(window_total)                     AS trigger_amount,
        MAX(window_count)                     AS txn_count,
        'International wires out in 30 days'  AS details
FROM (
    SELECT  a.customer_id, a.txn_datetime,
            COUNT(b.txn_id) AS window_count,
            SUM(b.amount)   AS window_total
    FROM    transactions a
    JOIN    transactions b
      ON    b.customer_id = a.customer_id
     AND    b.txn_type = 'WIRE_OUT'
     AND    b.counterparty_country <> 'US'
     AND    julianday(a.txn_datetime) - julianday(b.txn_datetime) BETWEEN 0 AND 30
    WHERE   a.txn_type = 'WIRE_OUT'
      AND   a.counterparty_country <> 'US'
    GROUP BY a.customer_id, a.txn_id
)
WHERE   window_count >= 3
  AND   window_total >= 25000
GROUP BY customer_id, strftime('%Y-%m', txn_datetime);


-- name: R07_RAPID_MOVEMENT
/* RULE 7 - Rapid movement of funds (in-and-out / pass-through)
   Logic: money IN of $10,000+ followed within 3 days by money OUT
   totalling at least 80% of that amount.
   Why: the account is just a "pipe" - the layering stage of laundering.
   TUNING NOTE: version 1 had no customer filter and flagged 47 businesses
   (precision 20%) - businesses receive payments and pay suppliers fast as
   normal cash flow. Banks SEGMENT rules by customer type, so this version
   applies to individuals only. A separate business rule would use much
   higher thresholds. */
SELECT  i.customer_id,
        'R07_RAPID_MOVEMENT'                  AS rule_id,
        date(i.txn_datetime)                  AS alert_date,
        i.amount                              AS trigger_amount,
        COUNT(o.txn_id) + 1                   AS txn_count,
        CAST(ROUND(100.0 * SUM(o.amount) / i.amount, 0) AS TEXT)
            || '% of $' || CAST(ROUND(i.amount, 0) AS TEXT) || ' moved out within 3 days' AS details
FROM    transactions i
JOIN    customers c ON c.customer_id = i.customer_id AND c.customer_type = 'Individual'
JOIN    transactions o
  ON    o.customer_id = i.customer_id
 AND    o.direction   = 'OUT'
 AND    julianday(o.txn_datetime) - julianday(i.txn_datetime) BETWEEN 0 AND 3
WHERE   i.direction = 'IN'
  AND   i.amount >= 10000
GROUP BY i.txn_id
HAVING  SUM(o.amount) >= 0.8 * i.amount;


-- name: R08_ROUND_AMOUNTS
/* RULE 8 - Round-dollar amounts
   Logic: 5+ transactions of $1,000 or more that are exact multiples of
   $1,000 in the same month.
   Why: genuine payments (invoices, bills, shopping) rarely land on
   exact round numbers again and again. */
SELECT  customer_id,
        'R08_ROUND_AMOUNTS'                   AS rule_id,
        MIN(date(txn_datetime))               AS alert_date,
        SUM(amount)                           AS trigger_amount,
        COUNT(*)                              AS txn_count,
        CAST(COUNT(*) AS TEXT) || ' round-dollar transactions in month' AS details
FROM    transactions
WHERE   amount >= 1000
  AND   amount = CAST(amount AS INTEGER)          -- no cents
  AND   CAST(amount AS INTEGER) % 1000 = 0        -- exact multiple of 1,000
GROUP BY customer_id, strftime('%Y-%m', txn_datetime)
HAVING  COUNT(*) >= 5;


-- name: R09_DORMANT_REACTIVATION
/* RULE 9 - Dormant account suddenly reactivated
   Logic: a transaction of $5,000+ after 180+ days with no activity.
   Technique: WINDOW FUNCTION LAG() gets the previous transaction date
   for the same customer, so we can measure the gap. */
SELECT  customer_id,
        'R09_DORMANT_REACTIVATION'            AS rule_id,
        date(txn_datetime)                    AS alert_date,
        amount                                AS trigger_amount,
        1                                     AS txn_count,
        'Active again after ' || CAST(CAST(gap_days AS INTEGER) AS TEXT) || ' dormant days' AS details
FROM (
    SELECT  t.*,
            julianday(txn_datetime)
              - julianday(LAG(txn_datetime) OVER (PARTITION BY customer_id ORDER BY txn_datetime)) AS gap_days
    FROM    transactions t
)
WHERE   gap_days >= 180
  AND   amount >= 5000;


-- name: R10_FUNNEL_ACCOUNT
/* RULE 10 - Funnel / money-mule account (many-to-one)
   Logic: an INDIVIDUAL receives money from 10+ different senders within
   a rolling 30 days. (Businesses are excluded - they naturally have
   many paying customers.) */
SELECT  customer_id,
        'R10_FUNNEL_ACCOUNT'                  AS rule_id,
        MIN(date(txn_datetime))               AS alert_date,
        MAX(window_total)                     AS trigger_amount,
        MAX(senders)                          AS txn_count,
        'Received from ' || CAST(MAX(senders) AS TEXT) || ' different senders in 30 days' AS details
FROM (
    SELECT  a.customer_id, a.txn_datetime,
            COUNT(DISTINCT b.counterparty_id) AS senders,
            SUM(b.amount)                     AS window_total
    FROM    transactions a
    JOIN    customers c   ON c.customer_id = a.customer_id AND c.customer_type = 'Individual'
    JOIN    transactions b
      ON    b.customer_id = a.customer_id
     AND    b.direction   = 'IN'
     AND    julianday(a.txn_datetime) - julianday(b.txn_datetime) BETWEEN 0 AND 30
    WHERE   a.direction = 'IN'
    GROUP BY a.customer_id, a.txn_id
)
WHERE   senders >= 10
GROUP BY customer_id, strftime('%Y-%m', txn_datetime);
