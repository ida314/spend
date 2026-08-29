-- Charges worth a second look: more than 3x that merchant's own trailing mean, or a
-- first-ever charge at a merchant over $100.
--
-- "Unusual" is relative to the merchant, not to the ledger: a $90 electricity bill is not
-- remarkable and a $90 coffee is. Counts nothing that is not spending.
WITH stats AS (
    SELECT merchant_key, AVG(net_spend_cents) AS mean_cents, COUNT(*) AS n
    FROM spending WHERE merchant_key IS NOT NULL GROUP BY 1
)
SELECT s.date, s.merchant, s.category, s.net_spend_cents AS cents,
       CAST(st.mean_cents AS INT)                        AS merchant_mean,
       CASE WHEN st.n = 1 THEN 'first time here' ELSE 'well above its usual' END AS why
FROM spending s JOIN stats st USING (merchant_key)
WHERE s.date >= date('now', '-180 days')
  AND ((st.n > 1 AND s.net_spend_cents > st.mean_cents * 3)
    OR (st.n = 1 AND s.net_spend_cents > 10000))
ORDER BY s.date DESC;
