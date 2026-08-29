-- Where the money went over the last 90 days.
--
-- Grouped on `merchant_key`, not on `description`: the raw bank description carries a store
-- number and a city, so grouping on it would make every branch look like a different shop.
SELECT merchant,
       COUNT(*)                          AS n,
       SUM(net_spend_cents)              AS cents,
       CAST(AVG(net_spend_cents) AS INT) AS mean_cents,
       MIN(date)                         AS first_seen,
       MAX(date)                         AS last_seen
FROM spending
WHERE date >= date('now', '-90 days')
GROUP BY merchant_key, merchant
ORDER BY cents DESC
LIMIT 30;
