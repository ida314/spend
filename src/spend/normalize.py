"""Bank descriptions in, comparable merchant names out. Pure: no SQLite, no HTTP.

The rule this module exists to enforce is that the ugly string is never destroyed. The
description a bank wrote is kept verbatim on the row forever, and this produces a *second*
field beside it, so a normalisation bug is always visible and always fixable by editing a
rules file and running `rebuild` -- never by re-fetching from a bank that keeps ninety days.

It exists for two reasons that are really one reason. The category rules match substrings
against a merchant name, and `SQ *BLUE BOTTLE COFFEE 00291 BROOKLYN NY` does not contain
"blue bottle" in any form the human who wrote that rule would recognise. And a statement row
and a photographed receipt cannot be compared at all until they agree on what to call the
shop -- which is the whole reason both streams end up in one workspace.

    "SQ *BLUE BOTTLE COFFEE   BROOKLYN NY"      -> "Blue Bottle Coffee"
    "TST* DIME SQUARE - BROO 8005551234"        -> "Dime Square"
    "AMZN Mktp US*RT4G91OJ3"                    -> "Amazon"
    "POS DEBIT 09/12 KEY FOOD #1188 BROOKLYN"   -> "Key Food"
    "CAPITAL ONE      AUTOPAY PYMT"             -> "Capital One Autopay"
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

RULES_DIR = Path(__file__).resolve().parent / "rules"
MERCHANTS_PATH = RULES_DIR / "merchants.toml"
FLOWS_PATH = RULES_DIR / "flows.toml"

# Payment processors and point-of-sale prefixes. These say who moved the money, not who was
# paid, and leaving them in front of the name is what makes every Square merchant look like
# the same merchant to a substring rule.
_PREFIXES = re.compile(
    r"^(?:"
    r"sq\s*\*|tst\*|tst\s*\*|py\s*\*|paypal\s*\*|sp\s+|ic\*\s*|dd\s*\*|ext\s*\*|"
    r"pos\s+debit|pos\s+purchase|debit\s+card\s+purchase|checkcard|visa\s+dda\s+pur|"
    r"purchase\s+authorized\s+on|recurring\s+payment|web\s+authorized\s+pmt|"
    r"aplpay\s*|apple\s+pay\s*|gpay\s*\*?|sumup\s*\*|toasttab\s*\*?|clover\s*\*?|"
    r"www\.|https?://"
    r")\s*", re.I)

# A date the processor stamped into the description, e.g. "09/12" right after the prefix.
_LEADING_DATE = re.compile(r"^\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\s+")

# Trailing noise: store numbers, phone numbers, masked cards, long reference digits, and the
# city + two-letter state tail every card network appends.
_TRAILERS = (
    re.compile(r"\s+#\s*\d+\s*$"),
    re.compile(r"\s+\d{3}[- ]?\d{3}[- ]?\d{4}\s*$"),      # a phone number
    re.compile(r"\s+x{2,}\d+\s*$", re.I),                  # xxxx1234
    re.compile(r"\s+[a-z0-9]*\d[a-z0-9]{5,}\s*$", re.I),   # a reference like RT4G91OJ3
    re.compile(r"\s+\d{4,}\s*$"),                          # a bare store number
    re.compile(r"[\s,]+[a-z .]+\s+[a-z]{2}\s*$", re.I),    # "  BROOKLYN NY"
    re.compile(r"\s+usa?\s*$", re.I),
    re.compile(r"\s+\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\s*$"),
)

_SEPARATORS = re.compile(r"[*_|]+")
_SPACE = re.compile(r"\s+")

# Words that are already the shop's own name in caps, and would look wrong title-cased.
_KEEP_UPPER = {"usa", "us", "nyc", "mta", "cvs", "amc", "rei", "h&m", "bp", "ups", "dsw",
               "kfc", "ihop", "att", "hbo", "ikea", "aaa", "tj"}
_KEEP_LOWER = {"of", "the", "and", "at", "on", "in", "for", "de", "la", "le"}


@dataclass(frozen=True)
class Aliases:
    """Canonical name -> the ugly fragments that mean it. First match wins."""

    entries: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @classmethod
    def load(cls, path: Path | None = None) -> Aliases:
        raw = tomllib.loads((path or MERCHANTS_PATH).read_text())
        return cls(tuple((name, tuple(pats)) for name, pats in raw.items()))

    def canonical(self, text: str) -> str | None:
        # Whitespace-collapsed first. Banks pad descriptions into fixed-width columns, so
        # "CAPITAL ONE      AUTOPAY PYMT" does not contain "capital one autopay" as a
        # substring at all, and the alias file would look broken for a reason nobody would
        # guess from reading it.
        low = _SPACE.sub(" ", text.casefold()).strip()
        for name, patterns in self.entries:
            if any(p.casefold() in low for p in patterns):
                return name
        return None


@dataclass(frozen=True)
class Flows:
    """Description patterns -> flow. Matched against the RAW description, deliberately.

    Normalisation is built to destroy exactly the words this test needs: "AUTOPAY PYMT" and
    "TRANSFER TO" are trailing noise to a merchant name and are the entire signal here. So
    this runs first, on the untouched string.
    """

    entries: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @classmethod
    def load(cls, path: Path | None = None) -> Flows:
        raw = tomllib.loads((path or FLOWS_PATH).read_text())
        return cls(tuple((flow, tuple(body.get("patterns", [])))
                         for flow, body in raw.items()))

    def classify(self, description: str) -> str | None:
        low = _SPACE.sub(" ", (description or "").casefold()).strip()
        for flow, patterns in self.entries:
            if any(p.casefold() in low for p in patterns):
                return flow
        return None


def strip(description: str) -> str:
    """The mechanical part: prefixes off the front, noise off the back."""
    text = _SPACE.sub(" ", (description or "").strip())
    for _ in range(3):                       # "POS DEBIT 09/12 KEY FOOD" needs two passes
        stripped = _PREFIXES.sub("", text)
        stripped = _LEADING_DATE.sub("", stripped)
        if stripped == text:
            break
        text = stripped.strip()
    text = _SEPARATORS.sub(" ", text)
    text = _SPACE.sub(" ", text).strip(" -,")
    for _ in range(3):                       # a store number *and* a city tail
        shorter = text
        for pattern in _TRAILERS:
            shorter = pattern.sub("", shorter).strip(" -,")
        if shorter == text or not shorter:
            break
        text = shorter
    return _SPACE.sub(" ", text).strip(" -,")


def titlecase(text: str) -> str:
    out = []
    for i, word in enumerate(text.split()):
        low = word.casefold()
        if low in _KEEP_UPPER:
            out.append(word.upper())
        elif low in _KEEP_LOWER and i:
            out.append(low)
        elif "'" in word:                    # Trader Joe's, not Trader Joe'S
            head, _, tail = word.partition("'")
            out.append(head.capitalize() + "'" + tail.lower())
        elif "-" in word:                    # 7-Eleven, Coca-Cola
            out.append("-".join(part.capitalize() for part in word.split("-")))
        else:
            out.append(word.capitalize())
    return " ".join(out)


def merchant(description: str, aliases: Aliases | None = None) -> str | None:
    """The display name. `None` when there is nothing left to call it."""
    if not description or not description.strip():
        return None
    aliases = aliases if aliases is not None else _aliases()
    if name := aliases.canonical(description):
        return name                          # the alias file wins; it was written by hand
    cleaned = strip(description)
    if not cleaned or not any(c.isalpha() for c in cleaned):
        return None
    return titlecase(cleaned)


def key(name: str | None) -> str | None:
    """The joinable form. Casefolded, punctuation-light, whitespace-collapsed.

    This is what a receipt merchant and a statement merchant are compared on, so it has to
    survive an apostrophe the model did or did not type.
    """
    if not name:
        return None
    folded = _SPACE.sub(" ", re.sub(r"[^\w\s]", "", name.casefold())).strip()
    return folded or None


@lru_cache(maxsize=1)
def _aliases() -> Aliases:
    return Aliases.load()
