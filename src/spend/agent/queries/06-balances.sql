-- What each account holds, and what it covers.
--
-- `covers_from` / `covers_to` are load-bearing: an account whose coverage stops in July has
-- not gone quiet, it has stopped being imported. Check this before comparing accounts.
--
-- A NULL balance means the source never reported one -- the file-drop adapter reads a
-- statement export, which carries transactions and not a live balance.
SELECT account, name, kind, institution,
       balance_cents, balance_at,
       covers_from, covers_to, last_seen_at,
       CASE WHEN unmapped THEN 'NOT IN accounts.toml' ELSE '' END AS warning
FROM accounts
ORDER BY unmapped DESC, account;
