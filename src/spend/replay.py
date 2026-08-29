"""The cache is built from the log; the log is never built from the cache.

The rule this module exists to enforce is that the database holds nothing that is not in the
log, and that replay is total and deterministic -- the same set of sealed files always
produces the same rows, including the integer ids.

`record()` is the structural keystone of the whole design. Every live write goes through it,
and `run()` goes through the same `apply()` for the same records, so there is exactly one
function that knows how a record maps onto rows. "Replay reproduces the cache" is then true
by construction rather than by two implementations kept in step by review.

The order is append-then-apply, never the reverse. A crash between the two leaves an event in
the log that the cache has not seen yet, and the next unlock picks it up; the other order
would lose it. It is the same argument `service.ingest_bytes` makes about writing the file
before the row, one layer down.

## Natural keys, never rowids

An extraction record names its receipt by the receipt's sha256, not by `receipts.id`. It has
to: an event written by one process cannot know what rowid another process's replay will
assign, and while the store is locked the sync timer cannot read the cache at all. Integer
ids exist only inside the cache, and replay assigns them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from spend import ledger, paths, store


@dataclass
class Stats:
    events: int = 0
    receipts: int = 0
    extractions: int = 0
    corrections: int = 0
    feed_records: int = 0
    orphans: list[str] = field(default_factory=list)
    bad: list[tuple[Path, str]] = field(default_factory=list)

    def line(self) -> str:
        parts = [f"{self.events} events", f"{self.receipts} receipts",
                 f"{self.feed_records} feed records"]
        if self.orphans:
            parts.append(f"{len(self.orphans)} orphaned")
        if self.bad:
            parts.append(f"{len(self.bad)} UNREADABLE")
        return ", ".join(parts)


# --- one record, one place -----------------------------------------------------------------

def _receipt_id(conn: sqlite3.Connection, sha: str, ids: dict[str, int]) -> int | None:
    if sha in ids:
        return ids[sha]
    row = conn.execute("SELECT id FROM receipts WHERE sha256=?", (sha,)).fetchone()
    if row is None:
        return None
    ids[sha] = int(row["id"])
    return ids[sha]


def apply(conn: sqlite3.Connection, rec: ledger.Record,
          ids: dict[str, int] | None = None) -> int | None:
    """Write one record's rows. The only mapping from an event to the cache."""
    ids = {} if ids is None else ids
    b = rec.body

    if rec.kind == "receipt":
        rid, _ = store.insert_receipt(
            conn, sha256=b["sha256"], path=b.get("blob") or "", mime=b.get("mime") or "",
            bytes_=b.get("bytes") or 0, source=b["source"],
            external_id=b.get("external_id"), source_meta=b.get("source_meta"),
            at=rec.at)
        ids[b["sha256"]] = rid
        return rid

    if rec.kind in ("extraction", "correction"):
        rid = _receipt_id(conn, b["receipt"], ids)
        if rid is None:
            return None                       # orphan; the caller records it
        if rec.kind == "extraction":
            return store.insert_extraction(
                conn, receipt_id=rid, status=b["status"], model=b["model"],
                prompt_version=b["prompt_version"], render_mode=b["render_mode"],
                raw_response=b.get("raw_response"), payload=b.get("payload"),
                note=b.get("note"), latency_ms=b.get("latency_ms"), at=rec.at)
        return store.insert_correction(
            conn, receipt_id=rid, field=b["field"], value=b.get("value"), at=rec.at)

    # The feed kinds. `uid` is the record's own digest, which is what makes these rows
    # survive the database being deleted: nothing here references a rowid.
    if rec.kind == "feed_poll":
        return store.insert_feed_poll(conn, uid=rec.sha, **b)[0]
    if rec.kind == "feed_account":
        return store.insert_feed_account(conn, uid=rec.sha, observed_at=rec.at, **b)[0]
    if rec.kind == "feed_record":
        return store.insert_feed_record(conn, uid=rec.sha, observed_at=rec.at, **b)[0]
    if rec.kind == "feed_correction":
        return store.insert_feed_correction(conn, uid=rec.sha, created_at=rec.at, **b)[0]

    raise ValueError(f"no rule for record kind {rec.kind!r}")


def record(conn: sqlite3.Connection, kind: str, body: dict, *,
           recipients: list | None = None) -> int | None:
    """Append to the log, then write the cache. The only write path in the application."""
    rec, _ = ledger.append(kind, body, recipients=recipients)
    return apply(conn, rec)


# --- the whole log -------------------------------------------------------------------------

def run(conn: sqlite3.Connection, identity, root: Path | None = None) -> Stats:
    """Rebuild the cache from every sealed event. The repair procedure and the unlock step."""
    from spend.service import rebuild                       # noqa: PLC0415 - cycle

    store.migrate(conn)
    good, bad = ledger.events(identity, root or paths.log_dir())
    stats = Stats(events=len(good), bad=bad)
    ids: dict[str, int] = {}

    # Two passes, not one. `datetime.now()` can step backwards under NTP, so an extraction
    # can legitimately sort ahead of the receipt it belongs to; a single pass would drop it
    # as an orphan and lose an answer the model already paid for. Both passes are over an
    # in-memory list, so the second one costs nothing.
    for rec in good:
        if rec.kind == "receipt":
            apply(conn, rec, ids)
            stats.receipts += 1

    for rec in good:
        if rec.kind == "receipt":
            continue
        if apply(conn, rec, ids) is None:
            stats.orphans.append(f"{rec.kind} {rec.sha[:12]} -> {rec.body.get('receipt')}")
        elif rec.kind == "extraction":
            stats.extractions += 1
        elif rec.kind == "correction":
            stats.corrections += 1
        elif rec.kind == "feed_record":
            stats.feed_records += 1

    conn.commit()
    rebuild(conn)
    return stats


def fresh(identity, root: Path | None = None) -> tuple[sqlite3.Connection, Stats]:
    """Delete the cache and rebuild it. What `spend unlock` calls."""
    db = paths.db_path()
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    conn = store.connect(db)
    return conn, run(conn, identity, root)
