"""The properties the whole design rests on.

If any of these break, the claims in the README stop being true: that a projection bug is
retroactively fixable, that a correction survives, and that a model failure costs you an
answer rather than giving you a wrong one.
"""

from __future__ import annotations

import re

import pytest

from spend import paths, seal, store
from spend.extract.base import Extraction
from spend.service import correct, extract_one, ingest_bytes, rebuild
from tests.conftest import FakeExtractor, ok


# Ordered by each table's own key, not by rowid. Physical row order is not something a
# rebuild owes anyone — reprojecting one receipt after an edit legitimately moves its rows
# to the end — and asserting on it would fail for a reason that means nothing.
_ORDER = {"transactions": "receipt_id", "line_items": "receipt_id, line_no"}


def snapshot(conn) -> dict:
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY {key}")]
            for t, key in _ORDER.items()}


async def seed(conn, jpeg_bytes, extractor=None, n: int = 1) -> list[int]:
    ids = []
    for i in range(n):
        # Vary the bytes so each is a distinct receipt rather than a dedupe.
        rid, _ = ingest_bytes(conn, jpeg_bytes + bytes([i]), mime="image/jpeg")
        ids.append(rid)
    if extractor:
        for rid in ids:
            await extract_one(conn, rid, extractor)
    return ids


# --- append-only ------------------------------------------------------------------------

TRUTH = ("receipts", "extractions", "corrections",
         "feed_polls", "feed_accounts", "feed_records", "feed_corrections")
_WRITE = re.compile(r"^\s*(UPDATE|DELETE\s+FROM)\s+[\"'`]?(\w+)", re.I)


async def test_nothing_ever_updates_or_deletes_a_truth_table(conn, jpeg_bytes):
    """Traced at the sqlite3 layer, so this holds for any statement any module issues —
    not just the ones a reviewer thought to look at."""
    violations: list[str] = []

    def trace(sql: str) -> None:
        if (m := _WRITE.match(sql)) and m.group(2) in TRUTH:
            violations.append(sql.strip())

    conn.set_trace_callback(trace)
    rid = (await seed(conn, jpeg_bytes, FakeExtractor(ok())))[0]
    correct(conn, rid, {"merchant": "CORRECTED"})
    await extract_one(conn, rid, FakeExtractor(ok(merchant="AGAIN")))
    rebuild(conn)
    conn.set_trace_callback(None)

    assert violations == []


async def test_re_extraction_appends_rather_than_replacing(conn, jpeg_bytes):
    rid = (await seed(conn, jpeg_bytes, FakeExtractor(ok(merchant="FIRST"))))[0]
    await extract_one(conn, rid, FakeExtractor(ok(merchant="SECOND")))
    rows = store.extractions_for(conn, rid)
    assert len(rows) == 2
    assert "FIRST" in rows[0]["raw_response"]      # the earlier answer is still on disk


# --- rebuild ------------------------------------------------------------------------------

async def test_rebuild_reproduces_the_projection_exactly(conn, jpeg_bytes):
    """The load-bearing test. Everything else rests on the projections holding nothing
    that is not recomputable from the log."""
    ids = await seed(conn, jpeg_bytes, FakeExtractor(ok(
        tax="1.00", total="11.00",
        line_items=[{"description": "A", "total": "5.00"},
                    {"description": "B", "total": "5.00"}])), n=3)
    correct(conn, ids[0], {"merchant": "EDITED"})
    before = snapshot(conn)

    rebuild(conn)

    assert snapshot(conn) == before


async def test_rebuild_from_nothing_lands_in_the_same_place(conn, jpeg_bytes):
    await seed(conn, jpeg_bytes, FakeExtractor(ok()), n=2)
    before = snapshot(conn)
    store.clear_projections(conn)
    assert snapshot(conn)["transactions"] == []
    rebuild(conn)
    assert snapshot(conn) == before


async def test_editing_the_rules_recategorises_history(conn, jpeg_bytes, monkeypatch, tmp_path):
    rid = (await seed(conn, jpeg_bytes, FakeExtractor(ok(merchant="NONESUCH CO"))))[0]
    assert conn.execute("SELECT category FROM transactions WHERE receipt_id=?",
                        (rid,)).fetchone()[0] is None

    rules = tmp_path / "categories.toml"
    rules.write_text('[household]\npatterns = ["nonesuch"]\n')
    monkeypatch.setattr("spend.project.RULES_PATH", rules)

    rebuild(conn)
    assert conn.execute("SELECT category FROM transactions WHERE receipt_id=?",
                        (rid,)).fetchone()[0] == "household"


# --- failure is absence ---------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    Extraction(status="error", note="JobFailed: backend died"),
    Extraction(status="error", note="RequestTimeout: deadline passed"),
    Extraction(status="invalid", raw_response="I'm sorry, I can't read that.",
               note="not JSON: line 1"),
    Extraction(status="invalid", raw_response="", note="model returned an empty response"),
])
async def test_a_model_that_fails_costs_an_answer_and_never_invents_one(conn, jpeg_bytes, bad):
    rid = (await seed(conn, jpeg_bytes, FakeExtractor(bad)))[0]
    txn = conn.execute("SELECT * FROM transactions WHERE receipt_id=?", (rid,)).fetchone()
    assert txn["status"] == "failed"
    assert txn["total_cents"] is None
    assert txn["merchant"] is None
    # The attempt is recorded, so the receipt is waiting rather than forgotten.
    assert len(store.extractions_for(conn, rid)) == 1
    assert rid in store.pending_receipt_ids(conn)


async def test_a_failed_receipt_stays_queued_and_succeeds_on_a_later_run(conn, jpeg_bytes):
    rid = (await seed(conn, jpeg_bytes,
                      FakeExtractor(Extraction(status="error", note="sir was down"))))[0]
    assert store.pending_receipt_ids(conn) == [rid]

    await extract_one(conn, rid, FakeExtractor(ok(merchant="LATER")))
    assert store.pending_receipt_ids(conn) == []
    assert conn.execute("SELECT merchant FROM transactions WHERE receipt_id=?",
                        (rid,)).fetchone()[0] == "LATER"


async def test_an_unreadable_file_fails_the_receipt_rather_than_the_worker(conn):
    rid, _ = ingest_bytes(conn, b"\xff\xd8\xff" + b"not really a jpeg" * 10,
                          mime="image/jpeg")
    result = await extract_one(conn, rid, FakeExtractor(ok()))
    assert result.status == "error"
    assert "render" in result.note
    assert conn.execute("SELECT status FROM transactions WHERE receipt_id=?",
                        (rid,)).fetchone()[0] == "failed"


# --- ingest ---------------------------------------------------------------------------------

def test_the_same_bytes_twice_is_one_receipt(conn, jpeg_bytes):
    a, new_a = ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    b, new_b = ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    assert (a, new_a, b, new_b) == (a, True, a, False)
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_a_receipt_is_sealed_under_its_own_hash(conn, jpeg_bytes):
    import hashlib

    from spend.service import blob_bytes
    rid, _ = ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    row = store.get_receipt(conn, rid)
    sha = hashlib.sha256(jpeg_bytes).hexdigest()
    assert row["sha256"] == sha
    # The name is the hash of the plaintext; the contents are not the plaintext.
    assert paths.blob_path(sha).read_bytes().startswith(seal.MAGIC)
    assert blob_bytes(row) == jpeg_bytes


@pytest.mark.parametrize("mime", ["application/pdf", "text/html", "", "application/zip"])
def test_a_type_the_renderer_cannot_read_is_refused_at_the_door(conn, jpeg_bytes, mime):
    """Storing it would mean a receipt that looks ingested and can never become a row."""
    from spend.service import IngestError
    with pytest.raises(IngestError):
        ingest_bytes(conn, jpeg_bytes, mime=mime)


def test_an_oversized_upload_is_refused(conn, jpeg_bytes, monkeypatch):
    from spend.service import IngestError
    monkeypatch.setattr("spend.config.MAX_UPLOAD_BYTES", 10)
    with pytest.raises(IngestError, match="limit"):
        ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")


def test_an_unchanged_correction_does_not_grow_the_log(conn, jpeg_bytes):
    rid, _ = ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    correct(conn, rid, {"merchant": "SAME"})
    assert correct(conn, rid, {"merchant": "SAME"}) == 0
    assert len(store.corrections_for(conn, rid)) == 1
