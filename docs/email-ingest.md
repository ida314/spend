# The email path (not built)

The MVP ingests photographs only. This is the contract the emailed-receipt path will need,
written down now while it is still cheap to influence — `email-tracker` does not exist yet
either, and it should know it will have a second consumer.

## Why it isn't built

`~/Projects/email-tracker` is an empty directory. There is an approved design
(`~/.claude/plans/i-want-to-make-fizzy-starlight.md`) and no code. Building a client
against an API nobody has run would mean shipping something untestable and discovering the
mismatches later, one at a time.

## What is already in place for it

Nothing here needs a migration when that day comes:

- `receipts.source` accepts `'email'`, and `receipts.external_id` + `source_meta` are
  already columns, with a partial unique index on `(source, external_id)`.
- `meta` is a key/value table, which is where the poll cursor goes.
- `render.render_text()` produces the same `Document` an image does, so an emailed body
  reaches the existing prompt with no second prompt to keep in step.
- `service.ingest_bytes(..., source="email", external_id=...)` is the whole ingest call.

The new code is one module — `sources/mail.py` — and a timer.

## What we need from email-tracker

Only the read surface, which is its M1–M3 and the uncontested part of its design:

```
GET  /v1/messages?since=<cursor>&include_body=true&include_attachments=true
GET  /v1/messages/{id}/attachments/{part}
```

with a `read`-scope bearer token. No writes, no flags, no move. We never need
`PATCH /flags` — read state belongs to the mail client, not to a spending tracker, and
marking a receipt read here would change what you see in aerc.

## Identifying a receipt

`GET /v1/messages` has no sender or date filter by design — "start fully dumb", consumers
filter locally. We filter on the **plus-tag of the recipient local part**: any `To` or `Cc`
address of the form `<anything>+<tag>@<anything>` where `<tag>` is in
`SPEND_RECEIPT_TAGS`, default `receipt,receipts`.

The mailbox address is never hardcoded — only the tag is. Merchants get the plus-address at
checkout, so the match is exact and there are no sender patterns to maintain.

## Cursor handling

The cursor lives here, in `meta`, not there. Round-trip it verbatim; never parse it.

- `409 cursor_epoch_mismatch` (their index was rebuilt) and `409 cursor_too_old`
  (tombstones pruned past us) → restart from no cursor. That is safe *because* ingest is
  idempotent: `receipts.sha256` and the `(source, external_id)` index collapse the
  re-delivery. Re-polling the whole mailbox costs time, never duplicate transactions.
- The stream is compacted and ordered by learning-time, not send-time. A receipt from
  March that arrives in the index today gets today's cursor. Never sort by `sent_ts` and
  assume it correlates.

## The one contract violation to avoid

**Never treat `messages: []` as "no new receipts" without reading `sync.state`.** Both of
email-tracker's design memos raise this independently: an empty list from a mail sync that
has been failing for two days is indistinguishable from a quiet week, and the `sync` block
on every response exists to tell them apart. `sync.state != "ok"` is a warning surfaced on
the list page, not a silent zero.

This repo already holds the same rule for its own worker: a page with no transactions
prints the backlog and the last extraction error rather than an empty list.

## HTML bodies matter as much as attachments

Most online merchants send an HTML receipt with no PDF at all. `body_html` → text →
`render_text()` → the existing prompt. Attachments are the *less* common case, and PDF
attachments need a rasteriser this repo does not have yet — see the note on
`service.EXTENSIONS`.

## Schedule

`spend-poll.timer`, `OnCalendar=*-*-* 03:00`, `Persistent=true`, matching
`jobtracker-tomorrow.timer`. Nightly is well inside any tombstone-pruning window.
