"""The seam between "a receipt was rendered" and "a model read it".

One protocol, so the backend is replaceable and so tests can supply an extractor that
fails on purpose. The contract is deliberately total: `extract` returns an `Extraction`
describing what happened, and never raises. A model that is down, slow, misconfigured or
returning nonsense costs a receipt the answer it did not have before; it must never be
able to produce a wrong one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from spend.render import Document
from spend.schema import ReceiptData


@dataclass(frozen=True)
class Extraction:
    """The outcome of one attempt. `status` mirrors the extractions table.

    'ok'      — validated, `data` is set
    'invalid' — the model answered, the answer was not a receipt we could parse
    'error'   — no usable answer came back at all
    """

    status: str
    raw_response: str | None = None
    data: ReceiptData | None = None
    note: str | None = None
    latency_ms: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class Extractor(Protocol):
    async def extract(self, doc: Document) -> Extraction: ...
