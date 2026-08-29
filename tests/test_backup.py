"""The backup is the only thing standing between a bad disk and receipts that no longer
exist anywhere.

What it checks changed with what it copies. There is no database in a backup any more --
the database is a cache -- so the old claim, that `VACUUM INTO` captures writes still in the
WAL, has no subject. The claim underneath it survives: a backup must not silently miss the
most recent writes. That is now free rather than delicate, and the test below says why.
"""

from __future__ import annotations

import pytest

from spend import backup, ledger, paths, seal
from spend.service import correct, ingest_bytes


@pytest.fixture
def dest(tmp_path):
    return tmp_path / "backups"


def test_an_event_appended_a_moment_ago_is_in_the_backup(conn, jpeg_bytes, dest):
    """The successor to the WAL test, and it holds for a different reason.

    `ledger.append` fsyncs and renames before it returns, so a sealed event is on disk the
    instant the call comes back -- there is no writeback window for a copy to race, and no
    read transaction to take. The old version of this needed VACUUM INTO to be true.
    """
    ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    r = backup.run(dest)
    assert r.logs == ledger.count()
    assert r.blobs == 1


def test_the_backup_contains_no_plaintext(conn, jpeg_bytes, dest):
    """The load-bearing one. A backup lives on a drive in someone else's house."""
    rid, _ = ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    correct(conn, rid, {"merchant": "TRADER JOE'S"})
    backup.run(dest)
    for tree in ("log", "blobs"):
        assert backup.plaintext_under(dest / tree) == []
    # Not just "starts with the age magic": the plaintext must not be in there anywhere.
    for f in (dest / "blobs").rglob("*.age"):
        assert jpeg_bytes not in f.read_bytes()
    for f in (dest / "log").rglob("*.age"):
        assert b"TRADER JOE" not in f.read_bytes()


def test_the_wrapped_identity_comes_across_and_the_secret_never_does(conn, jpeg_bytes, dest):
    """Losing the only copy of identity.age is a likelier catastrophe than scrypt falling."""
    paths.identity_file().write_bytes(seal.wrap("AGE-SECRET-KEY-XYZ", "a-passphrase-here"))
    ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    r = backup.run(dest)
    assert "identity.age" in r.keys
    assert "recipients.txt" in r.keys
    assert (dest / "config" / "identity.age").read_bytes().startswith(seal.MAGIC)
    # The unlocked identity lives in the runtime dir and is not part of the data root.
    assert not (dest / "identity").exists()


def test_second_run_skips_what_is_already_there(conn, jpeg_bytes, dest):
    ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    first = backup.run(dest)
    second = backup.run(dest)
    assert second.logs == 0 and second.blobs == 0
    assert second.present == first.logs + first.blobs


def test_a_truncated_file_is_copied_again(conn, jpeg_bytes, dest):
    """A run killed mid-copy leaves a short file under the right name, and content
    addressing alone would call that one done forever."""
    ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    backup.run(dest)
    victim = next((dest / "blobs").rglob("*.age"))
    victim.write_bytes(victim.read_bytes()[:10])
    assert backup.run(dest).blobs == 1
    assert backup.plaintext_under(dest / "blobs") == []


def test_a_backup_works_while_the_store_is_locked(conn, jpeg_bytes, dest):
    """The nightly timer holds no key at all, which is the whole point of ciphertext."""
    from spend import keys
    ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    conn.close()
    keys.lock()
    assert not keys.is_unlocked()
    r = backup.run(dest)
    assert r.logs > 0 and r.blobs == 1


def test_no_log_is_an_error_not_an_empty_backup(home, dest):
    """Backing up nothing over a good backup is the failure this cannot have."""
    with pytest.raises(FileNotFoundError):
        backup.run(dest)


def test_backup_dir_ignores_spend_home(monkeypatch, tmp_path):
    """A backup that lands inside the tree it is backing up is not a backup."""
    monkeypatch.setenv("SPEND_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SPEND_BACKUP_DIR", raising=False)
    assert paths.backup_dir() not in paths.data_dir().parents
    assert paths.backup_dir() != paths.data_dir()
