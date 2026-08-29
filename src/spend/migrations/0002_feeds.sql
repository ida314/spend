-- Bank feeds: what the accounts say happened, as opposed to what a receipt says.
--
-- A second, parallel truth stack. It does not touch the receipt tables and it does not
-- pretend to know that the $4.75 the bank posted on Tuesday is the coffee photographed on
-- Monday. Joining the two is a later feature; everything here is shaped so that join is a
-- query rather than a migration -- every projected row carries a local date, an amount in
-- integer cents and a normalised merchant, which is all a match needs, and the two indexes
-- at the bottom of this file are the join.
--
-- The append-only rule holds here exactly as it does for receipts, and the same traced test
-- proves it. A pending charge that posts three days later is not an UPDATE; it is a second
-- observation of the same external transaction, appended, and the projection keeps the
-- newest. What that buys is what it buys for receipts: the day a bug is found in how a
-- description is normalised, one `rebuild` fixes every historical row, and nothing has to be
-- re-fetched from a bank that only keeps ninety days.
--
-- Rows reference each other by `uid`, never by `id`. `id` is a rowid, good for ordering
-- inside one database; `uid` is derived from the event body that produced the row, and it is
-- what survives this database being deleted and rebuilt from the sealed log. A foreign key
-- across `id` would point at a number that means something different after a replay.

-- ---------------------------------------------------------------------------- truth ---

CREATE TABLE feed_polls (
    id            INTEGER PRIMARY KEY,
    uid           TEXT    NOT NULL UNIQUE,
    source        TEXT    NOT NULL CHECK (source IN ('simplefin', 'drop')),
    outcome       TEXT    NOT NULL CHECK (outcome IN ('ok', 'partial', 'error')),
    started_at    TEXT    NOT NULL,
    finished_at   TEXT    NOT NULL,
    window_from   TEXT,                    -- ISO date, inclusive. NULL for a file drop.
    window_to     TEXT,
    http_status   INTEGER,
    file_sha256   TEXT,                    -- drop only: the bytes that were imported
    file_name     TEXT,
    accounts_seen INTEGER NOT NULL DEFAULT 0,
    records_seen  INTEGER NOT NULL DEFAULT 0,
    -- How many of those were new is deliberately NOT a column. It is not known until the
    -- child records have been sealed, so storing it would mean an UPDATE against a truth
    -- table -- the one thing this schema forbids. It is derived:
    --   SELECT COUNT(*) FROM feed_records WHERE poll_uid = feed_polls.uid
    errors        TEXT,                    -- json array of errlist entries, verbatim
    note          TEXT,
    seen_ids      TEXT,                    -- json {native_account: [external_id, ...]}
    detail        TEXT                     -- json: url path, latency_ms, dialect, row count
);

-- Every attempt is recorded, including the ones that returned nothing. Same rule
-- `extractions` follows, for the same reason: an empty list from a feed that has been
-- failing for two days is indistinguishable from a quiet week, and this table is what tells
-- them apart. `doctor`, the workspace manifest and the list page all read it.
CREATE INDEX ix_feed_polls_source ON feed_polls (source, started_at DESC);

-- A file is claimed by its bytes, so re-dropping the same export is a no-op. That is what
-- makes it safe to import an overlapping monthly export twice by accident.
CREATE UNIQUE INDEX ux_feed_polls_file ON feed_polls (file_sha256)
    WHERE file_sha256 IS NOT NULL;

-- `seen_ids` looks like waste and is not. Without it, a pending charge the bank silently
-- withdrew is indistinguishable from one it merely stopped mentioning, because an unchanged
-- transaction inserts no row at all. Storing the id set per poll keeps "did this vanish"
-- derivable at rebuild time, which is the difference between a projection that recomputes
-- and one that needs a mutable last-seen column. Three accounts over a ninety-day window is
-- a few hundred short strings; under 4 MB a year.

CREATE TABLE feed_accounts (
    id                      INTEGER PRIMARY KEY,
    uid                     TEXT    NOT NULL UNIQUE,
    poll_uid                TEXT    NOT NULL,
    source                  TEXT    NOT NULL,
    native_id               TEXT    NOT NULL,   -- SimpleFIN account id, or 'drop:<dialect>:<hint>'
    org_name                TEXT,
    org_domain              TEXT,
    org_id                  TEXT,
    name                    TEXT,
    currency                TEXT    NOT NULL DEFAULT 'USD',
    balance_cents           INTEGER,
    available_balance_cents INTEGER,
    balance_at              TEXT,              -- ISO instant, from balance-date
    content_sha256          TEXT    NOT NULL,
    raw                     TEXT    NOT NULL,  -- the Account object with `transactions` removed
    observed_at             TEXT    NOT NULL
);

-- One row per *changed* state. A daily poll where the balance moved appends; one where it
-- did not is a no-op. The accumulation is the balance history, obtained for free rather than
-- by a snapshot cron that can miss a day.
CREATE UNIQUE INDEX ux_feed_accounts_content
    ON feed_accounts (source, native_id, content_sha256);
CREATE INDEX ix_feed_accounts_native ON feed_accounts (source, native_id, observed_at);

CREATE TABLE feed_records (
    id             INTEGER PRIMARY KEY,
    uid            TEXT    NOT NULL UNIQUE,
    poll_uid       TEXT    NOT NULL,
    source         TEXT    NOT NULL CHECK (source IN ('simplefin', 'drop')),
    native_account TEXT    NOT NULL,
    external_id    TEXT    NOT NULL,      -- SimpleFIN id, OFX FITID, or a synthesised digest
    posted_at      TEXT,                  -- ISO instant; NULL while pending (posted == 0)
    transacted_at  TEXT,
    pending        INTEGER NOT NULL DEFAULT 0,
    amount_cents   INTEGER,               -- signed: negative left the account. NULL when the
                                          -- string was not an amount -- absence, never zero.
    currency       TEXT    NOT NULL DEFAULT 'USD',
    description    TEXT    NOT NULL DEFAULT '',
    payee          TEXT,
    memo           TEXT,
    -- What the source itself said this is, when it said so in a field rather than in prose:
    -- Apple Card ships a `Type` column, OFX ships <TRNTYPE>. Kept separate from `raw`, which
    -- is verbatim and must stay that way, and separate from the projected `flow`, which is
    -- derived and rebuildable. A stated fact beats a substring guess but loses to
    -- rules/flows.toml, which is the hand-written override.
    flow_hint      TEXT,
    reject_reason  TEXT,                  -- why amount_cents is NULL, when it is
    content_sha256 TEXT    NOT NULL,      -- over the canonical subset, not the whole payload
    raw            TEXT    NOT NULL,      -- exactly what came back, kept even when bad
    observed_at    TEXT    NOT NULL
);

-- The idempotence guarantee. Re-polling a ninety-day window every night for a year inserts
-- nothing after the first pass, which is what makes an overlapping window free and a lost
-- cursor harmless. A pending charge that posts has a different canonical subset and so lands
-- as a second row rather than as an UPDATE.
CREATE UNIQUE INDEX ux_feed_records_content
    ON feed_records (source, native_account, external_id, content_sha256);
CREATE INDEX ix_feed_records_txn
    ON feed_records (source, native_account, external_id, observed_at, id);

CREATE TABLE feed_corrections (
    id         INTEGER PRIMARY KEY,
    uid        TEXT    NOT NULL UNIQUE,
    txn_key    TEXT    NOT NULL,          -- independent of accounts.toml, on purpose
    field      TEXT    NOT NULL,          -- merchant | category | flow | note | deleted
    value      TEXT,                      -- NULL means "clear this field"
    created_at TEXT    NOT NULL
);
CREATE INDEX ix_feed_corrections_txn ON feed_corrections (txn_key, created_at, id);

-- ----------------------------------------------------------------------- projections ---
-- Dropped and rebuilt by `rebuild()`, like `transactions` and `line_items`. No foreign key
-- between the two tables below on purpose: `clear_projections` deletes them in a tuple
-- order, and a declared FK would make that order load-bearing for a reason nobody reading
-- store.py would guess.

CREATE TABLE accounts (
    account_key   TEXT PRIMARY KEY,        -- yours, from rules/accounts.toml
    display_name  TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('checking','savings','credit','other')),
    institution   TEXT,
    currency      TEXT NOT NULL DEFAULT 'USD',
    sources       TEXT NOT NULL,           -- json array: which adapters feed this account
    native_ids    TEXT NOT NULL,           -- json array, newest observation first
    balance_cents INTEGER,
    balance_at    TEXT,
    last_seen_at  TEXT,
    covers_from   TEXT,                    -- earliest dated_on this account has data for
    covers_to     TEXT,
    unmapped      INTEGER NOT NULL DEFAULT 0
);

-- An account the feed reported that accounts.toml does not claim gets the synthetic key
-- 'unmapped:<native_id>' and unmapped=1 rather than being dropped. A bank account silently
-- absent from a spending tracker is the worst failure this system has, so `doctor` exits
-- non-zero while one exists.

CREATE TABLE feed_transactions (
    txn_key        TEXT PRIMARY KEY,
    account_key    TEXT NOT NULL,
    source         TEXT NOT NULL,
    native_account TEXT NOT NULL,
    external_id    TEXT NOT NULL,
    record_uid     TEXT NOT NULL,          -- the observation this row was derived from

    -- Truth holds UTC instants; the projection holds local calendar dates, computed with
    -- config.TZ. "What did I spend in August" is a local-calendar question, and a 9pm charge
    -- on the 31st is September in UTC. Changing TZ and rebuilding re-files the boundary
    -- rows, which is correct and would be impossible if the date were frozen into truth.
    posted_on      TEXT,
    transacted_on  TEXT,
    dated_on       TEXT,                   -- COALESCE(transacted_on, posted_on). Group by this.

    pending        INTEGER NOT NULL DEFAULT 0,
    currency       TEXT NOT NULL DEFAULT 'USD',

    -- Four amount columns, because one signed integer is a trap for every consumer.
    --   amount_cents    signed, bank convention. The fact. NULL when unparseable.
    --   outflow_cents   max(0, -amount).  Never negative.
    --   inflow_cents    max(0,  amount).  Never negative.
    --   net_spend_cents the column you SUM. Positive for a purchase, negative for a refund,
    --                   and exactly zero for transfers, card payments, income, unparseable
    --                   rows and tombstones. Safe to sum over any subset with no WHERE
    --                   clause at all -- which is the point, because the WHERE clause is
    --                   what a careless consumer forgets.
    amount_cents    INTEGER,
    outflow_cents   INTEGER,
    inflow_cents    INTEGER,
    net_spend_cents INTEGER NOT NULL DEFAULT 0,

    description   TEXT NOT NULL DEFAULT '',    -- verbatim, ugly, never overwritten
    merchant      TEXT,                        -- normalise.merchant(description)
    merchant_key  TEXT,                        -- casefolded, for joining to receipts
    category      TEXT,                        -- the same ten as schema.CATEGORIES
    flow          TEXT NOT NULL
                  CHECK (flow IN ('spend','refund','fee','income','transfer',
                                  'payment','unknown')),
    counts_as_spending INTEGER NOT NULL DEFAULT 0,

    status        TEXT NOT NULL CHECK (status IN ('ok','pending','needs_review','ignored')),
    review_reason TEXT,

    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    revisions         INTEGER NOT NULL DEFAULT 1,
    amount_changed    INTEGER NOT NULL DEFAULT 0,  -- the tip-adjustment signal
    vanished          INTEGER NOT NULL DEFAULT 0,  -- pending, then absent from a later poll
    duplicate_suspect INTEGER NOT NULL DEFAULT 0,
    deleted           INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX ix_feed_txn_date     ON feed_transactions (dated_on DESC);
CREATE INDEX ix_feed_txn_account  ON feed_transactions (account_key, dated_on DESC);
CREATE INDEX ix_feed_txn_merchant ON feed_transactions (merchant_key, dated_on);

-- The join the future receipt-to-statement matcher needs: a date window and an amount in
-- cents. Cheap now, free later.
CREATE INDEX ix_feed_txn_match    ON feed_transactions (dated_on, amount_cents);

-- The one change to an existing projection. Receipt merchants are raw model output
-- ("TRADER JOE'S #546") and feed merchants are normalised ("Trader Joe's"); until the two
-- streams share a vocabulary they cannot be compared at all. This runs the receipt merchant
-- through the same normaliser. It is a projection column, so it costs a rebuild and no
-- backfill -- but the DDL has to be here, because clear_projections() issues DELETE and not
-- DROP TABLE, so a projection's shape still lives in a migration even though its rows do not.
ALTER TABLE transactions ADD COLUMN merchant_key TEXT;
CREATE INDEX ix_transactions_match ON transactions (purchased_on, total_cents);
