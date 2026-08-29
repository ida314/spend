"""SQLite, and the only module that writes it.

Migrations are numbered forward-only `.sql` files applied in order, recorded in a
`_migrations` ledger with a sha256 of the text that was applied. Nothing generates SQL —
the files are the authority, and a checksum mismatch is a hard error rather than a
warning, because a migration that was edited after being applied means the database on
disk and the file on disk describe different schemas and only one of them is real.

The projection tables are exempt. They are dropped and rebuilt by `rebuild()`, so a change
to how a transaction is derived is a code change, not a migration.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spend import paths

# Inside the package, not at the repo root: a wheel installed anywhere still carries its
# own schema, so `migrate()` cannot silently find nothing and leave an empty database
# looking like a working one.
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

PROJECTION_TABLES = ("transactions", "line_items", "feed_transactions", "accounts")


def _json(value) -> str | None:
    """Sorted keys, always.

    The live path builds this dict from a pydantic model, in field order; replay builds the
    same dict out of a sealed record, which was canonicalised with sorted keys. Both write
    the same column, and a column whose text depends on which path wrote it would make
    "replay reproduces the cache exactly" false for a reason that means nothing.
    """
    return None if value is None else json.dumps(value, sort_keys=True)


def now() -> str:
    """UTC, ISO 8601, microsecond precision. Stored times are instants; display converts.

    Microseconds, not seconds, and the reason is load-bearing. Corrections replay in
    `(created_at, id)` order and the last edit to a field is the one that survives. Once the
    log is the truth, `id` is assigned by replay rather than by insertion, so it can no
    longer break a same-second tie -- the tie has to be broken by data the record itself
    carries, and the fallback below that is a content hash, which is a uniformly random
    permutation. Two submissions to the same field within one second (a double-tap on Save,
    a phone retrying a POST) would turn last-edit-wins into coin-flip-wins, silently.

    One clock, one precision, everywhere. Whole-second values from older rows still sort
    before same-second values carrying microseconds, so nothing already written moves.
    """
    return datetime.now(UTC).isoformat()


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or paths.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")       # a reader during extraction is normal
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- migrations -------------------------------------------------------------------------

class MigrationError(RuntimeError):
    pass


def migrate(conn: sqlite3.Connection) -> list[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _migrations ("
        " name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied = {r["name"]: r["sha256"] for r in conn.execute("SELECT * FROM _migrations")}
    ran = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        text = f.read_text()
        digest = hashlib.sha256(text.encode()).hexdigest()
        if f.name in applied:
            if applied[f.name] != digest:
                raise MigrationError(
                    f"{f.name} changed after it was applied. The database and this file "
                    f"no longer describe the same schema; write a new migration instead."
                )
            continue
        conn.executescript(text)
        conn.execute(
            "INSERT INTO _migrations (name, sha256, applied_at) VALUES (?,?,?)",
            (f.name, digest, now()),
        )
        conn.commit()
        ran.append(f.name)
    return ran


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) n FROM _migrations").fetchone()
    return row["n"] if row else 0


# --- truth: append only -------------------------------------------------------------------

def insert_receipt(
    conn: sqlite3.Connection, *, sha256: str, path: str, mime: str, bytes_: int,
    source: str, external_id: str | None = None, source_meta: dict | None = None,
    at: str | None = None,
) -> tuple[int, bool]:
    """Record an ingested receipt. Returns (receipt_id, is_new).

    Re-ingesting the same bytes is a no-op that returns the original id, which is what
    makes a re-poll after a lost cursor safe and an accidental double-tap on the upload
    button harmless.
    """
    cur = conn.execute(
        "INSERT INTO receipts (sha256, path, mime, bytes, source, external_id,"
        " source_meta, ingested_at) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(sha256) DO NOTHING",
        (sha256, path, mime, bytes_, source, external_id,
         _json(source_meta) if source_meta else None, at or now()),
    )
    if cur.rowcount:
        return int(cur.lastrowid), True
    row = conn.execute("SELECT id FROM receipts WHERE sha256=?", (sha256,)).fetchone()
    return int(row["id"]), False


def insert_extraction(
    conn: sqlite3.Connection, *, receipt_id: int, status: str, model: str,
    prompt_version: str, render_mode: str, raw_response: str | None = None,
    payload: dict | None = None, note: str | None = None, latency_ms: int | None = None,
    at: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO extractions (receipt_id, status, model, prompt_version, render_mode,"
        " raw_response, payload, note, latency_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (receipt_id, status, model, prompt_version, render_mode, raw_response,
         _json(payload), note, latency_ms, at or now()),
    )
    return int(cur.lastrowid)


def insert_correction(
    conn: sqlite3.Connection, *, receipt_id: int, field: str, value: str | None,
    at: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO corrections (receipt_id, field, value, created_at) VALUES (?,?,?,?)",
        (receipt_id, field, value, at or now()),
    )
    return int(cur.lastrowid)


# --- reads ---------------------------------------------------------------------------------

def get_receipt(conn: sqlite3.Connection, receipt_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()


def receipt_ids(conn: sqlite3.Connection) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM receipts ORDER BY id")]


def extractions_for(conn: sqlite3.Connection, receipt_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM extractions WHERE receipt_id=? ORDER BY created_at, id", (receipt_id,)))


def corrections_for(conn: sqlite3.Connection, receipt_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM corrections WHERE receipt_id=? ORDER BY created_at, id", (receipt_id,)))


def pending_receipt_ids(conn: sqlite3.Connection) -> list[int]:
    """Receipts with no successful extraction, oldest first — the worker's queue.

    Deliberately derived rather than stored. A queue table would be a fourth thing that
    can disagree with the other three; this cannot go stale, and re-running `extract`
    after fixing whatever was broken picks up exactly the receipts that still need it.
    """
    return [r["id"] for r in conn.execute(
        "SELECT r.id FROM receipts r"
        " WHERE NOT EXISTS (SELECT 1 FROM extractions e"
        "                   WHERE e.receipt_id = r.id AND e.status = 'ok')"
        " ORDER BY r.id")]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "receipts": q("SELECT COUNT(*) FROM receipts"),
        "extractions": q("SELECT COUNT(*) FROM extractions"),
        "corrections": q("SELECT COUNT(*) FROM corrections"),
        "transactions": q("SELECT COUNT(*) FROM transactions WHERE deleted=0"),
        "backlog": len(pending_receipt_ids(conn)),
    }


def last_extraction_error(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The most recent failure, for pages that must say why they are empty."""
    return conn.execute(
        "SELECT * FROM extractions WHERE status != 'ok' ORDER BY created_at DESC, id DESC"
        " LIMIT 1").fetchone()


# --- meta ------------------------------------------------------------------------------

def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# --- projection writes -------------------------------------------------------------------

def write_projection(conn: sqlite3.Connection, txn: dict[str, Any], items: list[dict]) -> None:
    """Replace one receipt's projected rows. Only ever called by the projector."""
    rid = txn["receipt_id"]
    conn.execute("DELETE FROM transactions WHERE receipt_id=?", (rid,))
    conn.execute("DELETE FROM line_items WHERE receipt_id=?", (rid,))
    cols = ", ".join(txn)
    conn.execute(f"INSERT INTO transactions ({cols}) VALUES ({', '.join('?' * len(txn))})",
                 tuple(txn.values()))
    for n, item in enumerate(items, start=1):
        conn.execute(
            "INSERT INTO line_items (receipt_id, line_no, description, qty, unit_cents,"
            " total_cents) VALUES (?,?,?,?,?,?)",
            (rid, n, item["description"], item.get("qty"), item.get("unit_cents"),
             item.get("total_cents")))


def clear_projections(conn: sqlite3.Connection) -> None:
    for t in PROJECTION_TABLES:
        conn.execute(f"DELETE FROM {t}")


# --- truth: the feeds, append only ---------------------------------------------------------
#
# Same rule as above and the same test proves it. Every insert here is idempotent against a
# unique index rather than against a rowid, because these rows are replayed from a log whose
# order is reconstructed rather than remembered: re-running a poll, or replaying the same
# sealed event twice, has to be a no-op that reports the row already there.


def _insert_unique(conn: sqlite3.Connection, table: str, row: dict) -> tuple[int | None, bool]:
    """Append one row, or find the one already there. Returns (id, is_new).

    The id is None when the conflict was on some *other* unique index than `uid`, which is
    not a bug and has one real case: `ux_feed_polls_file` means re-importing the same export
    is refused by its bytes, and the second attempt is a different event with a different
    uid. The caller wanted "this file is already in", and that is what a None says.
    """
    cols = ", ".join(row)
    cur = conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})"
        f" ON CONFLICT DO NOTHING",
        tuple(row.values()),
    )
    if cur.rowcount:
        return int(cur.lastrowid), True
    got = conn.execute(f"SELECT id FROM {table} WHERE uid=?", (row["uid"],)).fetchone()
    return (int(got["id"]), False) if got else (None, False)


def insert_feed_poll(conn: sqlite3.Connection, *, uid: str, source: str, outcome: str,
                     started_at: str, finished_at: str, window_from: str | None = None,
                     window_to: str | None = None, http_status: int | None = None,
                     file_sha256: str | None = None, file_name: str | None = None,
                     accounts_seen: int = 0, records_seen: int = 0, records_new: int = 0,
                     errors: list | None = None, note: str | None = None,
                     seen_ids: dict | None = None, detail: dict | None = None
                     ) -> tuple[int, bool]:
    return _insert_unique(conn, "feed_polls", {
        "uid": uid, "source": source, "outcome": outcome, "started_at": started_at,
        "finished_at": finished_at, "window_from": window_from, "window_to": window_to,
        "http_status": http_status, "file_sha256": file_sha256, "file_name": file_name,
        "accounts_seen": accounts_seen, "records_seen": records_seen,
        "records_new": records_new,
        "errors": _json(errors) if errors else None, "note": note,
        "seen_ids": _json(seen_ids) if seen_ids else None,
        "detail": _json(detail) if detail else None,
    })


def insert_feed_account(conn: sqlite3.Connection, *, uid: str, poll_uid: str, source: str,
                        native_id: str, content_sha256: str, raw: dict, observed_at: str,
                        org_name: str | None = None, org_domain: str | None = None,
                        org_id: str | None = None, name: str | None = None,
                        currency: str = "USD", balance_cents: int | None = None,
                        available_balance_cents: int | None = None,
                        balance_at: str | None = None) -> tuple[int, bool]:
    return _insert_unique(conn, "feed_accounts", {
        "uid": uid, "poll_uid": poll_uid, "source": source, "native_id": native_id,
        "org_name": org_name, "org_domain": org_domain, "org_id": org_id, "name": name,
        "currency": currency, "balance_cents": balance_cents,
        "available_balance_cents": available_balance_cents, "balance_at": balance_at,
        "content_sha256": content_sha256, "raw": _json(raw), "observed_at": observed_at,
    })


def insert_feed_record(conn: sqlite3.Connection, *, uid: str, poll_uid: str, source: str,
                       native_account: str, external_id: str, content_sha256: str,
                       raw: dict, observed_at: str, posted_at: str | None = None,
                       transacted_at: str | None = None, pending: bool = False,
                       amount_cents: int | None = None, currency: str = "USD",
                       description: str = "", payee: str | None = None,
                       memo: str | None = None, flow_hint: str | None = None,
                       reject_reason: str | None = None) -> tuple[int, bool]:
    return _insert_unique(conn, "feed_records", {
        "uid": uid, "poll_uid": poll_uid, "source": source,
        "native_account": native_account, "external_id": external_id,
        "posted_at": posted_at, "transacted_at": transacted_at, "pending": int(pending),
        "amount_cents": amount_cents, "currency": currency, "description": description,
        "payee": payee, "memo": memo, "flow_hint": flow_hint,
        "reject_reason": reject_reason,
        "content_sha256": content_sha256, "raw": _json(raw), "observed_at": observed_at,
    })


def insert_feed_correction(conn: sqlite3.Connection, *, uid: str, txn_key: str, field: str,
                           value: str | None, created_at: str | None = None
                           ) -> tuple[int, bool]:
    return _insert_unique(conn, "feed_corrections", {
        "uid": uid, "txn_key": txn_key, "field": field, "value": value,
        "created_at": created_at or now(),
    })


# --- feed reads ----------------------------------------------------------------------------

def feed_records_grouped(conn: sqlite3.Connection) -> Iterator[list[sqlite3.Row]]:
    """Every observation of one external transaction, oldest first, one group at a time.

    Grouped in SQL rather than in Python because this is the projector's whole input and it
    is the one query in this module that touches every feed row.
    """
    group: list[sqlite3.Row] = []
    key: tuple | None = None
    for r in conn.execute(
        "SELECT * FROM feed_records"
        " ORDER BY source, native_account, external_id, observed_at, id"
    ):
        k = (r["source"], r["native_account"], r["external_id"])
        if key is not None and k != key:
            yield group
            group = []
        key = k
        group.append(r)
    if group:
        yield group


def feed_corrections_all(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    out: dict[str, list[sqlite3.Row]] = {}
    for r in conn.execute(
            "SELECT * FROM feed_corrections ORDER BY txn_key, created_at, id"):
        out.setdefault(r["txn_key"], []).append(r)
    return out


def feed_account_latest(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The newest observation of each native account."""
    return list(conn.execute(
        "SELECT * FROM feed_accounts WHERE id IN ("
        "  SELECT MAX(id) FROM feed_accounts GROUP BY source, native_id)"
        " ORDER BY source, native_id"))


def feed_polls_recent(conn: sqlite3.Connection, source: str | None = None,
                      limit: int = 20) -> list[sqlite3.Row]:
    if source:
        return list(conn.execute(
            "SELECT * FROM feed_polls WHERE source=? ORDER BY started_at DESC, id DESC"
            " LIMIT ?", (source, limit)))
    return list(conn.execute(
        "SELECT * FROM feed_polls ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)))


def feed_polls_all(conn: sqlite3.Connection, source: str | None = None) -> list[sqlite3.Row]:
    """The whole poll history. `plan_window` is pure and takes this rather than a cursor."""
    if source:
        return list(conn.execute(
            "SELECT * FROM feed_polls WHERE source=? ORDER BY started_at, id", (source,)))
    return list(conn.execute("SELECT * FROM feed_polls ORDER BY started_at, id"))


def feed_poll_for_file(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    """Has this exact file already been imported? The drop adapter's claim check."""
    return conn.execute(
        "SELECT * FROM feed_polls WHERE file_sha256=?", (sha256,)).fetchone()


def feed_counts(conn: sqlite3.Connection) -> dict[str, int]:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "polls": q("SELECT COUNT(*) FROM feed_polls"),
        "records": q("SELECT COUNT(*) FROM feed_records"),
        "transactions": q("SELECT COUNT(*) FROM feed_transactions WHERE deleted=0"),
        "pending": q("SELECT COUNT(*) FROM feed_transactions WHERE pending=1 AND deleted=0"),
        "needs_review": q("SELECT COUNT(*) FROM feed_transactions"
                          " WHERE status='needs_review' AND deleted=0"),
        "duplicate_suspect": q("SELECT COUNT(*) FROM feed_transactions"
                               " WHERE duplicate_suspect=1 AND deleted=0"),
        "accounts": q("SELECT COUNT(*) FROM accounts"),
        "unmapped": q("SELECT COUNT(*) FROM accounts WHERE unmapped=1"),
    }


# --- feed projection writes ----------------------------------------------------------------

def write_feed_transactions(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """Replace the whole feed projection. Only ever called by the projector."""
    conn.execute("DELETE FROM feed_transactions")
    for row in rows:
        cols = ", ".join(row)
        conn.execute(
            f"INSERT INTO feed_transactions ({cols})"
            f" VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


def write_accounts(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.execute("DELETE FROM accounts")
    for row in rows:
        cols = ", ".join(row)
        conn.execute(
            f"INSERT INTO accounts ({cols}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()))
