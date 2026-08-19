"""Fixtures. No mock framework: a fake here is a small class that does the real thing
badly on purpose, which is easier to read than a recorded call list and cannot drift out
of step with the protocol it stands in for.
"""

from __future__ import annotations

import io
import pytest
from PIL import Image, ImageDraw

from spendtracker.extract.base import Extraction
from spendtracker.render import Document
from spendtracker.schema import ReceiptData


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Point the whole app at a scratch directory.

    Autouse and not optional: a test that forgot this would write into the real database
    in ~/.local/share, and the failure mode is silent.
    """
    monkeypatch.setenv("SPENDTRACKER_HOME", str(tmp_path / "st"))
    return tmp_path / "st"


@pytest.fixture
def conn(home):
    from spendtracker import paths, store
    paths.ensure_dirs()
    c = store.connect()
    store.migrate(c)
    yield c
    c.close()


@pytest.fixture
def jpeg_bytes() -> bytes:
    img = Image.new("RGB", (400, 600), "white")
    d = ImageDraw.Draw(img)
    d.text((20, 20), "TEST MART\nTOTAL 12.34", fill="black")
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


class FakeExtractor:
    """Returns whatever it was handed. `model`/`prompt_version` mirror the real one."""

    model = "fake-model"
    prompt_version = "test_v1"

    def __init__(self, *results: Extraction):
        self.results = list(results)
        self.calls: list[Document] = []

    async def extract(self, doc: Document) -> Extraction:
        self.calls.append(doc)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def ok(**kw) -> Extraction:
    data = ReceiptData.model_validate({
        "merchant": kw.pop("merchant", "TEST MART"),
        "purchased_on": kw.pop("purchased_on", "2026-08-14"),
        "total": kw.pop("total", "12.34"),
        "tax": kw.pop("tax", None),
        "line_items": kw.pop("line_items", []),
        **kw,
    })
    return Extraction(status="ok", raw_response=data.model_dump_json(), data=data,
                      latency_ms=1)


@pytest.fixture
def fake_ok():
    return FakeExtractor(ok())
