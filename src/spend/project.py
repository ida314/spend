"""Turning what the model said into the row a page shows. Pure: no SQLite, no HTTP.

The rule this module exists to enforce is that a transaction is never stored as a fact.
It is recomputed from three inputs — the extraction, the corrections, and the category
rules — every time any of them changes, and `rebuild` recomputes all of them from scratch.

That is what makes the fallible parts of this system safe to improve. Re-run extraction
with a better prompt and the merchant you fixed by hand stays fixed. Edit the rules file
and eight months of receipts recategorise. Find a bug in how a total is validated, fix it
here, and every historical row is correct — no backfill, no migration, no rows left
carrying the old answer.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from spend.money import MoneyError, to_cents
from spend.schema import ReceiptData

RULES_PATH = Path(__file__).resolve().parent / "rules" / "categories.toml"

# How far the parts may miss the printed total before the row is flagged. Two cents, not
# zero: a receipt with per-item rounding legitimately misses by a cent, and a check that
# fires on every third receipt is a check nobody reads.
TOLERANCE_CENTS = 2


@dataclass(frozen=True)
class Rules:
    """Merchant patterns in file order. First match wins."""

    entries: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @classmethod
    def load(cls, path: Path | None = None) -> Rules:
        raw = tomllib.loads((path or RULES_PATH).read_text())
        return cls(tuple((cat, tuple(body.get("patterns", []))) for cat, body in raw.items()))

    def categorise(self, merchant: str | None) -> str | None:
        if not merchant:
            return None
        m = merchant.casefold()
        for category, patterns in self.entries:
            if any(p.casefold() in m for p in patterns):
                return category
        return None


def project(
    receipt_id: int,
    extractions: list[dict],
    corrections: list[dict],
    rules: Rules,
) -> tuple[dict, list[dict]]:
    """Return (transaction_row, line_item_rows) for one receipt."""
    ok = [e for e in extractions if e["status"] == "ok" and e.get("payload")]
    base = ReceiptData.model_validate(json.loads(ok[-1]["payload"])) if ok else None

    txn: dict = {
        "receipt_id": receipt_id,
        "extraction_id": ok[-1]["id"] if ok else None,
        "merchant": base.merchant if base else None,
        "merchant_location": base.merchant_location if base else None,
        "purchased_on": base.purchased_on.isoformat() if base and base.purchased_on else None,
        "currency": base.currency if base else "USD",
        "subtotal_cents": _cents(base.subtotal if base else None),
        "tax_cents": _cents(base.tax if base else None),
        "tip_cents": _cents(base.tip if base else None),
        "total_cents": _cents(base.total if base else None),
        "category": None,
        "status": "pending",
        "review_reason": None,
        "deleted": 0,
    }

    items = [
        {"description": li.description, "qty": li.qty,
         "unit_cents": _cents(li.unit_price), "total_cents": _cents(li.total)}
        for li in (base.line_items if base else [])
    ]

    model_category = base.category if base else None
    manual_category = False

    # Corrections are applied in the order they were made, so the last edit to a field is
    # the one that survives. A correction against a receipt with no successful extraction
    # is how manual entry works: type the numbers in and the row becomes real.
    for c in corrections:
        field, value = c["field"], c["value"]
        if field == "deleted":
            txn["deleted"] = 1 if value in ("1", "true", "yes") else 0
        elif field == "category":
            txn["category"], manual_category = (value or None), True
        elif field in ("merchant", "merchant_location", "currency"):
            txn[field] = value or None
        elif field == "purchased_on":
            txn["purchased_on"] = _iso_date(value)
        elif field in ("subtotal", "tax", "tip", "total"):
            txn[f"{field}_cents"] = _cents(value)
        elif field.startswith("item."):
            items = _apply_item_correction(items, field, value)

    if not manual_category:
        txn["category"] = rules.categorise(txn["merchant"]) or model_category

    items = [i for i in items if i["description"]]
    txn["status"], txn["review_reason"] = _status(txn, items, extractions)
    return txn, items


def _status(txn: dict, items: list[dict], extractions: list[dict]) -> tuple[str, str | None]:
    """Why a row is or is not trustworthy.

    'needs_review' is not an error state — the row is shown, with its numbers, and stays
    in the totals. It marks the rows worth a human glance, which is a different thing from
    the rows that have no numbers at all.
    """
    has_any = any(txn[k] is not None for k in ("merchant", "total_cents", "purchased_on"))
    if not has_any:
        if not extractions:
            return "pending", "not extracted yet"
        if all(e["status"] != "ok" for e in extractions):
            note = extractions[-1].get("note") or "extraction failed"
            return "failed", note
        return "pending", "no usable extraction"

    missing = [n for n, k in (("merchant", "merchant"), ("total", "total_cents"),
                              ("date", "purchased_on")) if txn[k] is None]
    if missing:
        return "needs_review", f"missing {', '.join(missing)}"

    # The arithmetic check. It compares what the receipt printed against what its own
    # parts add up to, which catches a dropped line, a misread digit, and a tax line
    # mistaken for an item — three different failures with one cheap test.
    item_total = sum(i["total_cents"] for i in items if i["total_cents"] is not None)
    if items and all(i["total_cents"] is not None for i in items):
        parts = item_total + (txn["tax_cents"] or 0) + (txn["tip_cents"] or 0)
        if abs(parts - txn["total_cents"]) > TOLERANCE_CENTS:
            return "needs_review", (
                f"items + tax + tip = {parts / 100:.2f}, receipt says "
                f"{txn['total_cents'] / 100:.2f}")
    return "ok", None


def _apply_item_correction(items: list[dict], field: str, value: str | None) -> list[dict]:
    """`item.<n>.<attr>`, 1-based. Setting a description to empty removes the line."""
    _, n, attr = field.split(".", 2)
    idx = int(n) - 1
    items = [dict(i) for i in items]
    while len(items) <= idx:
        items.append({"description": "", "qty": None, "unit_cents": None, "total_cents": None})
    if attr == "description":
        items[idx]["description"] = value or ""
    elif attr == "qty":
        items[idx]["qty"] = value or None
    elif attr == "unit_price":
        items[idx]["unit_cents"] = _cents(value)
    elif attr == "total":
        items[idx]["total_cents"] = _cents(value)
    return items


def _cents(value) -> int | None:
    # Unparseable text is absence, not zero. A zero here would quietly enter the totals
    # and be indistinguishable from a genuinely free item.
    try:
        return to_cents(value)
    except MoneyError:
        return None


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None
