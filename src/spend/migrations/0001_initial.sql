-- Truth and projections, in one file because they arrive together.
--
-- The three truth tables are append-only. Nothing in this codebase issues an UPDATE or a
-- DELETE against them, and a test asserts it. The two projections are derived from them
-- and are dropped and rebuilt whenever the projection logic or the category rules change,
-- so they are deliberately not migrated: `rebuild` is cheaper and cannot half-apply.

-- ---------------------------------------------------------------------------- truth ---

CREATE TABLE receipts (
    id          INTEGER PRIMARY KEY,
    sha256      TEXT    NOT NULL UNIQUE,   -- the same photo twice is one receipt
    path        TEXT    NOT NULL,          -- relative to the receipts root
    mime        TEXT    NOT NULL,
    bytes       INTEGER NOT NULL,
    source      TEXT    NOT NULL CHECK (source IN ('upload', 'email')),
    external_id TEXT,                      -- email-tracker message id, when that lands
    source_meta TEXT,                      -- json: {from, subject, received_at}
    ingested_at TEXT    NOT NULL
);

-- Partial, because every uploaded receipt has a NULL external_id and SQLite treats NULLs
-- as distinct in a UNIQUE index — but relying on that would make the constraint mean
-- something subtly different from what it says.
CREATE UNIQUE INDEX ux_receipts_external ON receipts (source, external_id)
    WHERE external_id IS NOT NULL;

CREATE TABLE extractions (
    id             INTEGER PRIMARY KEY,
    receipt_id     INTEGER NOT NULL REFERENCES receipts (id),
    status         TEXT    NOT NULL CHECK (status IN ('ok', 'invalid', 'error')),
    model          TEXT    NOT NULL,
    prompt_version TEXT    NOT NULL,
    render_mode    TEXT    NOT NULL CHECK (render_mode IN ('image', 'ocr', 'text')),
    raw_response   TEXT,                   -- exactly what came back, kept even when bad
    payload        TEXT,                   -- validated ReceiptData json; NULL unless ok
    note           TEXT,                   -- why it is invalid or errored
    latency_ms     INTEGER,
    created_at     TEXT    NOT NULL
);
CREATE INDEX ix_extractions_receipt ON extractions (receipt_id, created_at);

CREATE TABLE corrections (
    id         INTEGER PRIMARY KEY,
    receipt_id INTEGER NOT NULL REFERENCES receipts (id),
    field      TEXT    NOT NULL,
    value      TEXT,                       -- NULL means "clear this field"
    created_at TEXT    NOT NULL
);
CREATE INDEX ix_corrections_receipt ON corrections (receipt_id, created_at, id);

-- ---------------------------------------------------------------------- projections ---

CREATE TABLE transactions (
    receipt_id        INTEGER PRIMARY KEY REFERENCES receipts (id),
    extraction_id     INTEGER,
    merchant          TEXT,
    merchant_location TEXT,
    purchased_on      TEXT,
    currency          TEXT    NOT NULL DEFAULT 'USD',
    subtotal_cents    INTEGER,
    tax_cents         INTEGER,
    tip_cents         INTEGER,
    total_cents       INTEGER,
    category          TEXT,
    status            TEXT    NOT NULL CHECK (status IN ('pending','ok','needs_review','failed')),
    review_reason     TEXT,
    deleted           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_transactions_date ON transactions (purchased_on DESC);
CREATE INDEX ix_transactions_status ON transactions (status);

CREATE TABLE line_items (
    receipt_id  INTEGER NOT NULL REFERENCES receipts (id),
    line_no     INTEGER NOT NULL,
    description TEXT    NOT NULL,
    qty         TEXT,
    unit_cents  INTEGER,
    total_cents INTEGER,
    PRIMARY KEY (receipt_id, line_no)
);

-- Small key/value corner for things that are neither truth nor projection: the
-- email-tracker cursor will live here, round-tripped verbatim.
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
