"""The directory an agent reads.

The first test in this file is the one that matters. Everything else here is in service of
it: the two streams describe overlapping reality, they are deliberately not deduplicated, and
the failure mode is an agent confidently reporting nearly twice the real number in one shot.
The defence is arithmetic, not documentation.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from spend import service, store
from spend.agent import workspace
from spend.sources import drop
from tests.conftest import FakeExtractor, capitalone_csv, checking_csv, ok


async def populated(conn, jpeg_bytes, drop_file):
    """A ledger with both streams, overlapping on purpose: the same Trader Joe's purchase
    arrives as a photograph and as a statement line."""
    service.record_poll(conn, drop.read_file(drop_file("c1.csv", capitalone_csv(
        "2026-08-14,2026-08-16,7734,SQ *BLUE BOTTLE COFFEE   BROOKLYN NY,Dining,4.75,",
        "2026-08-15,2026-08-16,7734,TRADER JOES #546 BROOKLYN NY,Grocery,47.03,",
        "2026-08-18,2026-08-19,7734,SPOTIFY USA,Entertainment,11.99,",
        "2026-08-20,2026-08-21,7734,PAYMENT THANK YOU - WEB,Payment,,1204.11"))))
    service.record_poll(conn, drop.read_file(drop_file("chk.csv", checking_csv(
        "4471,2026-08-20,1204.11,Debit,CAPITAL ONE AUTOPAY PYMT,4102.55",
        "4471,2026-08-15,3200.00,Credit,PAYROLL ACME DIR DEP,5306.66"))))

    from spend.service import correct, extract_one, ingest_bytes
    rid, _ = ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    await extract_one(conn, rid, FakeExtractor(ok(
        merchant="TRADER JOE'S #546", purchased_on="2026-08-15", total="47.03",
        line_items=[{"description": "OAT MILK", "total": "5.00"}])))
    correct(conn, rid, {"category": "groceries"})
    service.rebuild(conn)
    return conn


def rows_in(root: Path) -> list[dict]:
    return [json.loads(line)
            for f in sorted(root.rglob("*.jsonl"))
            for line in f.read_text().splitlines()]


# --- the one that matters ----------------------------------------------------------------

async def test_summing_every_row_in_the_directory_gives_the_bank_total(
        conn, jpeg_bytes, drop_file, accounts_toml, tmp_path):
    """`cat` the whole workspace, sum with no filter, and still be right.

    The Trader Joe's purchase is in here twice -- once photographed, once from the statement
    -- and the card payment is in here twice as well, one leg per account. A naive total
    would be wrong by both. This is the property that makes that impossible rather than
    merely discouraged.
    """
    await populated(conn, jpeg_bytes, drop_file)
    dest = tmp_path / "agent"
    workspace.build(conn, dest)

    everything = rows_in(dest)
    assert sum(r["net_spend_cents"] for r in everything) == 475 + 4703 + 1199

    receipts = [r for r in everything if r["stream"] == "receipt"]
    assert receipts and all(r["net_spend_cents"] == 0 for r in receipts)
    assert any(r["total_cents"] == 4703 for r in receipts), "the detail is still there"


async def test_the_ledger_refuses_a_receipt_row_that_would_count(
        conn, jpeg_bytes, drop_file, accounts_toml, tmp_path):
    """The CHECK constraint, so the property cannot quietly stop being true."""
    await populated(conn, jpeg_bytes, drop_file)
    dest = tmp_path / "agent"
    workspace.build(conn, dest)
    db = sqlite3.connect(dest / "ledger.db")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE receipts SET net_spend_cents = 4703")
    db.close()


async def test_the_spending_view_agrees_with_the_files(
        conn, jpeg_bytes, drop_file, accounts_toml, tmp_path):
    await populated(conn, jpeg_bytes, drop_file)
    dest = tmp_path / "agent"
    workspace.build(conn, dest)
    db = sqlite3.connect(dest / "ledger.db")
    from_sql = db.execute("SELECT SUM(net_spend_cents) FROM spending").fetchone()[0]
    db.close()
    assert from_sql == sum(r["net_spend_cents"] for r in rows_in(dest))


# --- shape --------------------------------------------------------------------------------

async def test_a_build_is_byte_identical_when_nothing_changed(
        conn, jpeg_bytes, drop_file, accounts_toml, tmp_path):
    """So "what changed since this morning" is a diff, not a feature somebody builds."""
    await populated(conn, jpeg_bytes, drop_file)
    a, b = tmp_path / "a", tmp_path / "b"
    workspace.build(conn, a)
    workspace.build(conn, b)
    for f in sorted(a.rglob("*.jsonl")):
        assert f.read_bytes() == (b / f.relative_to(a)).read_bytes()


async def test_rows_are_partitioned_by_local_month(
        conn, jpeg_bytes, drop_file, accounts_toml, tmp_path):
    await populated(conn, jpeg_bytes, drop_file)
    dest = tmp_path / "agent"
    workspace.build(conn, dest)
    assert (dest / "bank" / "2026" / "2026-08.jsonl").exists()
    for f in (dest / "bank").rglob("*.jsonl"):
        month = f.stem
        assert all(json.loads(line)["date"].startswith(month)
                   for line in f.read_text().splitlines())


async def test_every_row_has_exactly_the_documented_fields(
        conn, jpeg_bytes, drop_file, accounts_toml, tmp_path):
    await populated(conn, jpeg_bytes, drop_file)
    dest = tmp_path / "agent"
    workspace.build(conn, dest)
    for stream in ("bank", "receipts"):
        documented = {c.name for c in workspace.columns(
            "bank" if stream == "bank" else "receipt")} | {"match"}
        for row in rows_in(dest / stream):
            assert set(row) == documented, f"{stream} row drifted from SCHEMA.md"


async def test_schema_md_lists_every_column_the_ledger_actually_has(
        conn, jpeg_bytes, drop_file, accounts_toml, tmp_path):
    """The `schema.py` "enforced twice" pattern, pointed at documentation -- the one kind of
    artifact that rots without anybody noticing."""
    await populated(conn, jpeg_bytes, drop_file)
    dest = tmp_path / "agent"
    workspace.build(conn, dest)
    text = (dest / "SCHEMA.md").read_text()
    db = sqlite3.connect(dest / "ledger.db")
    try:
        for table in ("bank", "receipts"):
            for r in db.execute(f"PRAGMA table_info({table})"):
                assert f"| `{r[1]}` |" in text, f"{table}.{r[1]} is not in SCHEMA.md"
    finally:
        db.close()


async def test_the_workspace_is_replaced_whole(
        conn, jpeg_bytes, drop_file, accounts_toml, tmp_path):
    """An agent reading mid-build must never see half a ledger."""
    await populated(conn, jpeg_bytes, drop_file)
    dest = tmp_path / "agent"
    workspace.build(conn, dest)
    (dest / "stale.txt").write_text("from an older build")
    workspace.build(conn, dest)
    assert not (dest / "stale.txt").exists()
    assert not dest.with_name(dest.name + ".tmp").exists()
    assert (dest / "CLAUDE.md").exists()


# --- the manifest -------------------------------------------------------------------------

async def test_the_manifest_reports_a_stale_feed_rather_than_a_quiet_month(
        conn, jpeg_bytes, drop_file, accounts_toml, tmp_path):
    """A quiet month and a broken feed look identical in the rows. This is what tells
    them apart, and it is why CLAUDE.md says to read it first."""
    await populated(conn, jpeg_bytes, drop_file)
    manifest = workspace.build(conn, tmp_path / "agent")
    assert manifest["coverage"]["bank"]["from"] == "2026-08-14"
    assert manifest["coverage"]["receipts"]["rows"] == 1
    assert "simplefin has never run" in manifest["warnings"]
    assert manifest["feed_health"]["drop"]["polls"] == 2


async def test_an_unmapped_account_reaches_the_warnings(conn, jpeg_bytes, drop_file, tmp_path):
    """With no accounts.toml at all, every account is unmapped -- and must be visible."""
    await populated(conn, jpeg_bytes, drop_file)
    manifest = workspace.build(conn, tmp_path / "agent")
    assert any("not named in accounts.toml" in w for w in manifest["warnings"])


# --- the queries ---------------------------------------------------------------------------

async def test_every_canned_query_runs_against_a_freshly_built_ledger(
        conn, jpeg_bytes, drop_file, accounts_toml, tmp_path):
    """Cheap, and it is what keeps the queries from rotting when a column is renamed."""
    await populated(conn, jpeg_bytes, drop_file)
    dest = tmp_path / "agent"
    workspace.build(conn, dest)
    db = sqlite3.connect(dest / "ledger.db")
    try:
        files = sorted((dest / "queries").glob("*.sql"))
        assert len(files) == 8
        for q in files:
            db.execute(q.read_text().strip().rstrip(";")).fetchall()
    finally:
        db.close()


async def test_a_canned_query_says_what_it_does_not_count(tmp_path):
    """Each header carries a decision the reader should not have to re-derive."""
    for q in sorted(workspace.QUERIES.glob("*.sql")):
        head = q.read_text().split("SELECT")[0].split("WITH")[0]
        assert "--" in head and len(head) > 120, f"{q.name} has no explanation"


async def test_the_workspace_never_materialises_a_receipt_image(
        conn, jpeg_bytes, drop_file, accounts_toml, tmp_path):
    """The runtime tmpfs is a few hundred megabytes; a year of phone photographs is not."""
    await populated(conn, jpeg_bytes, drop_file)
    dest = tmp_path / "agent"
    workspace.build(conn, dest)
    for f in dest.rglob("*"):
        if f.is_file():
            assert jpeg_bytes not in f.read_bytes()
    assert not any(dest.rglob("*.jpg"))
