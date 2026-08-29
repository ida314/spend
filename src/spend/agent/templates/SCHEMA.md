# Schema

Reference for the files in this directory. `CLAUDE.md` is the part to read first; this is the
part to look things up in.

## Layout

```
CLAUDE.md          how to reason over this data. Read it before the data.
SCHEMA.md          this file
MANIFEST.json      what is and is not covered, per stream and per account
ledger.db          SQLite: the same rows, indexed, plus three views
categories.json    the category and flow vocabularies
accounts.json      one object per account (a JSON array, not JSONL --
                   every .jsonl file here is transaction rows and nothing else)
bank/<YYYY>/<YYYY-MM>.jsonl       statement rows, by local calendar month
receipts/<YYYY>/<YYYY-MM>.jsonl   receipt-derived rows, same partitioning
queries/*.sql      worked queries; each says what it does not count
```

Partitioning is **one JSONL per calendar month per stream, keyed on the local `date`**. Rows
inside a file are sorted by `(date, id)` and serialised with sorted keys, so a rebuild where
nothing changed is byte-identical and `diff` between builds is readable.

## The bank stream

<!--BANK-->

## The receipt stream

`net_spend_cents` is **always 0** here, by construction, and `ledger.db` enforces it with a
`CHECK` constraint. The two streams describe overlapping reality and are not deduplicated, so
zero is what makes "sum every row in the directory" give the right answer. A receipt's own
total is `total_cents`, positive.

<!--RECEIPT-->

Line items live in the `items` table in `ledger.db`, keyed on the receipt's `id`. They exist
only for receipts: a bank knows the total, not what was in the bag.

## Vocabularies

**Categories** — <!--CATEGORIES-->

Derived at projection time from a case-folded substring match against the *normalised*
merchant name. Unmatched merchants fall through to the model's own guess (receipts) or to
`NULL` (statements). The tail is not trustworthy; say so when an answer rests on it.

**Flows** — <!--FLOWS-->

`category` answers *what kind of thing was bought*. `flow` answers *whether anything was
bought at all*. They are independent. Only these flows contribute to `net_spend_cents`:
<!--SPENDING-->. Everything else contributes exactly zero.

## Identity and provenance

- `id` on a bank row is `sha256(source, native_account, external_id)[:24]`. It is stable
  across rebuilds, across rules-file edits, and across renaming an account — the account key
  is deliberately *not* part of it, so a correction cannot be orphaned by an edit.
- `id` on a receipt row is `receipt:<n>`, its id in the app.
- `revisions` counts how many times the source restated a transaction. `first_seen_at` is
  when this system first saw it; `last_seen_at` is the most recent observation.
- `amount_changed` is true when two observations disagreed about the amount — a pre-auth that
  posted at a different figure, most often a tip clearing.
- `duplicate_suspect` is true when a row looks like the same charge already present from a
  different source. **Nothing is merged and nothing is deleted**; it is a flag.
- **Nothing in this directory is deduplicated across streams.** That is deliberate, and it is
  why every receipt row nets to zero.

## The SQL view

<!--DDL-->
