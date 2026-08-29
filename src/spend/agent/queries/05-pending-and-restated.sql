-- What has not settled, and what settled at a different number than it authorised.
--
-- The tip-adjustment view. A restaurant that authorises $52 and posts $61 once the tip
-- clears shows up here as amount_changed = 1, with the revision count.
--
-- Unlike the other queries this one reads `bank`, not `spending`: a pending transfer or an
-- unparseable amount is exactly the sort of thing you want to see in a "what is odd" view.
SELECT date, account, merchant, description, flow,
       amount_cents, net_spend_cents, revisions, status, review_reason,
       CASE WHEN pending THEN 'not settled yet' ELSE 'restated after posting' END AS what
FROM bank
WHERE pending = 1 OR amount_changed = 1 OR status = 'needs_review'
ORDER BY date DESC;
