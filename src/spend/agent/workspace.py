"""A decrypted, read-only snapshot of the ledger, shaped for something that reads it once.

The rule this module exists to enforce is that **summing every row in the directory, with no
filter at all, gives the right answer.** That is not a convention documented in CLAUDE.md and
hoped for; it is a property of the data. Every receipt row carries `net_spend_cents: 0`, and
`ledger.db` holds a CHECK constraint saying so.

The reason is that the two streams here describe overlapping reality and are deliberately not
deduplicated -- the $47.03 at Trader Joe's is very likely both a photographed receipt and a
statement line. The failure mode is an agent summing both and reporting 1.8x the real number,
confidently, in one shot. A rule in a markdown file is a poor defence against that; a shape
that makes the mistake arithmetically impossible is a good one. The receipt stream stays
available for what it is actually good at -- line items, tax, tip, what was in the bag --
and reaching for `total_cents` is a deliberate act, which is exactly when you want an agent
to be deliberate.

Three other decisions worth naming:

**It lives on tmpfs and it is a snapshot.** Nothing written here is saved, and it is gone at
`spend lock`. Receipt *images* are never materialised: the runtime tmpfs is measured in
hundreds of megabytes and a year of phone photographs is not.

**It is deterministic.** Rows sort by `(date, id)` and serialise with sorted keys, so a
rebuild where nothing changed is byte-identical. That makes "what changed since this morning"
a `diff` rather than a feature somebody has to build.

**It is replaced atomically.** Built into a sibling directory and `os.replace`d as a whole,
so an agent reading halfway through a build never sees half a ledger.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from spend import config, paths, store
from spend.schema import CATEGORIES

HERE = Path(__file__).resolve().parent
QUERIES = HERE / "queries"
TEMPLATES = HERE / "templates"
FORMAT_VERSION = 1

FLOWS = ("spend", "refund", "fee", "income", "transfer", "payment", "unknown")
SPENDING_FLOWS = ("spend", "refund", "fee")


# One list, used by the JSONL writer, the ledger.db DDL and SCHEMA.md alike. A test asserts
# the documentation matches `PRAGMA table_info`, which is the "enforced twice" pattern from
# `schema.py` applied to prose that would otherwise rot silently.
@dataclass(frozen=True)
class Column:
    name: str
    sql: str
    summable: bool
    note: str


BANK_COLUMNS = (
    Column("id", "TEXT PRIMARY KEY", False, "stable across rebuilds; the same value grep and SQL see"),
    Column("stream", "TEXT NOT NULL", False, "always 'bank' here"),
    Column("date", "TEXT NOT NULL", False, "local calendar date. GROUP BY this one"),
    Column("posted_on", "TEXT", False, "when it settled; NULL while pending"),
    Column("transacted_on", "TEXT", False, "when it happened, if the bank said"),
    Column("pending", "INTEGER NOT NULL", False, "1 = not settled; the amount can still change"),
    Column("account", "TEXT NOT NULL", False, "your account key, from accounts.toml"),
    Column("account_name", "TEXT", False, "denormalised so no question needs a join"),
    Column("account_kind", "TEXT", False, "checking | savings | credit | other"),
    Column("institution", "TEXT", False, "denormalised"),
    Column("source", "TEXT NOT NULL", False, "simplefin | drop"),
    Column("description", "TEXT NOT NULL", False, "verbatim from the bank, ugly, never overwritten"),
    Column("merchant", "TEXT", False, "normalised and comparable"),
    Column("merchant_key", "TEXT", False, "casefolded; joins to the receipt stream"),
    Column("category", "TEXT", False, f"one of: {', '.join(CATEGORIES)}"),
    Column("flow", "TEXT NOT NULL", False, f"one of: {', '.join(FLOWS)}"),
    Column("amount_cents", "INTEGER", False, "SIGNED, bank convention. Negative left the account. Do NOT sum"),
    Column("outflow_cents", "INTEGER", True, "max(0, -amount). Summing this double-counts card payments"),
    Column("inflow_cents", "INTEGER", True, "max(0, amount)"),
    Column("net_spend_cents", "INTEGER NOT NULL", True, "THE ONE TO SUM. Zero for transfers, payments, income"),
    Column("amount", "TEXT", False, "decimal string, DISPLAY ONLY. Never do arithmetic on it"),
    Column("currency", "TEXT NOT NULL", False, "ISO 4217"),
    Column("counts_as_spending", "INTEGER NOT NULL", False, "1 when net_spend_cents is not forced to zero"),
    Column("status", "TEXT NOT NULL", False, "ok | pending | needs_review | ignored"),
    Column("review_reason", "TEXT", False, "why, when status is needs_review"),
    Column("revisions", "INTEGER NOT NULL", False, "how many times the bank restated this row"),
    Column("amount_changed", "INTEGER NOT NULL", False, "1 when a restatement moved the money (a tip cleared)"),
    Column("duplicate_suspect", "INTEGER NOT NULL", False, "1 when this looks like the same charge from two sources"),
    Column("external_id", "TEXT", False, "the source's own id"),
    Column("first_seen_at", "TEXT", False, "when this system first saw it"),
    Column("last_seen_at", "TEXT", False, "the most recent observation"),
)

RECEIPT_COLUMNS = (
    Column("id", "TEXT PRIMARY KEY", False, "'receipt:<n>'"),
    Column("stream", "TEXT NOT NULL", False, "always 'receipt' here"),
    Column("receipt_id", "INTEGER NOT NULL", False, "its id in the app"),
    Column("date", "TEXT", False, "purchased on, as read off the photograph"),
    Column("merchant", "TEXT", False, "normalised"),
    Column("merchant_raw", "TEXT", False, "what the model actually read"),
    Column("merchant_key", "TEXT", False, "casefolded; joins to the bank stream"),
    Column("category", "TEXT", False, f"one of: {', '.join(CATEGORIES)}"),
    Column("flow", "TEXT NOT NULL", False, "always 'spend'"),
    Column("total_cents", "INTEGER", False, "the receipt's own total, POSITIVE. Scope any use to receipts"),
    Column("subtotal_cents", "INTEGER", False, ""),
    Column("tax_cents", "INTEGER", False, ""),
    Column("tip_cents", "INTEGER", False, ""),
    Column("currency", "TEXT NOT NULL", False, "ISO 4217"),
    Column("net_spend_cents", "INTEGER NOT NULL", True,
           "ALWAYS 0, by construction. This is what makes summing the whole directory correct"),
    Column("status", "TEXT NOT NULL", False, "ok | pending | needs_review | failed"),
    Column("review_reason", "TEXT", False, ""),
    Column("receipt_url", "TEXT", False, "where to look at the photograph"),
)


def columns(stream: str) -> tuple[Column, ...]:
    return BANK_COLUMNS if stream == "bank" else RECEIPT_COLUMNS


def _amount_string(cents: int | None) -> str | None:
    if cents is None:
        return None
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def _json_line(row: dict) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --- rows ------------------------------------------------------------------------------------

def bank_rows(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute(
        "SELECT t.*, a.display_name, a.kind, a.institution FROM feed_transactions t"
        " LEFT JOIN accounts a ON a.account_key = t.account_key"
        " WHERE t.deleted = 0 ORDER BY t.dated_on, t.txn_key"
    ):
        out.append({
            "id": r["txn_key"], "stream": "bank",
            "date": r["dated_on"], "posted_on": r["posted_on"],
            "transacted_on": r["transacted_on"], "pending": bool(r["pending"]),
            "account": r["account_key"], "account_name": r["display_name"],
            "account_kind": r["kind"], "institution": r["institution"],
            "source": r["source"], "description": r["description"],
            "merchant": r["merchant"], "merchant_key": r["merchant_key"],
            "category": r["category"], "flow": r["flow"],
            "amount_cents": r["amount_cents"], "outflow_cents": r["outflow_cents"],
            "inflow_cents": r["inflow_cents"], "net_spend_cents": r["net_spend_cents"],
            "amount": _amount_string(r["amount_cents"]), "currency": r["currency"],
            "counts_as_spending": bool(r["counts_as_spending"]),
            "status": r["status"], "review_reason": r["review_reason"],
            "revisions": r["revisions"], "amount_changed": bool(r["amount_changed"]),
            "duplicate_suspect": bool(r["duplicate_suspect"]),
            "external_id": r["external_id"],
            "first_seen_at": r["first_seen_at"], "last_seen_at": r["last_seen_at"],
            # The pre-built join tuple, present identically on both streams. Sixty bytes, and
            # the day the matcher lands it is a join on three fields an agent can already do
            # by hand today.
            "match": {"date": r["dated_on"], "amount_cents": r["amount_cents"],
                      "merchant_key": r["merchant_key"]},
        })
    return out


def receipt_rows(conn: sqlite3.Connection) -> list[dict]:
    from spend import normalize
    base = f"http://{config.HOST}:{config.PORT}"
    out = []
    for r in conn.execute(
        "SELECT * FROM transactions WHERE deleted = 0 ORDER BY purchased_on, receipt_id"
    ):
        merchant = normalize.merchant(r["merchant"]) if r["merchant"] else None
        out.append({
            "id": f"receipt:{r['receipt_id']}", "stream": "receipt",
            "receipt_id": r["receipt_id"], "date": r["purchased_on"],
            "merchant": merchant, "merchant_raw": r["merchant"],
            "merchant_key": normalize.key(merchant), "category": r["category"],
            "flow": "spend",
            "total_cents": r["total_cents"], "subtotal_cents": r["subtotal_cents"],
            "tax_cents": r["tax_cents"], "tip_cents": r["tip_cents"],
            "currency": r["currency"],
            # Zero, always, by construction. See this module's docstring.
            "net_spend_cents": 0,
            "status": r["status"], "review_reason": r["review_reason"],
            "receipt_url": f"{base}/r/{r['receipt_id']}",
            "match": {"date": r["purchased_on"],
                      "amount_cents": -r["total_cents"] if r["total_cents"] is not None else None,
                      "merchant_key": normalize.key(merchant)},
        })
    return out


def line_items(conn: sqlite3.Connection) -> list[dict]:
    return [{"id": f"receipt:{r['receipt_id']}", "line_no": r["line_no"],
             "description": r["description"], "qty": r["qty"],
             "unit_cents": r["unit_cents"], "total_cents": r["total_cents"]}
            for r in conn.execute("SELECT * FROM line_items ORDER BY receipt_id, line_no")]


# --- the SQL view ------------------------------------------------------------------------------

def ddl() -> str:
    def table(name: str, cols: tuple[Column, ...], extra: str = "") -> str:
        body = ",\n    ".join(f"{c.name:<20} {c.sql}" for c in cols)
        return f"CREATE TABLE {name} (\n    {body}{extra}\n);"

    return "\n\n".join([
        f"PRAGMA user_version = {FORMAT_VERSION};",
        table("bank", BANK_COLUMNS),
        "CREATE INDEX ix_bank_date     ON bank (date);",
        "CREATE INDEX ix_bank_merchant ON bank (merchant_key, date);",
        "CREATE INDEX ix_bank_cat      ON bank (category, date);",
        "CREATE INDEX ix_bank_account  ON bank (account, date);",
        table("receipts", RECEIPT_COLUMNS,
              # Not a formality. A receipt is a photograph of a purchase the bank has almost
              # certainly also reported, and nothing here is deduplicated. Zero is what makes
              # "sum every row in the workspace" give the right answer, and a CHECK is what
              # makes it impossible to stop being true by accident.
              ",\n    CHECK (net_spend_cents = 0)"),
        "CREATE INDEX ix_receipts_date  ON receipts (date);",
        "CREATE INDEX ix_receipts_match ON receipts (date, total_cents);",
        "CREATE TABLE items (\n"
        "    id          TEXT NOT NULL REFERENCES receipts (id),\n"
        "    line_no     INTEGER NOT NULL,\n"
        "    description TEXT NOT NULL,\n"
        "    qty         TEXT,\n"
        "    unit_cents  INTEGER,\n"
        "    total_cents INTEGER,\n"
        "    PRIMARY KEY (id, line_no)\n);",
        "CREATE TABLE accounts (\n"
        "    account TEXT PRIMARY KEY, name TEXT, kind TEXT, institution TEXT,\n"
        "    sources TEXT, currency TEXT, balance_cents INTEGER, balance_at TEXT,\n"
        "    last_seen_at TEXT, covers_from TEXT, covers_to TEXT, unmapped INTEGER\n);",
        "CREATE TABLE feed_polls (\n"
        "    source TEXT, outcome TEXT, started_at TEXT, window_from TEXT, window_to TEXT,\n"
        "    records_seen INTEGER, records_new INTEGER, errors TEXT\n);",
        "-- Start here. Transfers, card payments, income, tombstones and unparseable rows\n"
        "-- are already gone, so a forgotten WHERE clause cannot double-count a year of\n"
        "-- card payments.\n"
        "CREATE VIEW spending AS SELECT * FROM bank WHERE counts_as_spending = 1;",
        "CREATE VIEW monthly AS\n"
        "SELECT substr(date, 1, 7) AS month,\n"
        "       COALESCE(category, 'uncategorised') AS category,\n"
        "       COUNT(*) AS n, SUM(net_spend_cents) AS cents\n"
        "FROM spending GROUP BY 1, 2;",
        "-- Both streams in one shape, for \"show me everything about this merchant\".\n"
        "-- Still safe to sum: net_spend_cents is zero on every receipt row.\n"
        "CREATE VIEW everything AS\n"
        "SELECT id, 'bank' AS stream, date, account, merchant_key, category, description,\n"
        "       net_spend_cents FROM bank\n"
        "UNION ALL\n"
        "SELECT id, 'receipt', date, NULL, merchant_key, category, merchant_raw,\n"
        "       net_spend_cents FROM receipts;",
    ]) + "\n"


def write_ledger(dest: Path, bank: list[dict], receipts: list[dict], items: list[dict],
                 accounts: list[dict], polls: list[dict]) -> None:
    db = sqlite3.connect(dest)
    try:
        db.executescript(ddl())
        _insert(db, "bank", [c.name for c in BANK_COLUMNS], bank)
        _insert(db, "receipts", [c.name for c in RECEIPT_COLUMNS], receipts)
        _insert(db, "items", ["id", "line_no", "description", "qty", "unit_cents",
                              "total_cents"], items)
        _insert(db, "accounts", ["account", "name", "kind", "institution", "sources",
                                 "currency", "balance_cents", "balance_at", "last_seen_at",
                                 "covers_from", "covers_to", "unmapped"], accounts)
        _insert(db, "feed_polls", ["source", "outcome", "started_at", "window_from",
                                   "window_to", "records_seen", "records_new", "errors"],
                polls)
        db.commit()
    finally:
        db.close()


def _insert(db: sqlite3.Connection, table: str, cols: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    db.executemany(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [tuple(_scalar(r.get(c)) for c in cols) for r in rows])


def _scalar(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return value


# --- the build ----------------------------------------------------------------------------------

def build(conn: sqlite3.Connection, dest: Path | None = None) -> dict:
    """Materialise the whole workspace. Returns the manifest."""
    dest = dest or paths.agent_dir()
    staging = dest.with_name(dest.name + ".tmp")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    bank = bank_rows(conn)
    receipts = receipt_rows(conn)
    items = line_items(conn)
    accounts = [{"account": r["account_key"], "name": r["display_name"], "kind": r["kind"],
                 "institution": r["institution"], "sources": r["sources"],
                 "currency": r["currency"], "balance_cents": r["balance_cents"],
                 "balance_at": r["balance_at"], "last_seen_at": r["last_seen_at"],
                 "covers_from": r["covers_from"], "covers_to": r["covers_to"],
                 "unmapped": r["unmapped"]}
                for r in conn.execute("SELECT * FROM accounts ORDER BY account_key")]
    polls = [dict(r) for r in store.feed_polls_all(conn)]

    _write_partitioned(staging / "bank", bank)
    _write_partitioned(staging / "receipts", receipts)
    # accounts.json, not accounts.jsonl, and the extension is load-bearing. CLAUDE.md tells
    # an agent it can cat every .jsonl file in this directory and sum the result -- so the
    # only .jsonl files here must be transaction rows. A stray one with no net_spend_cents
    # would make the one instruction that matters most throw a KeyError.
    (staging / "accounts.json").write_text(
        json.dumps(accounts, indent=2, sort_keys=True) + "\n")
    (staging / "categories.json").write_text(
        json.dumps({"categories": list(CATEGORIES), "flows": list(FLOWS),
                    "counts_as_spending": list(SPENDING_FLOWS)},
                   indent=2, sort_keys=True) + "\n")

    write_ledger(staging / "ledger.db", bank, receipts, items,
                 accounts, [{k: p.get(k) for k in
                             ("source", "outcome", "started_at", "window_from", "window_to",
                              "records_seen", "records_new", "errors")} for p in polls])

    shutil.copytree(QUERIES, staging / "queries")
    manifest = _manifest(bank, receipts, accounts, polls)
    (staging / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (staging / "CLAUDE.md").write_text((TEMPLATES / "CLAUDE.md").read_text())
    (staging / "SCHEMA.md").write_text(schema_markdown())

    # Replaced whole, so an agent reading mid-build never sees half a ledger.
    old = dest.with_name(dest.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        dest.rename(old)
    os.replace(staging, dest)
    shutil.rmtree(old, ignore_errors=True)
    return manifest


def _write_partitioned(root: Path, rows: list[dict]) -> None:
    """One JSONL per calendar month, by local date.

    A month is the unit a human thinks in, it makes "August" a file open rather than a grep,
    files stay at tens to hundreds of rows, and a rebuild that changes one month rewrites one
    file -- so a `diff` between builds is readable.
    """
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        month = (row.get("date") or "0000-00")[:7]
        buckets.setdefault(month, []).append(row)
    for month, group in sorted(buckets.items()):
        year = month[:4]
        (root / year).mkdir(parents=True, exist_ok=True)
        group.sort(key=lambda r: ((r.get("date") or ""), r["id"]))
        (root / year / f"{month}.jsonl").write_text(
            "".join(_json_line(r) + "\n" for r in group))


def _manifest(bank, receipts, accounts, polls) -> dict:
    def span(rows):
        dates = sorted(r["date"] for r in rows if r.get("date"))
        return {"from": dates[0] if dates else None, "to": dates[-1] if dates else None,
                "rows": len(rows)}

    warnings = []
    health = {}
    for source in ("simplefin", "drop"):
        mine = [p for p in polls if p["source"] == source]
        good = [p for p in mine if p["outcome"] in ("ok", "partial")]
        last_ok = max((p["started_at"] for p in good), default=None)
        stale = None
        if last_ok:
            stale = (datetime.now(UTC) - datetime.fromisoformat(last_ok)).days
        failed = [p for p in mine if p["outcome"] == "error"]
        health[source] = {
            "last_ok": last_ok, "stale_days": stale,
            "last_error": (failed[-1]["note"] if failed else None),
            "polls": len(mine),
        }
        if stale is not None and stale > 2:
            warnings.append(
                f"{source} has not succeeded in {stale} days; recent weeks are incomplete")
        if not mine:
            warnings.append(f"{source} has never run")

    for a in accounts:
        if a["unmapped"]:
            warnings.append(f"{a['account']} is not named in accounts.toml")

    return {
        "built_at": datetime.now(UTC).isoformat(),
        "format": FORMAT_VERSION,
        "timezone": config.TZ,
        "coverage": {"bank": span(bank), "receipts": span(receipts)},
        "accounts": [{"account": a["account"], "covers_from": a["covers_from"],
                      "covers_to": a["covers_to"], "last_seen_at": a["last_seen_at"],
                      "balance_cents": a["balance_cents"]} for a in accounts],
        "feed_health": health,
        "warnings": warnings,
    }


def schema_markdown() -> str:
    """Generated from the same column list the writer uses.

    A test asserts it matches `PRAGMA table_info` on a freshly built ledger, which is the
    "enforced twice" pattern from `schema.py` pointed at documentation -- the one kind of
    artifact that otherwise rots without anybody noticing.
    """
    def table(cols: tuple[Column, ...]) -> str:
        head = "| field | type | safe to sum | meaning |\n|---|---|---|---|\n"
        return head + "\n".join(
            f"| `{c.name}` | {c.sql.split()[0]} | {'**yes**' if c.summable else 'no'} "
            f"| {c.note} |" for c in cols) + "\n"

    return (TEMPLATES / "SCHEMA.md").read_text().replace(
        "<!--BANK-->", table(BANK_COLUMNS)).replace(
        "<!--RECEIPT-->", table(RECEIPT_COLUMNS)).replace(
        "<!--DDL-->", "```sql\n" + ddl() + "```\n").replace(
        "<!--CATEGORIES-->", ", ".join(f"`{c}`" for c in CATEGORIES)).replace(
        "<!--FLOWS-->", ", ".join(f"`{f}`" for f in FLOWS)).replace(
        "<!--SPENDING-->", ", ".join(f"`{f}`" for f in SPENDING_FLOWS))
