-- This month against the trailing three-month mean, per category.
--
-- The current month is EXCLUDED unless it is over. A partial month compared against whole
-- ones reads as a collapse in spending, every single time, and it is not one.
--
-- Counts nothing that is not spending; see 00.
WITH months AS (
    SELECT DISTINCT substr(date, 1, 7) AS m FROM spending
    WHERE substr(date, 1, 7) < strftime('%Y-%m', 'now', 'localtime')
),
recent AS (SELECT m FROM months ORDER BY m DESC LIMIT 4),
latest AS (SELECT MAX(m) AS m FROM recent),
per AS (
    SELECT substr(date, 1, 7) AS m,
           COALESCE(category, 'uncategorised') AS category,
           SUM(net_spend_cents) AS cents
    FROM spending WHERE substr(date, 1, 7) IN (SELECT m FROM recent)
    GROUP BY 1, 2
)
SELECT p.category,
       MAX(CASE WHEN p.m = (SELECT m FROM latest) THEN p.cents END)          AS this_month,
       CAST(AVG(CASE WHEN p.m <> (SELECT m FROM latest) THEN p.cents END) AS INT)
                                                                            AS prior_mean,
       MAX(CASE WHEN p.m = (SELECT m FROM latest) THEN p.cents END)
         - CAST(AVG(CASE WHEN p.m <> (SELECT m FROM latest) THEN p.cents END) AS INT)
                                                                            AS delta
FROM per p
GROUP BY p.category
ORDER BY delta DESC;
