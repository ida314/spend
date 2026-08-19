"""What the model is asked to return, and what a validated answer looks like.

Two representations of one shape. `RECEIPT_JSON_SCHEMA` is sent to the backend as a
`response_format`, so the grammar is enforced during generation rather than checked
afterwards. The pydantic models are the second gate: a schema constrains structure, not
meaning, and a well-formed object can still carry "THIRTY THREE" where an amount belongs.

Amounts cross this boundary as decimal strings and become integer cents exactly once, in
`money.to_cents`. See that module for why.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, field_validator

PROMPT_VERSION = "receipt_v1"

CATEGORIES = [
    "groceries", "restaurant", "transport", "fuel", "pharmacy",
    "household", "entertainment", "clothing", "services", "other",
]

_MONEY = {"type": ["string", "null"], "description": "decimal string, e.g. \"33.03\""}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": required, "properties": props}


# `strict` json_schema requires every property to be listed in `required`; optionality is
# expressed as a nullable type, not as an absent key. Writing it out this way keeps the
# two lists from drifting apart.
_ITEM_PROPS = {
    "description": {"type": "string"},
    "qty": {"type": ["string", "null"], "description": "count or weight, e.g. \"2\" or \"1.87 LB\""},
    "unit_price": _MONEY,
    "total": _MONEY,
}
_RECEIPT_PROPS = {
    "merchant": {"type": ["string", "null"]},
    "merchant_location": {"type": ["string", "null"]},
    "purchased_on": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
    "currency": {"type": "string", "description": "ISO 4217, default USD"},
    "subtotal": _MONEY,
    "tax": _MONEY,
    "tip": _MONEY,
    "total": _MONEY,
    "category": {"type": ["string", "null"], "enum": [*CATEGORIES, None]},
    "line_items": {"type": "array", "items": _obj(_ITEM_PROPS, list(_ITEM_PROPS))},
}

RECEIPT_JSON_SCHEMA = _obj(_RECEIPT_PROPS, list(_RECEIPT_PROPS))


class LineItem(BaseModel):
    description: str
    qty: str | None = None
    unit_price: str | None = None
    total: str | None = None


class ReceiptData(BaseModel):
    """A validated extraction. Still the model's claim, not yet a transaction."""

    merchant: str | None = None
    merchant_location: str | None = None
    purchased_on: date | None = None
    currency: str = "USD"
    subtotal: str | None = None
    tax: str | None = None
    tip: str | None = None
    total: str | None = None
    category: str | None = None
    line_items: list[LineItem] = Field(default_factory=list)

    @field_validator("purchased_on", mode="before")
    @classmethod
    def _empty_date_is_absent(cls, v):
        # The grammar permits any string here, and a model that cannot find a date
        # sometimes writes "" or "unknown" rather than null. None of those are a date, and
        # inventing one would put a receipt in the wrong month forever.
        if v in (None, "", "null", "unknown", "N/A"):
            return None
        return v

    @field_validator("currency", mode="before")
    @classmethod
    def _currency_default(cls, v):
        return (v or "USD").strip().upper()[:3]

    @field_validator("category", mode="before")
    @classmethod
    def _known_category_only(cls, v):
        # An unlisted category is dropped rather than kept: the rules file and the UI
        # filter both enumerate this list, and a one-off value would be invisible in both.
        if not v:
            return None
        v = str(v).strip().lower()
        return v if v in CATEGORIES else None
