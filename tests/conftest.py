"""Fixtures. No mock framework: a fake here is a small class that does the real thing
badly on purpose, which is easier to read than a recorded call list and cannot drift out
of step with the protocol it stands in for.
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image, ImageDraw

from spend.extract.base import Extraction
from spend.render import Document
from spend.schema import ReceiptData


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Point the whole app at a scratch directory.

    Autouse and not optional: a test that forgot this would write into the real database
    in ~/.local/share, and the failure mode is silent.
    """
    monkeypatch.setenv("SPEND_HOME", str(tmp_path / "st"))
    return tmp_path / "st"


@pytest.fixture
def unlocked(home):
    """An initialised, unlocked store in the scratch tree.

    The identity is written to the runtime directory directly rather than through
    `keys.init` + `keys.unlock`. That is not a shortcut around the real code path -- exactly
    one test walks it end to end -- it is because `passphrase.encrypt` is scrypt at rage's
    own work factor, about a second, and paying that in every test that touches the database
    would put a minute on the suite for no coverage.
    """
    from spend import keys, paths, seal
    paths.ensure_dirs()
    paths.ensure_runtime()
    secret, public = seal.generate()
    paths.recipients_file().write_text(public + "\n")
    paths.identity_runtime_file().write_text(secret)
    paths.expires_file().write_text(
        (datetime.now(UTC) + timedelta(hours=1)).isoformat())
    return keys.require_identity()


@pytest.fixture
def conn(unlocked):
    from spend import store
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


# --- the file-level append-only watch ----------------------------------------------------
# Installed once, at import, because an audit hook cannot be uninstalled. It is inert until
# `watch_files` arms it. The hook itself does no I/O of any kind -- it only appends to a list
# -- because a stat or an open inside an audit hook is how you write an infinite recursion.
# Whether a path "already existed" is therefore decided afterwards, against a snapshot taken
# when the watch was armed.

_WATCH: dict = {}


def _audit(event: str, args: tuple) -> None:
    if not _WATCH:
        return
    try:
        if event == "open":
            path, mode, flags = args
            writing = bool(mode and set(str(mode)) & set("wax+")) or bool(
                flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_TRUNC))
            if writing and path is not None:
                _WATCH["seen"].append(("write-open", str(path)))
        elif event == "os.remove":
            _WATCH["seen"].append(("unlink", str(args[0])))
        elif event == "os.truncate":
            _WATCH["seen"].append(("truncate", str(args[0])))
        elif event == "os.link":
            _WATCH["seen"].append(("link", str(args[1])))
        elif event == "os.rename":
            _WATCH["seen"].append(("rename-onto", str(args[1])))
    except Exception:  # noqa: BLE001 - an audit hook must never take the process down
        pass


sys.addaudithook(_audit)


@contextmanager
def watch_files(*roots):
    """Collect every rewrite, unlink or rename-over of a file that already existed.

    Creating a new file under `log/` is how an append works; touching one that is already
    there is the violation. That distinction is the whole rule, so the snapshot is what the
    test is really asserting against.
    """
    before = {str(p) for root in roots for p in root.rglob("*") if p.is_file()}
    found: list[str] = []
    _WATCH.clear()
    _WATCH["seen"] = []
    try:
        yield found
    finally:
        found.extend(
            f"{what} {path}" for what, path in _WATCH["seen"]
            if path in before and any(path.startswith(str(r)) for r in roots))
        _WATCH.clear()


# --- feed fakes ---------------------------------------------------------------------------
# Real-shaped exports, written by hand. A fake here does the real thing badly on purpose;
# these do the real thing correctly, which is what makes a sign test worth anything.

CAPITALONE_CREDIT_HEADER = (
    "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit")
CAPITALONE_CHECKING_HEADER = (
    "Account Number,Transaction Date,Transaction Amount,Transaction Type,"
    "Transaction Description,Balance")
APPLECARD_HEADER = (
    "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD)")


def capitalone_csv(*rows: str) -> str:
    return CAPITALONE_CREDIT_HEADER + "\n" + "\n".join(rows) + "\n"


def checking_csv(*rows: str) -> str:
    return CAPITALONE_CHECKING_HEADER + "\n" + "\n".join(rows) + "\n"


def applecard_csv(*rows: str) -> str:
    return APPLECARD_HEADER + "\n" + "\n".join(rows) + "\n"


def ofx(*trns: str) -> str:
    body = "".join(trns)
    return ("OFXHEADER:100\n<OFX><BANKACCTFROM><ACCTID>123456784471</ACCTID>"
            f"</BANKACCTFROM>{body}</OFX>")


def stmttrn(fitid: str, posted: str, amount: str, name: str, trntype: str = "DEBIT") -> str:
    return (f"<STMTTRN><TRNTYPE>{trntype}</TRNTYPE><DTPOSTED>{posted}</DTPOSTED>"
            f"<TRNAMT>{amount}</TRNAMT><FITID>{fitid}</FITID><NAME>{name}</NAME></STMTTRN>")


class FakeSource:
    """Hands back canned Polls, in order. Mirrors the real adapters' total contract."""

    def __init__(self, *polls):
        self.polls = list(polls)
        self.windows: list = []

    def poll(self, window):
        self.windows.append(window)
        return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]


@pytest.fixture
def accounts_toml(home):
    """The two Capital One accounts, named the way a real accounts.toml names them."""
    from spend import paths
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.accounts_rules_file().write_text('''
[cap1-quicksilver]
name        = "Capital One Quicksilver"
kind        = "credit"
institution = "Capital One"
native      = ["drop:capitalone-credit:7734", "ACT-quicksilver"]

[cap1-checking]
name        = "Capital One 360 Checking"
kind        = "checking"
institution = "Capital One"
native      = ["drop:capitalone-checking:4471", "ACT-checking"]

[apple-card]
name        = "Apple Card"
kind        = "credit"
institution = "Apple"
native      = ["drop:applecard:apple-card"]
'''.lstrip())
    from spend import feed
    return feed.Accounts.load()


@pytest.fixture
def drop_file(home):
    """Write an export into the inbox and hand back its path."""
    from spend import paths

    def write(name: str, text: str):
        root = paths.drop_dir() / "inbox"
        root.mkdir(parents=True, exist_ok=True)
        path = root / name
        path.write_text(text)
        return path
    return write


# --- feed fakes ---------------------------------------------------------------------------
# Real-shaped exports, written by hand. A fake here does the real thing badly on purpose;
# these do the real thing correctly, which is what makes a sign test worth anything.

CAPITALONE_CREDIT_HEADER = (
    "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit")
CAPITALONE_CHECKING_HEADER = (
    "Account Number,Transaction Date,Transaction Amount,Transaction Type,"
    "Transaction Description,Balance")
APPLECARD_HEADER = (
    "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD)")


def capitalone_csv(*rows: str) -> str:
    return CAPITALONE_CREDIT_HEADER + "\n" + "\n".join(rows) + "\n"


def checking_csv(*rows: str) -> str:
    return CAPITALONE_CHECKING_HEADER + "\n" + "\n".join(rows) + "\n"


def applecard_csv(*rows: str) -> str:
    return APPLECARD_HEADER + "\n" + "\n".join(rows) + "\n"


def ofx(*trns: str) -> str:
    body = "".join(trns)
    return ("OFXHEADER:100\n<OFX><BANKACCTFROM><ACCTID>123456784471</ACCTID>"
            f"</BANKACCTFROM>{body}</OFX>")


def stmttrn(fitid: str, posted: str, amount: str, name: str, trntype: str = "DEBIT") -> str:
    return (f"<STMTTRN><TRNTYPE>{trntype}</TRNTYPE><DTPOSTED>{posted}</DTPOSTED>"
            f"<TRNAMT>{amount}</TRNAMT><FITID>{fitid}</FITID><NAME>{name}</NAME></STMTTRN>")


class FakeSource:
    """Hands back canned Polls, in order. Mirrors the real adapters' total contract."""

    def __init__(self, *polls):
        self.polls = list(polls)
        self.windows: list = []

    def poll(self, window):
        self.windows.append(window)
        return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]


@pytest.fixture
def accounts_toml(home):
    """The three accounts, named the way a real accounts.toml names them."""
    from spend import feed, paths
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.accounts_rules_file().write_text('''
[cap1-quicksilver]
name        = "Capital One Quicksilver"
kind        = "credit"
institution = "Capital One"
native      = ["drop:capitalone-credit:7734", "ACT-quicksilver"]

[cap1-checking]
name        = "Capital One 360 Checking"
kind        = "checking"
institution = "Capital One"
native      = ["drop:capitalone-checking:4471", "ACT-checking"]

[apple-card]
name        = "Apple Card"
kind        = "credit"
institution = "Apple"
native      = ["drop:applecard:apple-card", "drop:ofx:4471"]
'''.lstrip())
    return feed.Accounts.load()


@pytest.fixture
def drop_file(home):
    """Write an export into the inbox and hand back its path."""
    from spend import paths

    def write(name: str, text: str):
        root = paths.drop_dir() / "inbox"
        root.mkdir(parents=True, exist_ok=True)
        path = root / name
        path.write_text(text)
        return path
    return write
