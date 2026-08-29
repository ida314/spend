"""The bank feeds: signs, identity, idempotence, and the double-count defence.

The sign tests are the ones to read first. A sign error here is silent -- nothing crashes,
the totals are simply wrong -- and three of the four formats disagree with each other about
how to write "money left the account".
"""

from __future__ import annotations

from datetime import date

import pytest

from spend import feed, service, store
from spend.sources import drop
from spend.sources.base import (
    MAX_SPAN_DAYS,
    OVERLAP_DAYS,
    Account,
    FeedError,
    Poll,
    Record,
    Window,
    plan_window,
)
from tests.conftest import applecard_csv, capitalone_csv, checking_csv, ofx, stmttrn


def poll_of(path):
    return drop.read_file(path)


def rows(conn):
    """Keyed by txn_key, not by merchant: two charges at one shop are two transactions, and
    keying on the name would silently hide exactly the case worth testing."""
    return {r["txn_key"]: dict(r) for r in conn.execute("SELECT * FROM feed_transactions")}


def only(conn):
    got = list(rows(conn).values())
    assert len(got) == 1, f"expected one row, got {len(got)}"
    return got[0]


def imported(conn, path, ctx=None):
    service.record_poll(conn, drop.read_file(path))
    service.reproject_feeds(conn, ctx)
    return rows(conn)


# --- the sign, per format -------------------------------------------------------------------

def test_a_capital_one_debit_is_negative_and_a_credit_is_positive(drop_file):
    p = poll_of(drop_file("c1.csv", capitalone_csv(
        "2026-08-14,2026-08-16,7734,SQ *BLUE BOTTLE COFFEE,Dining,4.75,",
        "2026-08-20,2026-08-21,7734,PAYMENT THANK YOU,Payment,,1204.11")))
    assert [r.amount_cents for r in p.records] == [-475, 120411]


def test_an_apple_card_purchase_is_positive_in_the_file_and_negative_in_the_ledger(drop_file):
    """The format that disagrees with every other one, and says nothing about it."""
    p = poll_of(drop_file("ac.csv", applecard_csv(
        "08/14/2026,08/16/2026,TRADER JOES #546,Trader Joes,Grocery,Purchase,47.03",
        "08/25/2026,08/25/2026,ACH Deposit Internet Transfer,Apple Card,Payment,Payment,244.10")))
    assert [r.amount_cents for r in p.records] == [-4703, 24410]


def test_a_capital_one_checking_debit_takes_its_sign_from_the_type_column(drop_file):
    p = poll_of(drop_file("chk.csv", checking_csv(
        "4471,2026-08-20,1204.11,Debit,CAPITAL ONE AUTOPAY PYMT,4102.55",
        "4471,2026-08-15,3200.00,Credit,PAYROLL ACME DIR DEP,5306.66")))
    assert [r.amount_cents for r in p.records] == [-120411, 320000]


def test_an_ofx_amount_is_already_signed_the_way_a_statement_is(drop_file):
    p = poll_of(drop_file("s.ofx", ofx(
        stmttrn("F1", "20260814120000[-5:EST]", "-47.03", "TRADER JOE#546"),
        stmttrn("F2", "20260820120000", "1204.11", "PAYMENT THANK YOU", "CREDIT"))))
    assert [r.amount_cents for r in p.records] == [-4703, 120411]


def test_an_unparseable_amount_is_absence_never_zero(drop_file, conn, accounts_toml):
    p = poll_of(drop_file("bad.csv", capitalone_csv(
        "2026-08-14,2026-08-16,7734,SOMETHING ODD,Dining,THIRTY THREE,")))
    assert p.records[0].amount_cents is None
    assert p.outcome == "partial"

    service.record_poll(conn, p)
    service.reproject_feeds(conn)
    row = only(conn)
    assert row["amount_cents"] is None
    assert row["status"] == "needs_review"
    assert row["net_spend_cents"] == 0          # visible and uncounted, not counted as free


def test_no_monetary_value_is_ever_a_float(drop_file):
    p = poll_of(drop_file("c1.csv", capitalone_csv(
        "2026-08-14,2026-08-16,7734,SUB CENT,Dining,0.745,")))
    for r in p.records:
        assert isinstance(r.amount_cents, int)


# --- net_spend_cents: the double-count defence ------------------------------------------------

def test_summing_every_row_with_no_where_clause_gives_the_right_total(
        conn, drop_file, accounts_toml):
    """The whole point of net_spend_cents.

    A card payment is in this ledger twice -- an outflow on checking and a credit on the
    card -- and neither is spending. Summing the naive column double-counts a year of them.
    """
    imported(conn, drop_file("c1.csv", capitalone_csv(
        "2026-08-14,2026-08-16,7734,SQ *BLUE BOTTLE COFFEE,Dining,4.75,",
        "2026-08-15,2026-08-16,7734,TRADER JOES #546,Grocery,47.03,",
        "2026-08-20,2026-08-21,7734,PAYMENT THANK YOU,Payment,,1204.11")))
    imported(conn, drop_file("chk.csv", checking_csv(
        "4471,2026-08-20,1204.11,Debit,CAPITAL ONE AUTOPAY PYMT,4102.55",
        "4471,2026-08-15,3200.00,Credit,PAYROLL ACME DIR DEP,5306.66")))

    net = conn.execute("SELECT SUM(net_spend_cents) FROM feed_transactions").fetchone()[0]
    assert net == 475 + 4703

    careless = conn.execute("SELECT SUM(outflow_cents) FROM feed_transactions").fetchone()[0]
    assert careless > net * 10, "the trap this column exists to remove must still be a trap"


def test_net_spend_is_zero_for_every_transfer_and_every_card_payment(
        conn, drop_file, accounts_toml):
    got = imported(conn, drop_file("chk.csv", checking_csv(
        "4471,2026-08-20,1204.11,Debit,CAPITAL ONE AUTOPAY PYMT,4102.55",
        "4471,2026-08-19,60.00,Debit,ZELLE TO SAM,4000.00",
        "4471,2026-08-15,3200.00,Credit,PAYROLL ACME DIR DEP,5306.66")))
    for row in got.values():
        assert row["flow"] in ("payment", "transfer", "income")
        assert row["net_spend_cents"] == 0
        assert row["counts_as_spending"] == 0


def test_a_refund_reduces_net_spend(conn, drop_file, accounts_toml):
    got = imported(conn, drop_file("c1.csv", capitalone_csv(
        "2026-08-15,2026-08-16,7734,TRADER JOES #546,Grocery,47.03,",
        "2026-08-22,2026-08-23,7734,TRADER JOES #546 RETURN,Grocery,,12.00")))
    assert sum(r["net_spend_cents"] for r in got.values()) == 4703 - 1200


def test_editing_flows_toml_stops_a_card_payment_being_counted(
        conn, drop_file, accounts_toml, tmp_path):
    """The rules file is read at projection time, so a fix is an edit and a rebuild."""
    from spend import normalize
    empty = tmp_path / "flows.toml"
    empty.write_text("[payment]\npatterns = []\n")
    blind = feed.Context(accounts=accounts_toml, categories=feed.Rules.load(),
                         aliases=normalize.Aliases.load(),
                         flows=normalize.Flows.load(empty))

    path = drop_file("chk.csv", checking_csv(
        "4471,2026-08-20,1204.11,Debit,CAPITAL ONE AUTOPAY PYMT,4102.55"))
    before = imported(conn, path, blind)
    assert only(conn)["net_spend_cents"] == 120411   # miscounted, as expected

    service.reproject_feeds(conn)                                     # the shipped rules
    assert only(conn)["net_spend_cents"] == 0


# --- identity and idempotence -------------------------------------------------------------

def test_the_same_csv_imported_twice_is_one_import(conn, drop_file, accounts_toml):
    path = drop_file("c1.csv", capitalone_csv(
        "2026-08-14,2026-08-16,7734,SQ *BLUE BOTTLE COFFEE,Dining,4.75,"))
    first = service.record_poll(conn, drop.read_file(path))
    second = service.record_poll(conn, drop.read_file(path))
    assert first["new"] == 1
    assert second["new"] == 0
    service.reproject_feeds(conn)
    assert len(rows(conn)) == 1


def test_an_overlapping_export_adds_only_the_new_rows(conn, drop_file, accounts_toml):
    a = drop_file("nov.csv", capitalone_csv(
        "2026-08-14,2026-08-16,7734,SQ *BLUE BOTTLE COFFEE,Dining,4.75,",
        "2026-08-15,2026-08-16,7734,TRADER JOES #546,Grocery,47.03,"))
    b = drop_file("dec.csv", capitalone_csv(
        "2026-08-15,2026-08-16,7734,TRADER JOES #546,Grocery,47.03,",
        "2026-08-18,2026-08-19,7734,SPOTIFY USA,Entertainment,11.99,"))
    assert service.record_poll(conn, drop.read_file(a))["new"] == 2
    assert service.record_poll(conn, drop.read_file(b))["new"] == 1
    service.reproject_feeds(conn)
    assert len(rows(conn)) == 3


def test_two_identical_charges_on_one_day_are_two_transactions(conn, drop_file, accounts_toml):
    """Real and rare: two $4.75 coffees. The ordinal in the synthesised id is what keeps
    them apart, and collapsing them would quietly halve a day."""
    got = imported(conn, drop_file("c1.csv", capitalone_csv(
        "2026-08-14,2026-08-16,7734,SQ *BLUE BOTTLE COFFEE,Dining,4.75,",
        "2026-08-14,2026-08-16,7734,SQ *BLUE BOTTLE COFFEE,Dining,4.75,")))
    total = conn.execute("SELECT COUNT(*), SUM(net_spend_cents) FROM feed_transactions"
                         ).fetchone()
    assert tuple(total) == (2, 950)


def test_a_reordered_export_produces_the_same_ids(drop_file):
    """File order must not participate: an export the bank chose to sort differently is the
    same export, and a new id would be a duplicate."""
    forward = poll_of(drop_file("a.csv", capitalone_csv(
        "2026-08-14,2026-08-16,7734,SQ *BLUE BOTTLE COFFEE,Dining,4.75,",
        "2026-08-15,2026-08-16,7734,TRADER JOES #546,Grocery,47.03,")))
    reverse = poll_of(drop_file("b.csv", capitalone_csv(
        "2026-08-15,2026-08-16,7734,TRADER JOES #546,Grocery,47.03,",
        "2026-08-14,2026-08-16,7734,SQ *BLUE BOTTLE COFFEE,Dining,4.75,")))
    assert {r.external_id for r in forward.records} == {r.external_id for r in reverse.records}


def test_an_ofx_fitid_is_used_verbatim_rather_than_synthesised(drop_file):
    p = poll_of(drop_file("s.ofx", ofx(
        stmttrn("2026081400123", "20260814120000", "-47.03", "TRADER JOE#546"))))
    assert p.records[0].external_id == "2026081400123"
    assert p.detail["synthesised_ids"] == 0


def test_a_format_no_dialect_claims_is_set_aside_rather_than_guessed(drop_file):
    p = poll_of(drop_file("junk.csv", "Alpha,Beta\n1,2\n"))
    assert p.outcome == "error"
    assert p.errors[0].code == "drop.unknown-format"
    assert "Alpha" in p.errors[0].msg          # says what it actually saw
    assert p.records == ()


# --- revisions -------------------------------------------------------------------------------

def test_a_pending_charge_that_posts_appends_rather_than_replacing(conn, accounts_toml):
    """The $52 authorised, $61 posted case: the tip cleared."""
    def observe(cents, pending, posted):
        return Poll(source="simplefin", outcome="ok",
                    accounts=(Account(native_id="ACT-quicksilver", name="QS"),),
                    records=(Record(native_account="ACT-quicksilver", external_id="SF-1",
                                    description="DIME SQUARE", amount_cents=cents,
                                    posted_at=posted, transacted_at="2026-08-14",
                                    pending=pending),))
    service.record_poll(conn, observe(-5200, True, None))
    service.record_poll(conn, observe(-6100, False, "2026-08-16"))
    service.reproject_feeds(conn)

    assert conn.execute("SELECT COUNT(*) FROM feed_records").fetchone()[0] == 2
    row = only(conn)
    assert row["amount_cents"] == -6100        # the newest observation is the current claim
    assert row["pending"] == 0
    assert row["revisions"] == 2
    assert row["amount_changed"] == 1
    assert row["net_spend_cents"] == 6100      # counted once, not twice


def test_re_observing_an_unchanged_transaction_adds_nothing(conn, accounts_toml):
    """Why an overlapping nightly window is free."""
    p = Poll(source="simplefin", outcome="ok",
             records=(Record(native_account="ACT-quicksilver", external_id="SF-1",
                             description="DIME SQUARE", amount_cents=-5200,
                             posted_at="2026-08-16"),))
    assert service.record_poll(conn, p)["new"] == 1
    assert service.record_poll(conn, p)["new"] == 0
    assert conn.execute("SELECT COUNT(*) FROM feed_records").fetchone()[0] == 1


def test_a_duplicate_arriving_from_two_sources_is_flagged_never_merged(
        conn, drop_file, accounts_toml):
    """A CSV overlapping a live feed. Flagged, so the day you reconnect the Bridge is not
    the day you silently double a month."""
    service.record_poll(conn, Poll(
        source="simplefin", outcome="ok",
        records=(Record(native_account="ACT-quicksilver", external_id="SF-1",
                        description="TRADER JOES #546", amount_cents=-4703,
                        posted_at="2026-08-15", transacted_at="2026-08-15"),)))
    service.record_poll(conn, drop.read_file(drop_file("c1.csv", capitalone_csv(
        "2026-08-15,2026-08-15,7734,TRADER JOES #546,Grocery,47.03,"))))
    service.reproject_feeds(conn)
    flagged = [r for r in rows(conn).values() if r["duplicate_suspect"]]
    assert len(flagged) == 1
    assert conn.execute("SELECT COUNT(*) FROM feed_transactions").fetchone()[0] == 2


# --- accounts ---------------------------------------------------------------------------------

def test_an_account_not_named_in_accounts_toml_is_surfaced_not_dropped(conn, accounts_toml):
    """A bank account silently missing from a spending tracker is the worst failure here."""
    service.record_poll(conn, Poll(
        source="simplefin", outcome="ok",
        accounts=(Account(native_id="ACT-9f2a", name="Chase Freedom", org_name="Chase"),),
        records=(Record(native_account="ACT-9f2a", external_id="X-1", description="ANYWHERE",
                        amount_cents=-100, posted_at="2026-08-15"),)))
    service.reproject_feeds(conn)
    account = conn.execute("SELECT * FROM accounts WHERE unmapped=1").fetchone()
    assert account["account_key"] == "unmapped:ACT-9f2a"
    assert only(conn)["status"] == "needs_review"


def test_renaming_a_native_id_in_accounts_toml_refiles_history_without_a_migration(
        conn, accounts_toml, tmp_path):
    """Reconnecting the Bridge issues new ids. History must not fork at the seam."""
    service.record_poll(conn, Poll(
        source="simplefin", outcome="ok",
        accounts=(Account(native_id="ACT-brand-new", name="QS"),),
        records=(Record(native_account="ACT-brand-new", external_id="SF-9",
                        description="SQ *BLUE BOTTLE", amount_cents=-475,
                        posted_at="2026-08-15"),)))
    service.reproject_feeds(conn)
    assert only(conn)["account_key"].startswith("unmapped:")

    from spend import paths
    paths.accounts_rules_file().write_text(
        paths.accounts_rules_file().read_text().replace(
            '"ACT-quicksilver"', '"ACT-quicksilver", "ACT-brand-new"'))
    service.reproject_feeds(conn, feed.Context.load())
    assert only(conn)["account_key"] == "cap1-quicksilver"


def test_a_correction_survives_an_accounts_toml_edit(conn, accounts_toml):
    """Why txn_key deliberately does not contain the account key."""
    service.record_poll(conn, Poll(
        source="simplefin", outcome="ok",
        records=(Record(native_account="ACT-quicksilver", external_id="SF-1",
                        description="SQ *UNKNOWABLE", amount_cents=-999,
                        posted_at="2026-08-15"),)))
    service.reproject_feeds(conn)
    key = next(iter(rows(conn).values()))["txn_key"]
    service.correct_feed(conn, key, {"merchant": "The Corner Shop", "category": "groceries"})

    from spend import paths
    paths.accounts_rules_file().write_text(
        paths.accounts_rules_file().read_text().replace("cap1-quicksilver", "the-card"))
    service.reproject_feeds(conn, feed.Context.load())
    row = only(conn)
    assert row["merchant"] == "The Corner Shop"
    assert row["category"] == "groceries"


# --- dates ------------------------------------------------------------------------------------

def test_a_statement_date_is_not_shifted_by_a_timezone(conn, drop_file, accounts_toml):
    """A statement says "the 14th". It has no time and no timezone, and manufacturing
    midnight UTC would move every imported row a day earlier."""
    got = imported(conn, drop_file("c1.csv", capitalone_csv(
        "2026-08-14,2026-08-16,7734,SQ *BLUE BOTTLE COFFEE,Dining,4.75,")))
    assert only(conn)["dated_on"] == "2026-08-14"


def test_a_late_evening_instant_is_filed_in_the_local_month(conn, accounts_toml):
    """A 9pm charge on the 31st is September in UTC and August in Brooklyn."""
    service.record_poll(conn, Poll(
        source="simplefin", outcome="ok",
        records=(Record(native_account="ACT-quicksilver", external_id="SF-1",
                        description="LATE NIGHT", amount_cents=-500,
                        posted_at="2026-09-01T01:30:00+00:00",
                        transacted_at="2026-09-01T01:30:00+00:00"),)))
    service.reproject_feeds(conn)
    assert only(conn)["dated_on"] == "2026-08-31"


# --- the window planner -------------------------------------------------------------------------

TODAY = date(2026, 8, 26)


def _poll(f, t, outcome="ok", started="2026-08-26T04:00:00+00:00"):
    return {"window_from": f, "window_to": t, "outcome": outcome, "started_at": started}


def test_a_window_never_exceeds_the_protocol_ceiling():
    for polls in ([], [_poll("2026-05-30", "2026-08-27")], [_poll("2026-08-01", "2026-08-27")]):
        w = plan_window(polls, TODAY)
        assert (w.end - w.start).days <= MAX_SPAN_DAYS


def test_consecutive_windows_overlap_because_banks_restate():
    w = plan_window([_poll("2026-05-30", "2026-08-27")], TODAY)
    assert w.start == TODAY - __import__("datetime").timedelta(days=OVERLAP_DAYS)


def test_the_daily_budget_stops_the_planner_before_the_protocol_does():
    spent = [_poll("2026-05-30", "2026-08-27") for _ in range(20)]
    assert plan_window(spent, TODAY) is None


def test_a_gap_left_by_a_week_of_failures_is_re_requested_before_the_tail():
    w = plan_window([_poll("2026-06-01", "2026-07-01"),
                     _poll("2026-07-20", "2026-08-27")], TODAY)
    assert w.start == date(2026, 7, 1) and w.end == date(2026, 7, 20)


def test_a_history_of_nothing_but_failures_starts_from_scratch():
    w = plan_window([_poll(None, None, "error")], TODAY)
    assert (w.end - w.start).days == MAX_SPAN_DAYS


def test_a_window_that_is_too_wide_is_refused_rather_than_truncated():
    with pytest.raises(ValueError):
        Window(start=date(2026, 1, 1), end=date(2026, 12, 31))


# --- the total contract ---------------------------------------------------------------------

def test_an_errlist_entry_is_recorded_on_the_poll_rather_than_raised(conn, accounts_toml):
    """The failure most likely to go unnoticed: a broken connection returns HTTP 200 with
    zero transactions and looks exactly like a frugal week."""
    service.record_poll(conn, Poll(
        source="simplefin", outcome="partial", http_status=200,
        errors=(FeedError(code="con.auth", msg="Capital One needs reauthorisation",
                          conn_id="CON-1"),),
        records=(Record(native_account="ACT-quicksilver", external_id="SF-2",
                        description="STILL WORKING", amount_cents=-100,
                        posted_at="2026-08-15"),)))
    row = conn.execute("SELECT * FROM feed_polls").fetchone()
    assert row["outcome"] == "partial"
    assert "con.auth" in row["errors"]
    assert row["records_seen"] == 1, "the accounts that worked are still there"


def test_a_connection_error_is_the_one_that_needs_a_human():
    assert FeedError(code="con.auth", msg="x").needs_a_human
    assert not FeedError(code="gen.transport", msg="x").needs_a_human
    assert Poll(source="s", outcome="partial",
                errors=(FeedError(code="con.auth", msg="x"),)).needs_a_human


def test_a_source_that_is_down_costs_transactions_and_never_invents_one(conn, accounts_toml):
    service.record_poll(conn, Poll(source="simplefin", outcome="error",
                                   errors=(FeedError(code="gen.transport", msg="timed out"),),
                                   note="timed out"))
    service.reproject_feeds(conn)
    assert conn.execute("SELECT COUNT(*) FROM feed_transactions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM feed_polls").fetchone()[0] == 1


# --- the plaintext that arrives from outside ------------------------------------------------

def test_an_imported_export_does_not_stay_plaintext_on_disk(conn, drop_file, accounts_toml):
    """A statement export is full of merchants, amounts and dates.

    It is plaintext for as long as it sits in the inbox -- unavoidable, you put it there --
    but leaving it that way after import would put a readable copy of the ledger on an
    unencrypted disk forever, which is the exact thing the sealed log exists to prevent.
    """
    from spend import backup, paths
    path = drop_file("c1.csv", capitalone_csv(
        "2026-08-15,2026-08-16,7734,TRADER JOES #546,Grocery,47.03,"))
    sha = __import__("hashlib").sha256(path.read_bytes()).hexdigest()

    service.record_poll(conn, drop.read_file(path))
    drop.archive(path, sha)

    assert not path.exists(), "the plaintext must be gone from the inbox"
    assert paths.blob_path(sha).exists(), "and sealed under its own hash"
    assert backup.plaintext_under(paths.data_dir() / "blobs") == []
    for f in paths.data_dir().rglob("*"):
        if f.is_file():
            assert b"TRADER JOES" not in f.read_bytes()


def test_a_file_no_dialect_claims_stays_readable_so_you_can_look_at_it(
        conn, drop_file, accounts_toml):
    """The one deliberate exception, and it is visible: it lives in `unrecognised/`."""
    from spend import paths
    path = drop_file("junk.csv", "Alpha,Beta\n1,2\n")
    sha = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    dest = drop.archive(path, sha, unrecognised=True)
    assert dest.parent == paths.drop_dir() / "unrecognised"
    assert dest.read_text().startswith("Alpha,Beta")


def test_thirty_nightly_polls_of_the_same_month_seal_one_file_each(conn, accounts_toml):
    """The claim the overlapping-window schedule rests on, stated directly.

    SimpleFIN's guidance is to re-ask for the last few days every night, and the planner asks
    for far more than that. That is only affordable because a re-delivered transaction seals
    to a file that already exists. It very nearly was not: `poll_uid` used to sit inside the
    record's identity hash, so each night's poll got a fresh digest and re-sealed every
    transaction it re-delivered. Nothing noticed, because the database deduplicated on a
    unique index and the log was never counted.
    """
    from spend import ledger
    from spend.sources.base import Account, Poll, Record

    month = Poll(
        source="simplefin", outcome="ok",
        accounts=(Account(native_id="ACT-quicksilver", name="QS", balance_cents=-81209),),
        records=tuple(
            Record(native_account="ACT-quicksilver", external_id=f"SF-{n}",
                   description=f"MERCHANT {n}", amount_cents=-100 * n,
                   posted_at=f"2026-08-{n:02d}")
            for n in range(1, 29)))

    service.record_poll(conn, month)
    after_first = ledger.count()
    assert after_first == 1 + 1 + 28              # the poll, the account, the transactions

    for _ in range(30):
        service.record_poll(conn, month)

    # One new file per night: the poll event itself, because every attempt is recorded even
    # when it turns out to have delivered nothing new. Not one per transaction per night.
    assert ledger.count() == after_first + 30
    assert conn.execute("SELECT COUNT(*) FROM feed_records").fetchone()[0] == 28
