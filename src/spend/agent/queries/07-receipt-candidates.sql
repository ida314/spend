-- Receipts matched to statement lines -- CANDIDATES ONLY.
--
-- NOTHING IN THIS WORKSPACE IS DEDUPLICATED. This query proposes pairs; it does not assert
-- them, and no total anywhere is affected by what it returns. A receipt and a statement row
-- in the same pair are the same purchase seen twice, so never add their amounts together.
--
-- Matched within +/- 4 days, +/- 2 cents, on the same normalised merchant. The last two
-- sections are the more useful half: receipts with no candidate, and sizeable charges with
-- no receipt.
SELECT 'candidate pair' AS kind, r.date AS receipt_date, b.date AS bank_date,
       r.merchant, r.total_cents, b.net_spend_cents, r.id AS receipt, b.id AS bank
FROM receipts r JOIN spending b
  ON r.merchant_key = b.merchant_key
 AND ABS(julianday(r.date) - julianday(b.date)) <= 4
 AND ABS(r.total_cents - b.net_spend_cents) <= 2

UNION ALL

SELECT 'receipt with no candidate', r.date, NULL, r.merchant, r.total_cents, NULL, r.id, NULL
FROM receipts r
WHERE r.total_cents IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM spending b
    WHERE b.merchant_key = r.merchant_key
      AND ABS(julianday(r.date) - julianday(b.date)) <= 4
      AND ABS(r.total_cents - b.net_spend_cents) <= 2)

UNION ALL

SELECT 'charge over $75 with no receipt', NULL, b.date, b.merchant, NULL, b.net_spend_cents,
       NULL, b.id
FROM spending b
WHERE b.net_spend_cents > 7500 AND NOT EXISTS (
    SELECT 1 FROM receipts r
    WHERE r.merchant_key = b.merchant_key
      AND ABS(julianday(r.date) - julianday(b.date)) <= 4
      AND ABS(r.total_cents - b.net_spend_cents) <= 2)

ORDER BY 1, 2, 3;
