-- Spending per month per category, in cents. The default opening move.
--
-- Counts nothing that is not spending: `spending` has already dropped transfers, card
-- payments, income, tombstones and rows whose amount would not parse. Reads only the bank
-- stream, so the receipt rows -- which describe the same purchases -- cannot double it.
SELECT substr(date, 1, 7)                    AS month,
       COALESCE(category, 'uncategorised')   AS category,
       COUNT(*)                              AS n,
       SUM(net_spend_cents)                  AS cents
FROM spending
GROUP BY 1, 2
ORDER BY month DESC, cents DESC;
