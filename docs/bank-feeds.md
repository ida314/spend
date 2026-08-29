# Bank feeds

A second way in, beside the camera. Photographs cover what you chose to photograph;
statements cover the subscriptions, the autopay, and everything you forgot.

Two adapters behind one Protocol, both with the same total contract `extract/base.py` has:
**`poll` returns a `Poll` describing what happened, and never raises.** A bridge that is down,
rate-limited, mid-reauth or returning nonsense costs you the transactions you did not have
yet. It must never be able to produce wrong ones.

This is kept **separate from the receipt path**: separate tables, separate CLI verbs, separate
files in the agent workspace. Matching a photographed receipt to its statement line is a
later, harder problem, and guessing at the rule before there is real data to test it against
would bake in the wrong one. What is done now is choosing the fields that make that join
cheap: a local date, an amount in integer cents, and a normalised merchant on both sides,
indexed.

## Setup

```bash
spend feeds connect <setup-token>     # once, from bridge.simplefin.org
```

The token is single-use. It base64-decodes to a claim URL; `spend` POSTs an empty body to it
and stores the Access URL that comes back at `~/.config/spend/simplefin.access`, mode 0600.
Then it does one balances-only look and prints a ready-to-paste `accounts.toml` stanza for
everything it can see, so the only manual step in the whole feature is a copy-paste.

```bash
spend feeds sync                      # the nightly timer runs this
spend feeds import                    # CSV/OFX from ~/.local/share/spend/drop/inbox
spend feeds accounts                  # what it has seen, and how it is mapped
spend feeds log                       # the poll history
```

## Apple Card, and why the drop folder exists

The Bridge backfills about a month at connect and Wallet exports one month at a time. So the
file-drop adapter is not a nicety, it is how Apple Card gets more than four weeks of history —
and it is the disaster-recovery path when any connection breaks.

**Take the OFX, not the CSV.** Apple Card offers both. OFX carries `<FITID>`, a genuinely
stable per-transaction id, and that removes a whole class of duplicate. Without it, ids have
to be synthesised from `(account, date, amount, description, ordinal-within-the-group)`, which
is stable against re-importing and against a reordered export — but **not** against the bank
restating a description between two exports. That is what `duplicate_suspect` exists to
surface, and it is a flag, never a merge and never a delete.

## The sign, which every format writes differently and none of them mentions

> `amount_cents` is signed from the account holder's point of view: money that left is
> negative, money that arrived is positive. A coffee is `-475` on checking and `-475` on a
> credit card.

| format | how it writes it |
|---|---|
| SimpleFIN | already this way |
| `capitalone-credit` | two columns, `Debit` and `Credit`, **both positive** |
| `capitalone-checking` | one positive column plus a `Transaction Type` of Debit/Credit |
| `applecard` | one column, and a purchase is **positive** |
| `ofx` / `qfx` | `<TRNAMT>`, already statement-signed |

A sign error here is silent: nothing crashes, the totals are just wrong. So it is asserted per
format in `tests/test_feeds.py` rather than trusted.

An amount that will not parse becomes `NULL` with a `reject_reason`, never zero. "We could not
read it" and "it was free" are different facts, and a zero enters the totals
indistinguishably from a genuinely free item. The row shows up as `needs_review` and nets to
nothing.

## The double-count, and the shape that prevents it

When $1,204.11 moves from checking to the Quicksilver card, the ledger holds **both legs**:
`-120411 CAPITAL ONE AUTOPAY PYMT` on checking, `+120411 PAYMENT THANK YOU` on the card.
Neither is spending — the spending was the individual charges on the card, already in the
ledger.

So `feed_transactions` carries four amount columns, not one:

```
amount_cents     signed, bank convention. The fact. NULL when unparseable.
outflow_cents    max(0, -amount).  Never negative.
inflow_cents     max(0,  amount).  Never negative.
net_spend_cents  THE COLUMN YOU SUM.
```

`net_spend_cents` is positive for a purchase, negative for a refund, and **exactly zero** for
transfers, card payments, income, unparseable rows and tombstones. It is safe to sum over any
subset with no `WHERE` clause at all — which is the point, because the `WHERE` clause is what
a careless consumer forgets, and the consumer is often an agent answering in one shot.

`flow` is a **second axis, orthogonal to `category`**. `category` answers what kind of thing
was bought; `flow` answers whether anything was bought at all. Growing the ten-value category
enum to hold `transfer` and `payment` would have conflated two independent questions.

`rules/flows.toml` is matched against the **raw description**, before normalisation, because
normalisation is built to destroy exactly the words it needs — `AUTOPAY PYMT` is trailing
noise to a merchant name and is the entire signal here. Edit it, run `spend rebuild`, and every
historical card payment stops counting.

## Identity, in three layers

| layer | value | stable against |
|---|---|---|
| dedupe key | `(source, native_account, external_id, content_sha256)` | re-polling the same window forever |
| transaction key | `sha256(source, native_account, external_id)[:24]` | rebuilds, rules edits, recategorisation |
| account key | declared by hand in `accounts.toml` | reconnecting the Bridge; a CSV with no id |

`txn_key` deliberately **excludes** the account key, so renaming an account cannot orphan a
correction. `account_key` is deliberately **not** derived from Bridge ids, because
reconnecting issues a fresh `conn_id` and fresh account ids, and history keyed on those would
fork at the seam. Local identity is the only identity this system trusts.

An account the feed reports that `accounts.toml` does not claim is **not dropped**. It gets
`unmapped:<native_id>` and `spend doctor` exits non-zero until you name it. A bank account
silently missing from a spending tracker is the worst failure this system has.

## Scheduling

No cursor exists, deliberately. The watermark is `MAX(window_to)` over successful polls, the
daily budget is a count of today's, and "which accounts exist" is the account log. All three
derived, for the reason `store.pending_receipt_ids` already gives about the extraction queue:
a stored cursor would be a fifth thing that can disagree with the log. It also could not be
read back by a sync running while the store is locked, which settles it.

`plan_window` is pure and takes the poll history, so the whole schedule is testable with no
database, no network and no clock. It handles a 20-call/day budget (ceiling 24, four held back
for a human debugging), 89-day spans (ceiling 90, one day of timezone slack), a 5-day overlap
on every window, a 400-day backfill in chunks on first run, and **gap repair** — a hole left
by a week of failures is re-requested before the tail, because the tail will still be there
tomorrow and a gap that ages past ninety days is gone for good.

Jitter lives in the timer (`RandomizedDelaySec=1h`), which is the Bridge's own guidance and
the right place to express it.

## The one contract violation to avoid

**Never treat an empty `transactions` array as a quiet week without reading `errlist`.**

A `con.auth` on the Capital One connection comes back as HTTP 200 with zero transactions and
is indistinguishable from a frugal week. `docs/email-ingest.md` writes down the same rule for
the mail path; it is the same mistake with a different feed, and it is silent both times.

Every attempt is recorded — including the ones that returned nothing — and every error is
recorded verbatim on the poll row rather than raised. `outcome = 'partial'` with a non-empty
`records` is normal and correct: the accounts that worked are still in the payload, and
discarding them because one connection is broken would lose real data.

The only escalation is that a `con.*` code makes `spend feeds sync` exit non-zero and `doctor`
say so, because nothing but a human redoing the setup-token dance will fix it.
