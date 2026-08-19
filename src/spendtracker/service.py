"""The impure middle: the operations both the CLI and the web app perform.

Everything here is a short script over the pure modules — hash a file and record it,
render and extract one receipt, recompute a projection. Keeping them in one place is what
lets `spend-tracker extract` and the background worker be the same code path, so a receipt
fixed from the terminal and one fixed from the phone cannot diverge.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from spendtracker import config, paths, render, store
from spendtracker.extract.base import Extraction, Extractor
from spendtracker.project import Rules, project

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
    """Store a receipt's bytes and record it. Returns (receipt_id, is_new).

    The file is written before the row, and named by its own hash. A crash between the two
    leaves an orphaned file in the receipts directory, which is inert; the other order
    would leave a row pointing at nothing, which every later read has to defend against.
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
    dest = paths.receipt_path(sha, EXTENSIONS[mime])
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        tmp.rename(dest)  # atomic within a filesystem: no half-written receipt is visible

    rid, is_new = store.insert_receipt(
        conn, sha256=sha, path=str(dest.relative_to(paths.receipts_dir())), mime=mime,
        bytes_=len(data), source=source, external_id=external_id, source_meta=source_meta)
    if is_new:
        reproject(conn, rid)
    return rid, is_new


def ingest_file(conn: sqlite3.Connection, path: Path, **kw) -> tuple[int, bool]:
    import mimetypes
    mime = kw.pop("mime", None) or mimetypes.guess_type(path.name)[0] or ""
    if path.suffix.lower() in (".heic", ".heif"):
        mime = "image/heic"
    return ingest_bytes(conn, path.read_bytes(), mime=mime, **kw)


def absolute_path(row: sqlite3.Row) -> Path:
    return paths.receipts_dir() / row["path"]


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
        doc = render.render(absolute_path(row), row["sha256"], mode)
    except (render.RenderError, OSError) as exc:
        result = Extraction(status="error", note=f"render: {exc}")
        store.insert_extraction(
            conn, receipt_id=receipt_id, status=result.status,
            model=getattr(extractor, "model", config.MODEL),
            prompt_version=getattr(extractor, "prompt_version", "?"),
            render_mode=mode, note=result.note)
        conn.commit()
        reproject(conn, receipt_id)
        return result

    result = await extractor.extract(doc)
    store.insert_extraction(
        conn, receipt_id=receipt_id, status=result.status,
        model=getattr(extractor, "model", config.MODEL),
        prompt_version=getattr(extractor, "prompt_version", "?"),
        render_mode=doc.mode, raw_response=result.raw_response,
        payload=result.data.model_dump(mode="json") if result.data else None,
        note=result.note, latency_ms=result.latency_ms)
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
    return len(ids)


def correct(conn: sqlite3.Connection, receipt_id: int, changes: dict[str, str | None]) -> int:
    """Append edits and reproject. Returns how many were recorded.

    A value equal to what the projection already shows is dropped rather than appended:
    submitting an unchanged form should not grow the correction log.
    """
    current = conn.execute(
        "SELECT * FROM transactions WHERE receipt_id=?", (receipt_id,)).fetchone()
    written = 0
    for field, value in changes.items():
        if current is not None and _unchanged(current, field, value):
            continue
        store.insert_correction(conn, receipt_id=receipt_id, field=field,
                                value=value if value not in ("", None) else None)
        written += 1
    if written:
        conn.commit()
        reproject(conn, receipt_id)
    return written


def _unchanged(row: sqlite3.Row, field: str, value: str | None) -> bool:
    from spendtracker.money import MoneyError, to_cents
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
