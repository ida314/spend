"""The seam between "an account exists somewhere" and "we have its transactions".

One protocol, so a second feed is a module rather than a refactor, and so tests can supply a
source that returns an errlist on purpose. The contract is deliberately total, in the same
words as `extract/base.py`: `poll` returns a `Poll` describing what happened, and never
raises. A bridge that is down, rate-limited, mid-reauth or returning nonsense costs you the
transactions you did not have yet; it must never be able to produce wrong ones.

`poll` is synchronous, unlike `Extractor.extract`. Extraction is async because `sir` queues
around a model swap and a receipt may wait minutes. A feed poll is one HTTP GET on a daily
timer; making it async would buy nothing and would put an event loop in the CLI path.

## The sign invariant

`amount_cents` is signed from the account holder's point of view: money that left the account
is negative, money that arrived is positive. A coffee is -475 on a checking account and -475
on a credit card. This is the sign a statement uses and the sign SimpleFIN uses, so it is the
sign that survives contact with the source.

Every adapter is responsible for normalising into it, and two of the formats disagree.
Capital One splits into `Debit` and `Credit` columns that are *both positive*, so the sign
lives in which one is filled. Apple Card writes a purchase as a *positive* number. Getting
this wrong is silent -- the totals are simply the wrong sign, and nothing crashes -- so it is
asserted per format in the tests rather than trusted.

An amount that will not parse becomes `amount_cents = None` with a `reject_reason`, never
zero. That is `project._cents`'s rule, one layer out: "we could not read it" and "it was
free" are different facts, and a zero would enter the totals indistinguishably from a
genuinely free item.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol

# The protocol ceiling is 90 days per request; one day of slack absorbs timezone rounding at
# the window edges rather than discovering it as a missing transaction.
MAX_SPAN_DAYS = 89

# SimpleFIN's own guidance. Banks restate, so re-ask for the last few days every time. It is
# free: the log is content-addressed, so a re-delivered transaction writes no new file.
OVERLAP_DAYS = 5

# A year plus a month, so a month-over-month view has a same-month-last-year figure to
# compare against on the first day it runs.
BACKFILL_DAYS = 400

# The protocol ceiling is 24 requests a day. Four are held back for a human debugging at the
# terminal, because discovering the limit by being cut off is the worst time to discover it.
MAX_CALLS_DAY = 20


@dataclass(frozen=True)
class Window:
    start: date
    end: date

    def __post_init__(self) -> None:
        if (self.end - self.start).days > MAX_SPAN_DAYS:
            raise ValueError(f"a window may not exceed {MAX_SPAN_DAYS} days")


@dataclass(frozen=True)
class Account:
    native_id: str
    name: str | None = None
    org_name: str | None = None
    org_domain: str | None = None
    org_id: str | None = None
    currency: str = "USD"
    balance_cents: int | None = None
    available_balance_cents: int | None = None
    balance_at: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Record:
    native_account: str
    external_id: str
    description: str
    amount_cents: int | None
    posted_at: str | None = None        # ISO instant; None while pending
    transacted_at: str | None = None
    pending: bool = False
    currency: str = "USD"
    payee: str | None = None
    memo: str | None = None
    reject_reason: str | None = None
    # What the source itself says this is, when it says so authoritatively rather than in
    # prose: Apple Card ships a `Type` column, OFX ships `<TRNTYPE>`. A stated fact beats a
    # substring guess, so the projector prefers it over its structural default -- but not
    # over rules/flows.toml, which is the hand-written override and has to stay on top.
    flow_hint: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FeedError:
    """One entry from a source's own error list, kept verbatim.

    `code` follows SimpleFIN's `prefix.subcode` shape -- `con.auth`, `act.failed`, and this
    codebase's own `gen.transport`, `drop.unknown-format`. The prefix is what decides whether
    a human has to do something: only a `con.*` needs someone to redo the setup-token dance,
    and nothing else will fix it.
    """

    code: str
    msg: str
    account_id: str | None = None
    conn_id: str | None = None

    @property
    def needs_a_human(self) -> bool:
        return self.code.startswith("con.")


@dataclass(frozen=True)
class Poll:
    """The outcome of one attempt. `outcome` mirrors the feed_polls table.

    'ok'      -- the source answered and had nothing to complain about
    'partial' -- the source answered and reported problems with some accounts
    'error'   -- nothing usable came back at all

    'partial' with a non-empty `records` is normal and correct, not a contradiction: a
    SimpleFIN AccountSet carries `errlist` alongside the accounts that *did* work, and
    throwing those away because one connection is broken would lose real data.
    """

    source: str
    outcome: str
    accounts: tuple[Account, ...] = ()
    records: tuple[Record, ...] = ()
    errors: tuple[FeedError, ...] = ()
    window: Window | None = None
    http_status: int | None = None
    note: str | None = None
    file_sha256: str | None = None
    file_name: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def needs_a_human(self) -> bool:
        return any(e.needs_a_human for e in self.errors)

    def seen_ids(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in self.records:
            out.setdefault(r.native_account, []).append(r.external_id)
        return out


class Source(Protocol):
    name: str

    def poll(self, window: Window | None) -> Poll: ...


# --- the schedule, as a pure function -------------------------------------------------------

def plan_window(polls: list, today: date) -> Window | None:
    """What to ask for next, or None when there is nothing to ask for.

    Pure, and takes the poll history rather than a connection, so the whole calling schedule
    is testable without a database, a network or a clock.

    There is deliberately no cursor. The watermark is derived from the polls that succeeded,
    the daily budget is derived by counting today's, and both would otherwise be a fifth
    thing that can disagree with the log -- which is the argument `store.pending_receipt_ids`
    already makes about the extraction queue. A stored cursor also could not survive the
    database being a rebuildable cache, and could not be *read back* by a sync running while
    the store is locked. Deriving it settles all three at once.
    """
    done = [p for p in polls if p["outcome"] in ("ok", "partial")]
    if len([p for p in polls if _started_on(p) == today]) >= MAX_CALLS_DAY:
        return None

    # `end` is tomorrow throughout: a bank in a later timezone can post something dated
    # tomorrow, and a window that stopped at today would miss it until the day after.
    end = today + timedelta(days=1)
    floor_for = lambda e: e - timedelta(days=MAX_SPAN_DAYS)  # noqa: E731

    if not done:
        # First run: walk backwards a chunk at a time. Five calls covers BACKFILL_DAYS, well
        # inside the budget, so a cold start finishes in one night rather than one a day.
        return Window(start=floor_for(end), end=end)

    covered = sorted((p["window_from"], p["window_to"]) for p in done
                     if p["window_from"] and p["window_to"])
    if gap := _first_gap(covered, today):
        return gap

    earliest = date.fromisoformat(covered[0][0])
    backfill_floor = today - timedelta(days=BACKFILL_DAYS)
    if earliest > backfill_floor:
        # Still backfilling. Anything older than ninety days is unrecoverable from the
        # Bridge, so this only ever reaches back as far as the protocol will answer for --
        # and a hole beyond that becomes a `doctor` line telling you to export a CSV.
        back_end = earliest
        start = max(floor_for(back_end), backfill_floor, floor_for(end))
        if start < back_end:
            return Window(start=start, end=back_end)

    watermark = date.fromisoformat(max(w for _, w in covered))
    start = min(watermark, today) - timedelta(days=OVERLAP_DAYS)
    return Window(start=max(start, floor_for(end)), end=end)


def _started_on(poll) -> date | None:
    try:
        return date.fromisoformat(poll["started_at"][:10])
    except (ValueError, TypeError, KeyError):
        return None


def _first_gap(covered: list[tuple[str, str]], today: date) -> Window | None:
    """A hole left by a week of failures, if it is still inside the protocol's reach.

    Returned *before* the tail, because the tail will still be there tomorrow and a gap that
    ages past ninety days is gone for good -- at which point the only repair is a CSV export,
    which is a thing a human has to do.
    """
    floor = today - timedelta(days=MAX_SPAN_DAYS)
    reach_end = None
    for raw_from, raw_to in covered:
        start, end = date.fromisoformat(raw_from), date.fromisoformat(raw_to)
        if reach_end is not None and start > reach_end:
            hole_start, hole_end = max(reach_end, floor), min(start, today)
            if hole_start < hole_end:
                return Window(start=hole_start,
                              end=min(hole_end, hole_start + timedelta(days=MAX_SPAN_DAYS)))
            continue
        reach_end = end if reach_end is None else max(reach_end, end)
    return None
