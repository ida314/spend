"""The properties the encrypted store rests on.

If any of these break, the claim stops being "your spending is encrypted at rest" and
becomes "your spending is encrypted at rest, probably, most of the time".
"""

from __future__ import annotations

import hashlib
import json

import pytest

from spend import backup, keys, ledger, paths, replay, seal, store
from spend.service import correct, extract_one, ingest_bytes, rebuild
from tests.conftest import FakeExtractor, ok, watch_files

_ALL = ("receipts", "extractions", "corrections", "transactions", "line_items")


def snapshot(conn) -> dict:
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in _ALL}


async def busy(conn, jpeg_bytes, n: int = 3) -> list[int]:
    ids = []
    for i in range(n):
        rid, _ = ingest_bytes(conn, jpeg_bytes + bytes([i]), mime="image/jpeg")
        await extract_one(conn, rid, FakeExtractor(ok(merchant=f"SHOP {i}")))
        ids.append(rid)
    correct(conn, ids[0], {"merchant": "FIXED BY HAND"})
    correct(conn, ids[1], {"category": "groceries"})
    return ids


# --- nothing durable is plaintext ---------------------------------------------------------

async def test_a_locked_store_has_no_plaintext_under_the_data_root(conn, jpeg_bytes, unlocked):
    await busy(conn, jpeg_bytes)
    conn.close()
    keys.lock()
    assert backup.plaintext_under(paths.log_dir()) == []
    assert backup.plaintext_under(paths.blobs_dir()) == []


async def test_what_a_receipt_says_is_not_findable_on_disk(conn, jpeg_bytes):
    rid, _ = ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    await extract_one(conn, rid, FakeExtractor(ok(merchant="TRADER JOE'S", total="47.03")))
    blob = b"".join(f.read_bytes() for f in paths.data_dir().rglob("*.age"))
    assert b"TRADER JOE" not in blob
    assert b"47.03" not in blob
    assert jpeg_bytes not in blob


# --- the log is append-only, as files ------------------------------------------------------

async def test_nothing_ever_rewrites_or_unlinks_a_sealed_event(conn, jpeg_bytes):
    """Traced at the interpreter's audit layer, so this holds for any call any module makes
    -- not just the ones a reviewer thought to look at. Creating a new file under log/ is how
    an append works; touching one that is already there is the violation."""
    await busy(conn, jpeg_bytes)                      # give it something to rewrite
    with watch_files(paths.log_dir(), paths.blobs_dir()) as violations:
        rid, _ = ingest_bytes(conn, jpeg_bytes + b"x", mime="image/jpeg")
        await extract_one(conn, rid, FakeExtractor(ok(merchant="AGAIN")))
        correct(conn, rid, {"merchant": "AND AGAIN"})
        ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")   # a re-upload: must touch nothing
        rebuild(conn)
        backup.run(paths.data_dir().parent / "b")
    assert violations == []


async def test_the_sealed_files_only_ever_grow(conn, jpeg_bytes):
    """Belt and braces, needing no interception at all: a rewrite changes the hash, an
    unlink loses the key, and a rename-over changes the inode."""
    def fingerprint():
        return {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_ino)
                for root in (paths.log_dir(), paths.blobs_dir())
                for p in root.rglob("*.age")}

    await busy(conn, jpeg_bytes)
    before = fingerprint()
    rid, _ = ingest_bytes(conn, jpeg_bytes + b"z", mime="image/jpeg")
    await extract_one(conn, rid, FakeExtractor(ok()))
    correct(conn, rid, {"total": "9.99"})
    rebuild(conn)
    after = fingerprint()
    assert before.items() <= after.items()
    assert len(after) > len(before)


async def test_re_uploading_the_same_photo_seals_nothing_new(conn, jpeg_bytes):
    ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    n = ledger.count()
    blobs = len(list(paths.blobs_dir().rglob("*.age")))
    rid, is_new = ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    assert not is_new
    assert ledger.count() == n                         # the record hashes the same
    assert len(list(paths.blobs_dir().rglob("*.age"))) == blobs


# --- the database is a cache ---------------------------------------------------------------

async def test_the_cache_can_be_deleted_and_replay_reproduces_it_exactly(
        conn, jpeg_bytes, unlocked):
    """The load-bearing test of the whole design. Ids included: replay sorts before it
    inserts, so the rowids a phone has bookmarked come back the same."""
    await busy(conn, jpeg_bytes, n=4)
    before = snapshot(conn)
    conn.close()

    fresh, stats = replay.fresh(unlocked)
    assert stats.bad == [] and stats.orphans == []
    assert snapshot(fresh) == before
    fresh.close()


async def test_replaying_twice_is_the_same_as_replaying_once(conn, jpeg_bytes, unlocked):
    await busy(conn, jpeg_bytes)
    conn.close()
    a, _ = replay.fresh(unlocked)
    first = snapshot(a)
    a.close()
    b, _ = replay.fresh(unlocked)
    assert snapshot(b) == first
    b.close()


# --- write while locked --------------------------------------------------------------------

def test_an_event_sealed_while_locked_is_read_on_the_next_unlock(conn, jpeg_bytes, unlocked):
    """The property the nightly bank sync depends on: append with public keys only.

    The identity is removed from the process's reach entirely before the append, so this
    cannot pass by accident.
    """
    ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    conn.close()
    keys.lock()
    assert not keys.is_unlocked()

    recipients = seal.load_recipients()                # public keys, still readable
    rec, is_new = ledger.append("feed_poll", {
        "source": "simplefin", "outcome": "ok",
        "started_at": "2026-08-26T04:00:00+00:00",
        "finished_at": "2026-08-26T04:00:02+00:00",
        "window_from": "2026-07-27", "window_to": "2026-08-27",
        "records_seen": 12,
    }, recipients=recipients)
    assert is_new

    paths.ensure_runtime()
    paths.identity_runtime_file().write_text(str(unlocked))
    conn2, stats = replay.fresh(keys.require_identity())
    assert stats.bad == []
    poll = conn2.execute("SELECT * FROM feed_polls WHERE uid=?", (rec.sha,)).fetchone()
    assert poll["records_seen"] == 12
    conn2.close()


def test_appending_needs_no_identity_at_all(home):
    """Stated as bluntly as it can be: the write path never touches a secret."""
    paths.ensure_dirs()
    _, public = seal.generate()
    paths.recipients_file().write_text(public + "\n")
    rec, is_new = ledger.append("correction",
                                {"receipt": "a" * 64, "field": "merchant", "value": "X"})
    assert is_new and rec.path.read_bytes().startswith(seal.MAGIC)
    with pytest.raises(seal.Locked):
        ledger.read(rec.path, None)


# --- corruption ----------------------------------------------------------------------------

async def test_a_corrupt_sealed_event_names_the_file_and_replays_the_rest(
        conn, jpeg_bytes, unlocked):
    await busy(conn, jpeg_bytes, n=3)
    total = ledger.count()
    conn.close()

    victim = sorted(paths.log_dir().rglob("*.age"))[1]
    blob = bytearray(victim.read_bytes())
    blob[-1] ^= 0xFF
    victim.write_bytes(bytes(blob))

    fresh, stats = replay.fresh(unlocked)
    assert len(stats.bad) == 1
    assert stats.bad[0][0] == victim
    assert stats.events == total - 1
    fresh.close()


async def test_a_sealed_event_that_is_not_what_its_name_claims_is_refused(
        conn, jpeg_bytes, unlocked):
    """The second, independent check. age's AEAD catches a flipped bit; this catches a file
    that was swapped for another valid one, or written by a `canonical` that has drifted."""
    ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    a, b = sorted(paths.log_dir().rglob("*.age"))[:2] if ledger.count() > 1 else (None, None)
    rec, _ = ledger.append("correction", {"receipt": "b" * 64, "field": "total", "value": "1"})
    impostor = paths.log_path("c" * 64)
    impostor.parent.mkdir(parents=True, exist_ok=True)
    impostor.write_bytes(rec.path.read_bytes())        # valid age, wrong name
    with pytest.raises(ledger.LedgerCorrupt):
        ledger.read(impostor, unlocked)


# --- ordering -------------------------------------------------------------------------------

async def test_two_corrections_in_the_same_second_keep_their_order(conn, jpeg_bytes, unlocked):
    """Why store.now() carries microseconds.

    Replay assigns the rowids, so `id` can no longer break a same-second tie -- the timestamp
    has to. A double-tap on Save must not become coin-flip-wins.
    """
    rid, _ = ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    for value in ("FIRST", "SECOND", "THIRD"):
        correct(conn, rid, {"merchant": value})
    ats = [r["created_at"] for r in conn.execute(
        "SELECT created_at FROM corrections ORDER BY created_at, id")]
    assert len(set(ats)) == 3, "same-second corrections must be distinguishable"
    conn.close()

    fresh, _ = replay.fresh(unlocked)
    assert fresh.execute(
        "SELECT merchant FROM transactions").fetchone()["merchant"] == "THIRD"
    fresh.close()


async def test_an_extraction_that_sorts_before_its_receipt_is_still_applied(
        conn, jpeg_bytes, unlocked):
    """A clock that steps backwards under NTP is why replay is two passes."""
    rid, _ = ingest_bytes(conn, jpeg_bytes, mime="image/jpeg")
    sha = store.get_receipt(conn, rid)["sha256"]
    conn.close()
    ledger.append("extraction", {
        "receipt": sha, "status": "ok", "model": "m", "prompt_version": "v",
        "render_mode": "ocr", "raw_response": "{}",
        "payload": json.loads(ok(merchant="BACKWARDS").data.model_dump_json()),
        "note": None, "latency_ms": 1,
    }, at="1999-01-01T00:00:00.000000+00:00")           # long before the receipt

    fresh, stats = replay.fresh(unlocked)
    assert stats.orphans == []
    assert fresh.execute(
        "SELECT merchant FROM transactions").fetchone()["merchant"] == "BACKWARDS"
    fresh.close()


# --- keys -----------------------------------------------------------------------------------

def test_init_then_unlock_round_trips_through_the_passphrase(home):
    """The one test that pays scrypt, because it is the one path a human actually walks."""
    from datetime import timedelta
    paths.ensure_dirs()
    public, backup_secret = keys.init("a-long-enough-passphrase")
    assert public.startswith("age1")
    assert backup_secret.startswith("AGE-SECRET-KEY-")
    assert public in paths.recipients_file().read_text()
    assert backup_secret not in paths.recipients_file().read_text()   # secret, not recipient

    with pytest.raises(seal.SealError):
        keys.unlock("the-wrong-passphrase", timedelta(hours=1))
    assert not keys.is_unlocked()

    keys.unlock("a-long-enough-passphrase", timedelta(hours=1))
    assert keys.is_unlocked()
    assert seal.unseal(seal.seal(b"round trip"), keys.require_identity()) == b"round trip"


def test_the_paper_backup_key_can_open_everything(conn, jpeg_bytes, home):
    """The only thing between a forgotten passphrase and total loss."""
    from datetime import timedelta
    paths.ensure_dirs()
    _, backup_secret = keys.init("a-long-enough-passphrase")
    keys.unlock("a-long-enough-passphrase", timedelta(hours=1))
    fresh_conn = store.connect()
    store.migrate(fresh_conn)
    ingest_bytes(fresh_conn, jpeg_bytes, mime="image/jpeg")
    fresh_conn.close()

    paper = seal.load_identity(backup_secret)
    good, bad = ledger.events(paper)
    assert bad == [] and len(good) >= 1


def test_a_weak_passphrase_is_refused(home):
    paths.ensure_dirs()
    for weak in ("short", "1234567890123456"):
        with pytest.raises(ValueError):
            keys.init(weak)


def test_re_initialising_is_refused_because_it_would_orphan_the_log(home):
    paths.ensure_dirs()
    keys.init("a-long-enough-passphrase")
    with pytest.raises(keys.AlreadyInitialised):
        keys.init("another-long-passphrase")


# --- the property the nightly timer depends on ------------------------------------------------

def test_a_feed_pull_records_everything_while_locked(conn, accounts_toml, unlocked):
    """The whole encryption design is arranged so this works, and for a while it did not.

    `spend feeds sync` used to go through the same `_open()` every other command uses, which
    exits when the store is locked -- so the unit that was supposed to append all night
    would have failed every night, and the README, both docs and the systemd unit all said
    otherwise. Sealing needs only the public keys; nothing here should need the identity.
    """
    from spend import keys, ledger, replay, service
    from spend.sources.base import Account, Poll, Record

    conn.close()
    keys.lock()
    assert not keys.is_unlocked()

    before = ledger.count()
    summary = service.record_poll(None, Poll(
        source="simplefin", outcome="ok",
        accounts=(Account(native_id="ACT-quicksilver", name="QS", balance_cents=-81209),),
        records=(Record(native_account="ACT-quicksilver", external_id="SF-7",
                        description="SQ *BLUE BOTTLE COFFEE", amount_cents=-475,
                        posted_at="2026-08-16"),)))
    assert summary["locked"] and summary["new"] == 1
    assert ledger.count() == before + 3          # the poll, the account, the transaction

    # And the next unlock picks it all up.
    paths.ensure_runtime()
    paths.identity_runtime_file().write_text(str(unlocked))
    fresh, stats = replay.fresh(keys.require_identity())
    try:
        assert stats.bad == [] and stats.feed_records == 1
        row = fresh.execute("SELECT * FROM feed_transactions").fetchone()
        assert row["merchant"] == "Blue Bottle Coffee"
        assert row["net_spend_cents"] == 475
    finally:
        fresh.close()


def test_a_locked_pull_of_something_already_seen_seals_nothing(conn, accounts_toml, unlocked):
    """Why the locked path can ask for the widest window it likes: an unchanged transaction
    hashes to a file that is already there."""
    from spend import keys, ledger, service
    from spend.sources.base import Poll, Record

    poll = Poll(source="simplefin", outcome="ok",
                records=(Record(native_account="ACT-quicksilver", external_id="SF-7",
                                description="SQ *BLUE BOTTLE COFFEE", amount_cents=-475,
                                posted_at="2026-08-16"),))
    service.record_poll(conn, poll)
    conn.close()
    keys.lock()

    before = ledger.count()
    assert service.record_poll(None, poll)["new"] == 0
    # The poll event itself is new -- it happened, and every attempt is recorded -- but the
    # transaction it re-delivered is not.
    assert ledger.count() == before + 1
