"""Turning what the bank said into the rows a page and an agent read. Pure: no SQLite.

The rule this module exists to enforce is the same one `project.py` enforces for receipts: a
feed transaction is never stored as a fact. It is recomputed from the observations, the
corrections, and four rules files -- every time any of them changes. Edit `flows.toml` and a
year of card payments stops counting as spending; edit `merchants.toml` and eight months of
`SQ *` descriptions get a name; fix a bug here and every historical row is right, with no
backfill and no re-fetch from a bank that only keeps ninety days.

## net_spend_cents, and why there are four amount columns

One signed integer is a trap for every consumer, so the projection carries four:

    amount_cents     signed, bank convention. The fact. NULL when it would not parse.
    outflow_cents    max(0, -amount).  Never negative.
    inflow_cents     max(0,  amount).  Never negative.
    net_spend_cents  the column you SUM.

`net_spend_cents` is positive for a purchase, negative for a refund, and **exactly zero** for
transfers, card payments, income, unparseable rows and tombstones. It is safe to sum over any
subset of any table with no WHERE clause at all.

That is the whole point of it. The WHERE clause is what a careless consumer forgets, and the
consumer here is often an agent answering a question in one shot. Making the common mistake
impossible is worth more than a rule in a document that says do not make it: $1,204.11 moving
from checking to a credit card is in this ledger twice, and summing `outflow_cents` would add
a year of card payments to a year of purchases and be confidently, invisibly wrong.

## Two axes, not one enum

`category` answers *what kind of thing was bought*. `flow` answers *whether anything was
bought at all*. They are independent questions, so growing the ten-value category enum to
hold `transfer` and `payment` would have conflated them -- and a card payment does not stop
being uncategorised just because it is not spending.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from spend import config, normalize, paths
from spend.project import Rules

RULES_DIR = Path(__file__).resolve().parent / "rules"
ACCOUNTS_PATH = RULES_DIR / "accounts.toml"

UNIT = "\x1f"
SPENDING_FLOWS = ("spend", "refund", "fee")
CORRECTABLE = ("merchant", "category", "flow", "note", "deleted")


def txn_key(source: str, native_account: str, external_id: str) -> str:
    """One transaction's identity, across rebuilds and rules edits.

    Deliberately does not contain the account key. The account key comes from a hand-edited
    file, and if it were in here, renaming an account would orphan every correction ever
    typed against its transactions.
    """
    return hashlib.sha256(
        UNIT.join([source, native_account, external_id]).encode()).hexdigest()[:24]


def content_sha(record) -> str:
    """A revision id over a canonical subset, not over the whole payload.

    Whole-payload hashing would let a bridge that re-serialises `extra` in a different key
    order manufacture a revision every single night, and `revisions` would become a count of
    polls rather than a count of times the bank changed its mind.
    """
    subset = {
        "posted_at": record.posted_at, "transacted_at": record.transacted_at,
        "amount_cents": record.amount_cents, "description": record.description,
        "payee": record.payee, "memo": record.memo, "pending": bool(record.pending),
    }
    return hashlib.sha256(
        json.dumps(subset, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def content_sha_account(account) -> str:
    """A balance observation's identity. A poll where nothing moved appends nothing."""
    subset = {"name": account.name, "org_name": account.org_name,
              "currency": account.currency, "balance_cents": account.balance_cents,
              "available_balance_cents": account.available_balance_cents,
              "balance_at": account.balance_at}
    return hashlib.sha256(
        json.dumps(subset, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --- account identity ------------------------------------------------------------------------

@dataclass(frozen=True)
class Accounts:
    """`native_id` -> the account key you chose. See rules/accounts.toml for why."""

    entries: tuple[tuple[str, dict], ...] = ()

    @classmethod
    def load(cls, path: Path | None = None) -> Accounts:
        # The user's own file wins; the shipped one is a commented-out example.
        chosen = path or (paths.accounts_rules_file()
                          if paths.accounts_rules_file().exists() else ACCOUNTS_PATH)
        raw = tomllib.loads(chosen.read_text())
        return cls(tuple((key, body) for key, body in raw.items()))

    def key_for(self, native_id: str) -> tuple[str, dict, bool]:
        """Returns (account_key, body, unmapped).

        An account the feed reported that this file does not claim is *not* dropped. A bank
        account silently missing from a spending tracker is the worst failure this system
        has, so it gets a synthetic key and `doctor` exits non-zero until you name it.
        """
        for key, body in self.entries:
            if native_id in body.get("native", []):
                return key, body, False
        return f"unmapped:{native_id}", {}, True


@dataclass(frozen=True)
class Context:
    """Everything the projection reads, so a test can vary one thing at a time."""

    accounts: Accounts
    categories: Rules
    aliases: normalize.Aliases
    flows: normalize.Flows
    tz: str = config.TZ

    @classmethod
    def load(cls) -> Context:
        return cls(accounts=Accounts.load(), categories=Rules.load(),
                   aliases=normalize.Aliases.load(), flows=normalize.Flows.load())


def local_date(instant: str | None, tz: str) -> str | None:
    """A UTC instant becomes a local calendar date, here and nowhere else.

    Truth holds instants; the projection holds dates. "What did I spend in August" is a
    local-calendar question, and a 9pm charge on the 31st is September in UTC. Changing TZ
    and rebuilding re-files the boundary rows, which is correct -- and impossible if the date
    had been frozen into truth.
    """
    if not instant:
        return None
    # A bare YYYY-MM-DD is already a calendar date -- that is what a statement export gives
    # -- and there is no instant to convert. Passing it through a timezone would silently
    # move every imported transaction one day earlier.
    if len(instant) == 10:
        try:
            return datetime.strptime(instant, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(instant).astimezone(ZoneInfo(tz)).date().isoformat()
    except (ValueError, TypeError):
        return None


# --- one transaction --------------------------------------------------------------------------

def project_one(observations: list, corrections: list, ctx: Context) -> dict:
    """Project every observation of one external transaction into one row.

    `observations` are ordered oldest first. The newest wins -- not "the one where pending is
    0", because a bank can un-post and re-post, and the newest observation is by definition
    its current claim.
    """
    first, latest = observations[0], observations[-1]
    source, native = latest["source"], latest["native_account"]
    account_key, body, unmapped = ctx.accounts.key_for(native)
    kind = body.get("kind", "other") if not unmapped else "other"

    amounts = [o["amount_cents"] for o in observations if o["amount_cents"] is not None]
    amount = latest["amount_cents"]
    description = latest["description"] or ""
    posted_on = local_date(latest["posted_at"], ctx.tz)
    transacted_on = local_date(latest["transacted_at"], ctx.tz)

    payee = latest.get("payee")
    merchant = payee or normalize.merchant(description, ctx.aliases)
    hint = latest.get("flow_hint")

    row = {
        "txn_key": txn_key(source, native, latest["external_id"]),
        "account_key": account_key, "source": source, "native_account": native,
        "external_id": latest["external_id"], "record_uid": latest["uid"],
        "posted_on": posted_on, "transacted_on": transacted_on,
        "dated_on": transacted_on or posted_on,
        "pending": int(bool(latest["pending"])),
        "currency": latest["currency"] or "USD",
        "amount_cents": amount,
        "description": description,
        "merchant": merchant,
        "merchant_key": normalize.key(merchant),
        "category": None, "flow": "unknown",
        "status": "ok", "review_reason": None,
        "first_seen_at": first["observed_at"], "last_seen_at": latest["observed_at"],
        "revisions": len(observations),
        "amount_changed": int(len(set(amounts)) > 1),
        "vanished": 0, "duplicate_suspect": 0, "deleted": 0,
    }

    manual: dict[str, str | None] = {}
    for c in corrections:
        if c["field"] in CORRECTABLE:
            manual[c["field"]] = c["value"]

    if "merchant" in manual:
        row["merchant"] = manual["merchant"] or None
        row["merchant_key"] = normalize.key(row["merchant"])
    if "deleted" in manual:
        row["deleted"] = 1 if manual["deleted"] in ("1", "true", "yes") else 0

    row["flow"] = (manual.get("flow")
                   or ctx.flows.classify(description)
                   or hint
                   or _structural_flow(amount, kind))
    row["category"] = (manual["category"] or None) if "category" in manual else \
        ctx.categories.categorise(row["merchant"])

    row.update(_amounts(row, amount))
    row["status"], row["review_reason"] = _status(row, amount, latest, unmapped)
    if row["status"] == "ignored" or row["deleted"]:
        row["net_spend_cents"] = 0
        row["counts_as_spending"] = 0
    return row


def _structural_flow(amount: int | None, kind: str) -> str:
    """What the sign and the account type imply, when nothing else said.

    On a credit card the only inflows are payments and refunds, so a positive amount that no
    payment rule claimed is a refund -- close to exhaustive, and it needs no lookback.
    """
    if amount is None:
        return "unknown"
    if amount < 0:
        return "spend"
    if kind == "credit":
        return "refund"
    if kind in ("checking", "savings"):
        return "income"
    return "unknown"


def _amounts(row: dict, amount: int | None) -> dict:
    counts = (row["flow"] in SPENDING_FLOWS and not row["deleted"] and amount is not None)
    return {
        "outflow_cents": max(0, -amount) if amount is not None else None,
        "inflow_cents": max(0, amount) if amount is not None else None,
        "counts_as_spending": int(counts),
        "net_spend_cents": -amount if counts else 0,
    }


def _status(row: dict, amount: int | None, latest, unmapped: bool) -> tuple[str, str | None]:
    if row["deleted"]:
        return "ignored", "deleted"
    if amount is None:
        # Visible and uncounted, never counted as zero. `project._cents`'s rule, one layer out.
        return "needs_review", f"amount unparseable: {latest.get('reject_reason') or 'unknown'}"
    if unmapped:
        return "needs_review", f"{row['native_account']} is not named in accounts.toml"
    if not row["dated_on"]:
        return "needs_review", "no date"
    if row["pending"]:
        return "pending", None
    return "ok", None


# --- the whole projection -----------------------------------------------------------------------

def project_all(groups, corrections: dict, ctx: Context) -> list[dict]:
    rows = [project_one([dict(o) for o in group],
                        [dict(c) for c in corrections.get(
                            txn_key(group[0]["source"], group[0]["native_account"],
                                    group[0]["external_id"]), [])],
                        ctx)
            for group in groups]
    return mark_duplicates(rows)


def mark_duplicates(rows: list[dict]) -> list[dict]:
    """Flag a charge that looks like it arrived twice, from two different sources.

    A reconnected Bridge issues new native ids, and a CSV import can overlap a feed. Neither
    is merged and neither is deleted -- this is a flag, and it is the exact primitive the
    future receipt-to-statement matcher will reuse. Shipping it now means the day you
    reconnect the Bridge is not the day you silently double your March.
    """
    seen: dict[tuple, str] = {}
    for row in sorted(rows, key=lambda r: (r["dated_on"] or "", r["txn_key"])):
        fingerprint = (row["account_key"], row["dated_on"], row["amount_cents"],
                       row["merchant_key"])
        if None in fingerprint[1:3]:
            continue
        if fingerprint in seen and seen[fingerprint] != row["native_account"]:
            row["duplicate_suspect"] = 1
        seen.setdefault(fingerprint, row["native_account"])
    return rows


def account_rows(latest_accounts, txns: list[dict], ctx: Context) -> list[dict]:
    """One row per account key, latest observation wins, with what it actually covers."""
    by_key: dict[str, dict] = {}
    for obs in latest_accounts:
        key, body, unmapped = ctx.accounts.key_for(obs["native_id"])
        row = by_key.setdefault(key, {
            "account_key": key,
            "display_name": body.get("name") or obs["name"] or key,
            "kind": body.get("kind", "other") if not unmapped else "other",
            "institution": body.get("institution") or obs["org_name"],
            "currency": obs["currency"] or "USD",
            "sources": [], "native_ids": [],
            "balance_cents": obs["balance_cents"], "balance_at": obs["balance_at"],
            "last_seen_at": obs["observed_at"],
            "covers_from": None, "covers_to": None,
            "unmapped": int(unmapped),
        })
        if obs["source"] not in row["sources"]:
            row["sources"].append(obs["source"])
        if obs["native_id"] not in row["native_ids"]:
            row["native_ids"].append(obs["native_id"])
        if (obs["observed_at"] or "") >= (row["last_seen_at"] or ""):
            row.update(balance_cents=obs["balance_cents"], balance_at=obs["balance_at"],
                       last_seen_at=obs["observed_at"])

    for txn in txns:
        row = by_key.get(txn["account_key"])
        if row is None or not txn["dated_on"]:
            continue
        row["covers_from"] = min(row["covers_from"] or txn["dated_on"], txn["dated_on"])
        row["covers_to"] = max(row["covers_to"] or txn["dated_on"], txn["dated_on"])

    out = []
    for row in by_key.values():
        row["sources"] = json.dumps(row["sources"])
        row["native_ids"] = json.dumps(row["native_ids"])
        out.append(row)
    return sorted(out, key=lambda r: r["account_key"])
