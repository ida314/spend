"""The append-only encrypted log. The only thing in this system that must survive.

The rule this module exists to enforce is that a file under `log/` is created once, by
rename, and is never reopened for write, truncated, renamed over, or unlinked. A test traces
that at the interpreter's audit layer, the same way `test_invariants` traces SQL.

Named `ledger` rather than `log` because `service`, `worker` and `web.api` each bind
`log = logging.getLogger(__name__)` at module scope, and `from spend import log` would
shadow it in whichever imported it first. The directory on disk stays `log/`; the README
already calls it that.

## One file per event

Not one segment per month, and the constraint is load-bearing rather than aesthetic.
Appending to a segment means decrypting it first, which would put the private key on a timer
that runs while you are asleep. One file per event means `seal()` -- which needs only public
keys -- is the entire write path, so the nightly bank sync can append all night and remain
structurally incapable of reading a single historical transaction.

## The name is the hash of what the record *is*, not of when it arrived

`sha256` over the canonical JSON of `{v, kind, body}`. Two things are deliberately outside
that hash and travel in `aside` instead: `at`, and provenance like which poll saw a
transaction. Both describe the *observation*, not the thing observed, and putting either in
the digest would mean a nightly poll re-sealed every transaction it re-delivered -- turning
the one property this design leans on hardest into its opposite. Excluding them makes
re-appending an identical record a byte-level no-op: `seal_to` sees the file already there
and returns. It is what makes SimpleFIN's recommended overlapping windows affordable
-- re-polling thirty days every night for a year writes nothing after the first pass -- and
it means the timestamp that persists is the first observation's, which is exactly what
`first_seen_at` should mean.

A pending charge that posts is *not* a no-op, correctly: its body differs, so it hashes
differently and lands as a second file. Both survive and the projection takes the newer. An
UPDATE, without one.

## If this ever gets slow

Replay decrypts every file. At three events per receipt plus one per feed observation that
is seconds, not minutes, and the escape hatch when it stops being seconds is a sealed
`snapshot/<n>.age` holding every record up to sequence n, written during an unlocked session
-- when the key is present anyway, so it does not violate the property above -- with replay
reading the newest snapshot plus the events after it. Do not build that until the numbers
say so.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from spend import paths, seal

VERSION = 1

KINDS = ("receipt", "extraction", "correction",
         "feed_poll", "feed_account", "feed_record", "feed_correction")


class LedgerCorrupt(RuntimeError):
    """A sealed file opened but is not the record its name claims."""


def now() -> str:
    """UTC, ISO 8601, microseconds. Ties in replay order are broken by the digest."""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Record:
    sha: str
    kind: str
    at: str
    body: dict
    aside: dict = field(default_factory=dict)
    path: Path | None = None

    @property
    def order(self) -> tuple[str, str]:
        """Total and deterministic: the same set of files always replays the same way."""
        return (self.at, self.sha)

    @property
    def fields(self) -> dict:
        """Everything the record carries. What `replay.apply` writes into the cache."""
        return {**self.body, **self.aside}


def canonical(payload: dict) -> bytes:
    """Stable across processes, Python versions and dict insertion order.

    The filename is the hash of exactly these bytes, so this cannot be `json.dumps` with
    default arguments and cannot ever change without orphaning every file already written.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


def digest(kind: str, body: dict) -> str:
    return hashlib.sha256(canonical({"v": VERSION, "kind": kind, "body": body})).hexdigest()


def append(kind: str, body: dict, *, at: str | None = None, aside: dict | None = None,
           recipients: list | None = None) -> tuple[Record, bool]:
    """Seal one event into the log. Returns (record, is_new).

    `body` is what the record is and decides its name. `aside` is what is true about the
    observation rather than about the thing -- which poll saw it, and when -- and is sealed
    alongside without entering the digest, so a re-observation seals nothing.

    Needs only the recipients, never the identity. That is the whole point.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown record kind {kind!r}")
    sha = digest(kind, body)
    rec = Record(sha=sha, kind=kind, at=at or now(), body=body, aside=aside or {},
                 path=paths.log_path(sha))
    payload = {"v": VERSION, "kind": rec.kind, "at": rec.at, "body": rec.body,
               "aside": rec.aside}
    is_new = seal.seal_to(rec.path, canonical(payload), recipients)
    return rec, is_new


def iter_paths(root: Path | None = None) -> Iterator[Path]:
    """Every sealed event, in a stable order. Needs no key."""
    root = root or paths.log_dir()
    if not root.exists():
        return
    for shard in sorted(p for p in root.iterdir() if p.is_dir()):
        yield from sorted(p for p in shard.iterdir() if p.suffix == ".age")


def count(root: Path | None = None) -> int:
    return sum(1 for _ in iter_paths(root))


def read(path: Path, identity) -> Record:
    """Decrypt one event and check it is the record its filename claims.

    Two independent integrity checks, and they catch different things: age's own AEAD tag
    catches a flipped bit, and this catches a file that was renamed, swapped, or written by
    a version of `canonical` that no longer agrees with this one.
    """
    payload = json.loads(seal.open_file(path, identity))
    kind, body, at = payload.get("kind"), payload.get("body"), payload.get("at")
    aside = payload.get("aside") or {}
    if not isinstance(body, dict) or kind not in KINDS:
        raise LedgerCorrupt(f"{path.name} is not a {__name__} record")
    sha = digest(kind, body)
    if sha != path.stem:
        raise LedgerCorrupt(f"{path.name} contains a record that hashes to {sha[:12]}…")
    return Record(sha=sha, kind=kind, at=at, body=body, aside=aside, path=path)


def events(identity, root: Path | None = None) -> tuple[list[Record], list[tuple[Path, str]]]:
    """Every record, replay-ordered, plus the ones that would not open.

    A corrupt file is collected and skipped, never raised. The entire reason for one file per
    event is that 29,999 good records survive one bad one; aborting the replay would throw
    away that property at the only moment it matters.
    """
    good: list[Record] = []
    bad: list[tuple[Path, str]] = []
    for path in iter_paths(root):
        try:
            good.append(read(path, identity))
        except (seal.SealError, LedgerCorrupt, ValueError) as exc:
            bad.append((path, str(exc)))
    good.sort(key=lambda r: r.order)
    return good, bad
