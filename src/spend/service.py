"""The impure middle: the operations both the CLI and the web app perform.

Everything here is a short script over the pure modules — hash a file and record it,
render and extract one receipt, recompute a projection. Keeping them in one place is what
lets `spend extract` and the background worker be the same code path, so a receipt
fixed from the terminal and one fixed from the phone cannot diverge.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from spend import config, keys, paths, render, replay, seal, store
from spend.extract.base import Extraction, Extractor
from spend.project import Rules, project

log = logging.getLogger(__name__)

# What a phone or a mail client actually sends. Anything else is refused at the door
# rather than stored and discovered to be unreadable an hour later by the worker.
EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/heic": ".heic", "image/heif": ".heif",
}
# PDF is deliberately absent. `render` cannot rasterise one, so accepting a PDF would mean
# storing a file the worker is guaranteed to fail on — a receipt that looks ingested and
# never becomes a row. Refusing it at the door is the honest answer until a renderer
# exists. Emailed PDF receipts are the reason to add one; that path is not built yet.


class IngestError(ValueError):
    pass


def ingest_bytes(
    conn: sqlite3.Connection, data: bytes, *, mime: str, source: str = "upload",
    external_id: str | None = None, source_meta: dict | None = None,
) -> tuple[int, bool]:
    """Seal a receipt's bytes and record it. Returns (receipt_id, is_new).

    The blob is sealed before the event is appended, and the event is appended before the
    cache row is written. A crash anywhere in that chain leaves something inert -- an
    orphaned sealed blob, or an event the next unlock picks up -- never a row pointing at
    nothing, which every later read would have to defend against.

    Both writes are content-addressed and both are idempotent, so re-uploading the same
    photograph touches no file at all.
    """
    mime = (mime or "").split(";")[0].strip().lower()
    if mime not in EXTENSIONS:
        raise IngestError(f"unsupported type {mime!r}")
    if not data:
        raise IngestError("empty file")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise IngestError(f"file is {len(data) // 1024} KiB; the limit is "
                          f"{config.MAX_UPLOAD_BYTES // 1024} KiB")

    import hashlib
    sha = hashlib.sha256(data).hexdigest()
    dest = paths.blob_path(sha)
    seal.seal_to(dest, data)

    known = conn.execute("SELECT 1 FROM receipts WHERE sha256=?", (sha,)).fetchone()
    rid = replay.record(conn, "receipt", {
        "sha256": sha,
        "blob": str(dest.relative_to(paths.blobs_dir())),
        "mime": mime,
        "bytes": len(data),
        "source": source,
        "external_id": external_id,
        "source_meta": source_meta,
    })
    is_new = known is None
    if is_new:
        reproject(conn, rid)
    return rid, is_new


def ingest_file(conn: sqlite3.Connection, path: Path, **kw) -> tuple[int, bool]:
    import mimetypes
    mime = kw.pop("mime", None) or mimetypes.guess_type(path.name)[0] or ""
    if path.suffix.lower() in (".heic", ".heif"):
        mime = "image/heic"
    return ingest_bytes(conn, path.read_bytes(), mime=mime, **kw)


def blob_bytes(row: sqlite3.Row) -> bytes:
    """One receipt's original bytes, decrypted into memory.

    Per request, never in bulk, and never written back out plaintext: the runtime tmpfs is
    measured in hundreds of megabytes and a year of phone photographs is not. A streaming
    `decrypt_io` through a pipe would be the memory-optimal shape and is what to reach for
    the day something larger than a 25 MiB upload arrives; one phone and one photograph at a
    time does not justify the thread.
    """
    return seal.open_file(paths.blob_path(row["sha256"]), keys.require_identity())


async def extract_one(
    conn: sqlite3.Connection, receipt_id: int, extractor: Extractor
) -> Extraction:
    """Render one receipt, read it, record the attempt, and reproject.

    Every outcome is recorded, including the failures. The row that says a model timed out
    is the difference between a receipt that is waiting and a receipt that was forgotten,
    and it is what `doctor` and the list page read to explain themselves.
    """
    row = store.get_receipt(conn, receipt_id)
    if row is None:
        raise LookupError(f"no receipt {receipt_id}")

    mode = config.RENDER_MODE
    try:
        doc = render.render(blob_bytes(row), row["sha256"], mode)
    except (render.RenderError, seal.SealError, OSError) as exc:
        result = Extraction(status="error", note=f"render: {exc}")
        replay.record(conn, "extraction", {
            "receipt": row["sha256"], "status": result.status,
            "model": getattr(extractor, "model", config.MODEL),
            "prompt_version": getattr(extractor, "prompt_version", "?"),
            "render_mode": mode, "raw_response": None, "payload": None,
            "note": result.note, "latency_ms": None})
        conn.commit()
        reproject(conn, receipt_id)
        return result

    result = await extractor.extract(doc)
    replay.record(conn, "extraction", {
        "receipt": row["sha256"], "status": result.status,
        "model": getattr(extractor, "model", config.MODEL),
        "prompt_version": getattr(extractor, "prompt_version", "?"),
        "render_mode": doc.mode, "raw_response": result.raw_response,
        "payload": result.data.model_dump(mode="json") if result.data else None,
        "note": result.note, "latency_ms": result.latency_ms})
    conn.commit()
    reproject(conn, receipt_id)
    return result


# --- projection ----------------------------------------------------------------------

def reproject(conn: sqlite3.Connection, receipt_id: int, rules: Rules | None = None) -> None:
    rules = rules or Rules.load()
    txn, items = project(
        receipt_id,
        [dict(r) for r in store.extractions_for(conn, receipt_id)],
        [dict(r) for r in store.corrections_for(conn, receipt_id)],
        rules,
    )
    store.write_projection(conn, txn, items)
    conn.commit()


def rebuild(conn: sqlite3.Connection) -> int:
    """Drop every projected row and derive them all again.

    Safe by construction: the projections hold nothing that is not recomputable from the
    three append-only tables, so this cannot lose anything. It is the repair procedure for
    a projection bug and the apply step for an edited rules file.
    """
    rules = Rules.load()
    store.clear_projections(conn)
    ids = store.receipt_ids(conn)
    for rid in ids:
        txn, items = project(
            rid,
            [dict(r) for r in store.extractions_for(conn, rid)],
            [dict(r) for r in store.corrections_for(conn, rid)],
            rules,
        )
        store.write_projection(conn, txn, items)
    conn.commit()
    # Both projections, always. `clear_projections` empties the feed tables too, so a rebuild
    # that only re-derived receipts would leave the ledger looking like an empty year.
    reproject_feeds(conn)
    return len(ids)


def correct(conn: sqlite3.Connection, receipt_id: int, changes: dict[str, str | None]) -> int:
    """Append edits and reproject. Returns how many were recorded.

    A value equal to what the projection already shows is dropped rather than appended:
    submitting an unchanged form should not grow the correction log.
    """
    row = store.get_receipt(conn, receipt_id)
    if row is None:
        raise LookupError(f"no receipt {receipt_id}")
    sha = row["sha256"]
    current = conn.execute(
        "SELECT * FROM transactions WHERE receipt_id=?", (receipt_id,)).fetchone()
    written = 0
    for field, value in changes.items():
        if current is not None and _unchanged(current, field, value):
            continue
        replay.record(conn, "correction", {
            "receipt": sha, "field": field,
            "value": value if value not in ("", None) else None})
        written += 1
    if written:
        conn.commit()
        reproject(conn, receipt_id)
    return written


def _unchanged(row: sqlite3.Row, field: str, value: str | None) -> bool:
    from spend.money import MoneyError, to_cents
    if field.startswith("item."):
        return False
    if field in ("subtotal", "tax", "tip", "total"):
        try:
            return to_cents(value) == row[f"{field}_cents"]
        except MoneyError:
            return False
    if field not in row.keys():
        return False
    have = row[field]
    return (have or None) == (value or None)


def summary(conn: sqlite3.Connection) -> dict:
    """What `doctor` and the pages print about the health of the whole thing."""
    c = store.counts(conn)
    err = store.last_extraction_error(conn)
    c["last_error"] = (
        {"at": err["created_at"], "status": err["status"], "note": err["note"]}
        if err else None)
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(total_cents),0) s FROM transactions"
        " WHERE deleted=0 AND status IN ('ok','needs_review')").fetchone()
    c["counted"], c["total_cents"] = row["n"], row["s"]
    c["needs_review"] = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE deleted=0 AND status='needs_review'"
    ).fetchone()[0]
    return c


# --- feeds ---------------------------------------------------------------------------------

def record_poll(conn: sqlite3.Connection | None, poll) -> dict:
    """Seal one poll's outcome and everything it saw. Returns a small summary.

    `conn` may be None, and that is the whole point of the design rather than a convenience.
    Sealing needs only the age recipients -- public keys -- so a sync running while the store
    is locked appends everything it pulled and writes no cache at all; the next `spend
    unlock` replays it. Pass a connection when one is open and the cache is kept in step as
    the events land.

    Every attempt is recorded, including the ones that returned nothing, and the source's own
    error list is recorded verbatim rather than raised. That is not tidiness: an empty
    transaction list from a feed that has been failing for two days is indistinguishable from
    a quiet week, and this row is the only thing that can tell them apart. `docs/email-ingest.md`
    already names the same rule for the mail path; it is the same mistake with a different feed.
    """
    from spend import feed, ledger      # noqa: PLC0415 - keeps the import graph acyclic

    recipients = seal.load_recipients()
    started = poll.detail.get("started_at") or ledger.now()

    def append(kind: str, body: dict, aside: dict | None = None) -> tuple:
        rec, is_new = ledger.append(kind, body, aside=aside, recipients=recipients)
        if conn is not None:
            replay.apply(conn, rec)
        return rec, is_new

    # `records_new` is deliberately absent from the body. It is not known until the child
    # records below have been sealed, and adding it afterwards would mean an UPDATE against
    # a truth table -- the one thing this schema forbids. It is derived from the log instead.
    poll_rec, _ = append("feed_poll", {
        "source": poll.source, "outcome": poll.outcome,
        "started_at": started, "finished_at": ledger.now(),
        "window_from": poll.window.start.isoformat() if poll.window else None,
        "window_to": poll.window.end.isoformat() if poll.window else None,
        "http_status": poll.http_status,
        "file_sha256": poll.file_sha256, "file_name": poll.file_name,
        "accounts_seen": len(poll.accounts), "records_seen": len(poll.records),
        "errors": [vars(e) for e in poll.errors] or None,
        "note": poll.note,
        "seen_ids": poll.seen_ids() or None,
        "detail": {k: v for k, v in poll.detail.items() if k != "started_at"} or None,
    })

    for account in poll.accounts:
        # `poll_uid` travels in `aside`: it says which poll saw this balance, which is a
        # fact about the observation and not about the balance. In the digest it would make
        # every nightly poll re-seal every account it looked at.
        append("feed_account", {
            "source": poll.source,
            "native_id": account.native_id, "org_name": account.org_name,
            "org_domain": account.org_domain, "org_id": account.org_id,
            "name": account.name, "currency": account.currency,
            "balance_cents": account.balance_cents,
            "available_balance_cents": account.available_balance_cents,
            "balance_at": account.balance_at,
            "content_sha256": feed.content_sha_account(account), "raw": dict(account.raw),
        }, {"poll_uid": poll_rec.sha})

    # Counted from the log, not from the database, so the number is the same whether or not
    # a cache happened to be open. A re-observed transaction whose content is unchanged
    # hashes to a file that already exists, and is not new.
    new = 0
    for record in poll.records:
        _, is_new = append("feed_record", {
            "source": poll.source,
            "native_account": record.native_account, "external_id": record.external_id,
            "posted_at": record.posted_at, "transacted_at": record.transacted_at,
            "pending": bool(record.pending), "amount_cents": record.amount_cents,
            "currency": record.currency, "description": record.description,
            "payee": record.payee, "memo": record.memo, "flow_hint": record.flow_hint,
            "reject_reason": record.reject_reason,
            "content_sha256": feed.content_sha(record), "raw": dict(record.raw),
        }, {"poll_uid": poll_rec.sha})
        new += is_new

    if conn is not None:
        conn.commit()
    return {"poll_uid": poll_rec.sha, "records": len(poll.records), "new": new,
            "outcome": poll.outcome, "errors": len(poll.errors),
            "locked": conn is None}


def reproject_feeds(conn: sqlite3.Connection, ctx=None) -> int:
    """Recompute every feed transaction and account row from the observations."""
    from spend import feed      # noqa: PLC0415
    ctx = ctx or feed.Context.load()
    rows = feed.project_all(list(store.feed_records_grouped(conn)),
                            store.feed_corrections_all(conn), ctx)
    store.write_feed_transactions(conn, rows)
    store.write_accounts(conn, feed.account_rows(store.feed_account_latest(conn), rows, ctx))
    conn.commit()
    return len(rows)


def correct_feed(conn: sqlite3.Connection, txn_key: str,
                 changes: dict[str, str | None]) -> int:
    """Append edits to a feed transaction and reproject. Same shape as `correct`."""
    from spend import feed      # noqa: PLC0415
    written = 0
    for field, value in changes.items():
        if field not in feed.CORRECTABLE:
            continue
        replay.record(conn, "feed_correction", {
            "txn_key": txn_key, "field": field,
            "value": value if value not in ("", None) else None})
        written += 1
    if written:
        conn.commit()
        reproject_feeds(conn)
    return written
