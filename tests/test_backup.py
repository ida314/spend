"""The backup is the only thing standing between a bad disk and receipts that no longer
exist anywhere. These check the two claims src/spend/backup.py makes: that the copied
database is complete, and that a second run is cheap without being wrong.
"""

from __future__ import annotations

import sqlite3

import pytest

from spend import backup, paths, store
from spend.service import ingest_bytes


@pytest.fixture
def dest(tmp_path):
    return tmp_path / "backups"


def test_copies_writes_still_in_the_wal(conn, jpeg_bytes, dest):
    """The reason this is VACUUM INTO and not `cp`.

    The receipt is committed but the WAL has not been checkpointed, so the .db file on
    disk does not contain it yet. A file copy would produce a database that opens fine and
    has lost the last receipt.
    """
    ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    conn.commit()

    result = backup.run(dest)

    copy = sqlite3.connect(result.db)
    assert copy.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1


def test_receipt_files_come_across(conn, jpeg_bytes, dest):
    ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    conn.commit()

    result = backup.run(dest)

    assert result.copied == 1 and result.present == 0
    copied = sorted(result.receipts.rglob("*.jpg"))
    assert len(copied) == 1
    assert copied[0].read_bytes() == jpeg_bytes
    # Sharded the same way as the live tree, so restoring is a move and not a re-ingest.
    assert copied[0].parent.name == copied[0].stem[:2]


def test_second_run_skips_what_is_already_there(conn, jpeg_bytes, dest):
    ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    conn.commit()
    backup.run(dest)

    result = backup.run(dest)

    assert result.copied == 0 and result.present == 1


def test_a_truncated_file_is_copied_again(conn, jpeg_bytes, dest):
    """Content addressing says a same-named file is the same file. A run killed mid-copy
    breaks that, and size is what catches it."""
    ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    conn.commit()
    result = backup.run(dest)
    victim = next(result.receipts.rglob("*.jpg"))
    victim.write_bytes(jpeg_bytes[:100])

    result = backup.run(dest)

    assert result.copied == 1
    assert victim.read_bytes() == jpeg_bytes


def test_the_previous_backup_survives_a_failed_run(conn, jpeg_bytes, dest):
    """The rename is what makes the swap atomic. A leftover .tmp from a killed run must
    not be mistaken for the backup, and must not block the next one."""
    ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    conn.commit()
    first = backup.run(dest)
    good = first.db.read_bytes()
    (dest / (first.db.name + ".tmp")).write_bytes(b"half a database")

    result = backup.run(dest)

    assert not (dest / (first.db.name + ".tmp")).exists()
    assert result.db.read_bytes() != b"half a database"
    assert len(result.db.read_bytes()) >= len(good)


def test_no_database_is_an_error_not_an_empty_backup(dest):
    """Backing up nothing over a good backup is the failure this cannot have."""
    with pytest.raises(FileNotFoundError):
        backup.run(dest)
    assert not (dest / "spend.db").exists()


def test_backup_dir_ignores_spend_home(monkeypatch, tmp_path):
    """SPEND_HOME moves the whole app; it must not move the backup inside it."""
    monkeypatch.setenv("SPEND_HOME", str(tmp_path / "app"))
    monkeypatch.delenv("SPEND_BACKUP_DIR", raising=False)
    assert (tmp_path / "app") not in paths.backup_dir().parents

    monkeypatch.setenv("SPEND_BACKUP_DIR", str(tmp_path / "elsewhere"))
    assert paths.backup_dir() == tmp_path / "elsewhere"
