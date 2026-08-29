"""CSV and OFX exports, dropped in a folder.

This is the Apple Card path -- the Bridge backfills only about a month at connect, and Wallet
exports the rest one month at a time -- and it is the disaster-recovery path for any
connection that breaks. It needs no network, no credential and no third party, which is why
it is worth having even when the feed is healthy.

## Prefer OFX

Apple Card and most banks offer both. Take the OFX: it carries `<FITID>`, a genuinely stable
per-transaction id, and that makes the synthesised-id problem below disappear entirely. CSV
is supported because sometimes OFX is not offered, not because it is as good.

## Claiming a file

Hash, check, parse, commit, *then* move. That order is the same argument
`service.ingest_bytes` makes about writing the file before the row. A crash between the
commit and the move leaves the file in the inbox and the records committed; the next run
re-hashes it, finds its poll row, and just archives it. The reverse order loses an import to
a crash, silently, and you would not find out until you went looking for a month that was
never there.

## The sign, per format

Every export disagrees with every other one and none of them says so:

  capitalone-credit    two columns, `Debit` and `Credit`, both positive
  capitalone-checking  one positive column plus a `Transaction Type` of Debit/Credit
  applecard            one column, and a purchase is POSITIVE
  ofx                  `<TRNAMT>`, already signed the way a statement is

`base.py` states the invariant they are all normalised into. The tests assert it per format,
because a sign error here is silent -- nothing crashes, the totals are just wrong.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from spend import paths
from spend.money import MoneyError, to_cents
from spend.sources.base import Account, FeedError, Poll, Record, Window

NAME = "drop"
UNIT = "\x1f"


def _iso(raw: str | None) -> str | None:
    """A bare calendar date, from any of the shapes an issuer actually writes.

    `YYYY-MM-DD`, deliberately, and not an instant. A statement export says "the 14th" -- it
    has no time in it and no timezone, and the 14th on your statement is the 14th in your
    kitchen. Manufacturing midnight UTC and then converting that to local time would move
    every transaction in an export a day earlier, which is the sort of error that looks like
    a rounding quirk in a monthly total and is actually a whole day of spending in the wrong
    month. `feed.local_date` passes a bare date straight through for the same reason.
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%Y/%m/%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    if len(text) >= 8 and text[:8].isdigit():          # OFX: 20260814120000[-5:EST]
        try:
            return datetime.strptime(text[:8], "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    return None


def _cents(raw: str | None) -> tuple[int | None, str | None]:
    """Absence, never zero. Returns (cents, reject_reason)."""
    if raw is None or not str(raw).strip():
        return None, None
    try:
        return to_cents(str(raw)), None
    except MoneyError as exc:
        return None, str(exc)


def _tail(raw: str | None, default: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    return digits[-4:] if len(digits) >= 4 else (raw or default).strip() or default


# --- dialects -------------------------------------------------------------------------------
# Sniffed from the header row, never from the filename: Capital One's export names are
# seasonal and Apple's are localised, and guessing from a filename is how a checking export
# gets read with a credit card's sign convention.


def _capitalone_credit(row: dict) -> dict:
    debit, credit = row.get("Debit"), row.get("Credit")
    cents, why = _cents(debit or credit)
    if cents is not None:
        # The sign lives in which column is filled, not in the number.
        cents = -abs(cents) if (debit or "").strip() else abs(cents)
    return {
        "native_account": f"drop:capitalone-credit:{_tail(row.get('Card No.'), 'card')}",
        "posted_at": _iso(row.get("Posted Date")),
        "transacted_at": _iso(row.get("Transaction Date")),
        "description": (row.get("Description") or "").strip(),
        "amount_cents": cents, "reject_reason": why,
    }


def _capitalone_checking(row: dict) -> dict:
    cents, why = _cents(row.get("Transaction Amount"))
    kind = (row.get("Transaction Type") or "").strip().casefold()
    if cents is not None:
        cents = -abs(cents) if kind.startswith("debit") else abs(cents)
    posted = _iso(row.get("Transaction Date"))
    return {
        "native_account": f"drop:capitalone-checking:{_tail(row.get('Account Number'), 'acct')}",
        "posted_at": posted, "transacted_at": posted,
        "description": (row.get("Transaction Description") or "").strip(),
        "amount_cents": cents, "reject_reason": why,
    }


def _applecard(row: dict) -> dict:
    cents, why = _cents(row.get("Amount (USD)"))
    kind = (row.get("Type") or "").strip().casefold()
    if cents is not None:
        # Apple writes a purchase as a POSITIVE number, the opposite of every other format
        # here and of the invariant in base.py. `Type` is what tells a payment from a charge.
        cents = abs(cents) if kind == "payment" else -abs(cents)
    return {
        "native_account": "drop:applecard:apple-card",
        "posted_at": _iso(row.get("Clearing Date")),
        "transacted_at": _iso(row.get("Transaction Date")),
        # `Description` and not `Merchant`, even though Merchant is far cleaner. The raw
        # description is what rules/flows.toml matches on, and Apple's payment rows say
        # "ACH Deposit Internet Transfer" there and "Apple Card" in Merchant -- keeping only
        # the tidy one would throw away the entire signal that this is not spending.
        "description": (row.get("Description") or "").strip(),
        # Merchant is already clean, so it is offered to the projector as a payee and skips
        # normalisation. That is the one genuine advantage this format has.
        "payee": (row.get("Merchant") or "").strip() or None,
        "flow_hint": "payment" if kind == "payment" else None,
        "amount_cents": cents, "reject_reason": why,
    }


DIALECTS: tuple[tuple[str, frozenset, Callable], ...] = (
    ("capitalone-credit",
     frozenset({"Transaction Date", "Posted Date", "Description", "Debit", "Credit"}),
     _capitalone_credit),
    ("capitalone-checking",
     frozenset({"Transaction Date", "Transaction Amount", "Transaction Type",
                "Transaction Description"}),
     _capitalone_checking),
    ("applecard",
     frozenset({"Transaction Date", "Clearing Date", "Merchant", "Amount (USD)"}),
     _applecard),
)


class UnknownFormat(ValueError):
    """No dialect claimed this header, and guessing would be worse than refusing."""


def sniff(header: list[str]) -> tuple[str, Callable]:
    have = {h.strip() for h in header}
    for name, required, parse in DIALECTS:
        if required <= have:
            return name, parse
    raise UnknownFormat(f"no dialect claims these columns: {', '.join(sorted(have))}")


# --- parsing ---------------------------------------------------------------------------------

_STMTTRN = re.compile(r"<STMTTRN>(.*?)</STMTTRN>", re.S | re.I)
_ACCTID = re.compile(r"<ACCTID>\s*([^\s<]+)", re.I)


def _ofx_tag(block: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>\s*([^<\r\n]*)", block, re.I)
    return m.group(1).strip() if m else None


def parse_ofx(text: str) -> tuple[str, list[tuple]]:
    acct = _ACCTID.search(text)
    native = f"drop:ofx:{_tail(acct.group(1) if acct else None, 'ofx')}"
    rows = []
    for block in _STMTTRN.findall(text):
        cents, why = _cents(_ofx_tag(block, "TRNAMT"))   # already statement-signed
        posted = _iso(_ofx_tag(block, "DTPOSTED"))
        rows.append({
            "native_account": native, "posted_at": posted,
            "transacted_at": _iso(_ofx_tag(block, "DTUSER")) or posted,
            "description": (_ofx_tag(block, "NAME") or _ofx_tag(block, "MEMO") or "").strip(),
            "amount_cents": cents, "reject_reason": why,
            "memo": _ofx_tag(block, "MEMO"),
            # <FITID> is a genuinely stable id. Using it verbatim is the whole reason to
            # prefer OFX: no synthesised digest, so a restated description cannot duplicate.
            "external_id": _ofx_tag(block, "FITID"),
            "raw": {"memo": _ofx_tag(block, "MEMO"), "trntype": _ofx_tag(block, "TRNTYPE")},
        })
    return "ofx", rows


def parse_csv(text: str) -> tuple[str, list[tuple]]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise UnknownFormat("the file has no header row")
    dialect, parse = sniff(list(reader.fieldnames))
    rows = []
    for raw in reader:
        if not any((v or "").strip() for v in raw.values()):
            continue
        rows.append({**parse(raw), "raw": dict(raw)})
    return dialect, rows


def _synthesise_ids(rows: list[tuple]) -> list[str]:
    """A stable id for a format that has none.

    Sorted by content first, so an export whose row order changed produces the same ids. The
    ordinal within a group of otherwise-identical rows is what keeps two genuinely separate
    $4.75 coffees on one day from collapsing into one transaction.

    The cost, stated plainly: if the bank restates a description between two exports, the
    same charge gets a new id and lands twice. That is what `duplicate_suspect` exists to
    surface, and it is the reason to prefer OFX, which does not have this problem at all.
    """
    order = sorted(range(len(rows)),
                   key=lambda i: (rows[i].get("posted_at") or "", rows[i]["description"],
                                  rows[i].get("amount_cents") or 0))
    seen: dict[tuple, int] = {}
    ids = [""] * len(rows)
    for i in order:
        r = rows[i]
        native, posted = r["native_account"], r.get("posted_at") or ""
        group = (native, posted, r.get("amount_cents"), r["description"])
        ordinal = seen.get(group, 0)
        seen[group] = ordinal + 1
        ids[i] = hashlib.sha256(UNIT.join(
            [native, posted, str(r.get("amount_cents")), r["description"], str(ordinal)]
        ).encode()).hexdigest()[:24]
    return ids


def read_file(path: Path) -> Poll:
    """Parse one export into a Poll. Never raises; an unreadable file is a recorded error."""
    started = datetime.now(UTC).isoformat()
    try:
        blob = path.read_bytes()
    except OSError as exc:
        return Poll(source=NAME, outcome="error", file_name=path.name,
                    errors=(FeedError(code="drop.unreadable", msg=str(exc)),),
                    note=str(exc), detail={"started_at": started})
    sha = hashlib.sha256(blob).hexdigest()
    text = blob.decode("utf-8-sig", errors="replace")

    try:
        dialect, rows = (parse_ofx(text) if _looks_like_ofx(text) else parse_csv(text))
    except UnknownFormat as exc:
        return Poll(source=NAME, outcome="error", file_sha256=sha, file_name=path.name,
                    errors=(FeedError(code="drop.unknown-format", msg=str(exc)),),
                    note=str(exc), detail={"started_at": started})

    synthesised = _synthesise_ids(rows)
    records, accounts = [], {}
    for row, made in zip(rows, synthesised, strict=True):
        native = row["native_account"]
        records.append(Record(
            native_account=native,
            external_id=row.get("external_id") or made,
            description=row["description"], amount_cents=row.get("amount_cents"),
            posted_at=row.get("posted_at"), transacted_at=row.get("transacted_at"),
            pending=False, payee=row.get("payee"), memo=row.get("memo"),
            flow_hint=row.get("flow_hint"), reject_reason=row.get("reject_reason"),
            raw=row.get("raw", {})))
        accounts.setdefault(native, Account(native_id=native, name=native.split(":")[-1]))

    bad = [r for r in records if r.amount_cents is None]
    return Poll(
        source=NAME,
        outcome="partial" if bad else "ok",
        accounts=tuple(accounts.values()),
        records=tuple(records),
        errors=tuple(FeedError(code="drop.amount", msg=f"{r.description}: {r.reject_reason}")
                     for r in bad),
        file_sha256=sha, file_name=path.name,
        detail={"dialect": dialect, "rows": len(rows), "started_at": started,
                "synthesised_ids": sum(1 for r in rows if not r.get("external_id"))})


def _looks_like_ofx(text: str) -> bool:
    head = text.lstrip()[:200].upper()
    return head.startswith("OFXHEADER") or "<OFX>" in head


def inbox() -> list[Path]:
    root = paths.drop_dir() / "inbox"
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir()
                  if p.is_file() and p.suffix.lower() in (".csv", ".ofx", ".qfx", ".txt"))


def archive(path: Path, sha: str, *, unrecognised: bool = False) -> Path:
    """Take a claimed file out of the inbox. Idempotent: only ever called after the commit.

    A recognised export is **sealed and the plaintext removed**. It arrived from outside and
    was plaintext for as long as it sat in the inbox -- that is unavoidable, you dropped it
    there -- but archiving it as-is would leave a file full of merchants, amounts and dates
    on an unencrypted disk forever, which is the exact thing this whole design exists to
    prevent. It is sealed under its own hash into `blobs/`, the same place receipt
    originals live, so `spend verify` covers it and `spend backup` carries it.

    An *unrecognised* file stays plaintext, deliberately and visibly. You have to be able to
    open it to work out what dialect it is, and sealing a file whose format nobody has
    identified yet would make that a chore for no benefit -- it is still sitting in a
    directory called `unrecognised/` that you are being asked to go and look at.
    """
    root = paths.drop_dir() / ("unrecognised" if unrecognised else "archive")
    if unrecognised:
        root.mkdir(parents=True, exist_ok=True)
        dest = root / f"{datetime.now(UTC):%Y-%m-%d}-{sha[:8]}-{path.name}"
        if not dest.exists():
            path.rename(dest)
        return dest

    from spend import seal                    # noqa: PLC0415 - keeps sources/ import-light
    dest = paths.blob_path(sha)
    seal.seal_to(dest, path.read_bytes())
    path.unlink(missing_ok=True)
    return dest
