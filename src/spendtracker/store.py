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

from spendtracker import paths

# Inside the package, not at the repo root: a wheel installed anywhere still carries its
# own schema, so `migrate()` cannot silently find nothing and leave an empty database
# looking like a working one.
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

PROJECTION_TABLES = ("transactions", "line_items")


def now() -> str:
    """UTC, ISO 8601, second precision. Stored times are instants; display converts."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


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
         json.dumps(source_meta) if source_meta else None, now()),
    )
    if cur.rowcount:
        return int(cur.lastrowid), True
    row = conn.execute("SELECT id FROM receipts WHERE sha256=?", (sha256,)).fetchone()
    return int(row["id"]), False


def insert_extraction(
    conn: sqlite3.Connection, *, receipt_id: int, status: str, model: str,
    prompt_version: str, render_mode: str, raw_response: str | None = None,
    payload: dict | None = None, note: str | None = None, latency_ms: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO extractions (receipt_id, status, model, prompt_version, render_mode,"
        " raw_response, payload, note, latency_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (receipt_id, status, model, prompt_version, render_mode, raw_response,
         json.dumps(payload) if payload is not None else None, note, latency_ms, now()),
    )
    return int(cur.lastrowid)


def insert_correction(
    conn: sqlite3.Connection, *, receipt_id: int, field: str, value: str | None
) -> int:
    cur = conn.execute(
        "INSERT INTO corrections (receipt_id, field, value, created_at) VALUES (?,?,?,?)",
        (receipt_id, field, value, now()),
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
