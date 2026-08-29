-- Subscriptions and bills: charged in at least three of the last four full months, each
-- time within 10% of the typical amount.
--
-- Counts nothing that is not spending, and that matters more here than anywhere else:
-- autopay is the most regular thing in any ledger and it is not a subscription. `spending`
-- has already dropped it.
--
-- The current month is excluded, because a partial month makes a monthly charge look lapsed.
-- Annual renewals will not appear here; widen the window to find those.
WITH months AS (
    SELECT DISTINCT substr(date, 1, 7) AS m FROM spending
    WHERE substr(date, 1, 7) < strftime('%Y-%m', 'now', 'localtime')
    ORDER BY m DESC LIMIT 4
),
hits AS (
    SELECT merchant_key, merchant, substr(date, 1, 7) AS m,
           SUM(net_spend_cents) AS cents
    FROM spending
    WHERE substr(date, 1, 7) IN (SELECT m FROM months) AND merchant_key IS NOT NULL
    GROUP BY 1, 2, 3
)
SELECT merchant,
       COUNT(*)                AS months_charged,
       MIN(cents)              AS low_cents,
       MAX(cents)              AS high_cents,
       CAST(AVG(cents) AS INT) AS typical_cents
FROM hits
GROUP BY merchant_key, merchant
HAVING months_charged >= 3
   AND (MAX(cents) - MIN(cents)) <= (AVG(cents) * 0.10)
ORDER BY typical_cents DESC;
