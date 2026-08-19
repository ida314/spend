"""Money, as integer cents, and the only place a string becomes one.

No float ever touches a monetary value in this project. `0.1 + 0.2` is the canonical
demonstration of why, and a spending tracker that is a cent out on a third of its rows is
worse than useless — it is confidently wrong, which is the failure this codebase is
otherwise built to avoid.

The model is asked for decimal strings for the same reason. Asking it for cents directly
means asking it to multiply by 100 on every field, and it will occasionally get that
wrong in a way no schema can catch; asking it to copy the digits it can see is a task it
cannot fail at silently.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# What OCR and receipts actually produce: a currency symbol, thousands separators, a
# trailing minus, or a parenthesised negative for a refund line.
_CLEAN = re.compile(r"[^\d.,\-()]")
_TRAILING_MINUS = re.compile(r"^(.*?)-$")


class MoneyError(ValueError):
    """The text was not a monetary amount. Callers treat this as absence, never as zero."""


def to_cents(text: str | int | None) -> int | None:
    """Parse a receipt's rendering of an amount into integer cents.

    Returns None for None or empty input. Raises MoneyError on text that is present but
    unparseable, because "the model wrote something we don't understand" and "the receipt
    didn't say" are different facts and the projection treats them differently.
    """
    if text is None:
        return None
    if isinstance(text, int):
        return text
    s = str(text).strip()
    if not s:
        return None

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    s = _CLEAN.sub("", s).strip()
    if m := _TRAILING_MINUS.match(s):
        negative, s = True, m.group(1)
    if s.startswith("-"):
        negative, s = True, s[1:]
    s = s.replace("(", "").replace(")", "")

    if not s:
        raise MoneyError(f"no digits in {text!r}")

    # Separators are ambiguous, and the ambiguity is decidable from position rather than
    # locale. Whichever separator comes last is the decimal point — a grouping mark can
    # never be the last one — with a single exception: one comma with exactly three digits
    # after it is "1,234", a grouped thousand, not a millicent amount.
    #
    # The three-digit case is not hypothetical. Receipts print per-unit prices to a tenth
    # of a cent ("2 @ 0.745"), and reading that as 74500 cents put $745.00 of bananas in
    # the first receipt this parser ever saw.
    last = max(s.rfind("."), s.rfind(","))
    if last == -1:
        whole, frac = s, "00"
    elif s[last] == "," and len(s) - last - 1 == 3 and s.count(",") + s.count(".") == 1:
        whole, frac = s.replace(",", ""), "00"
    else:
        whole, frac = s[:last], s[last + 1:]
    whole = whole.replace(".", "").replace(",", "") or "0"
    if not whole.isdigit() or not frac.isdigit():
        raise MoneyError(f"not an amount: {text!r}")

    # Quantised rather than truncated, and only ever here. A tenth of a cent has nowhere
    # to live in an integer-cent column; a unit price is shown, never summed, and the
    # item's own total — which the receipt prints in whole cents — stays exact.
    try:
        amount = Decimal(whole) + Decimal(frac) / (10 ** len(frac))
        cents = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:  # pragma: no cover - guarded by isdigit above
        raise MoneyError(f"not an amount: {text!r}") from exc
    return -cents if negative else cents


def format_cents(cents: int | None, currency: str = "USD") -> str:
    """Render for a page. Display only — never parsed back."""
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    return f"{sign}{symbol}{whole:,}.{frac:02d}"
