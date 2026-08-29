"""The identity, and whether the store is open.

The rule this module exists to enforce is that the identity exists in exactly one place at a
time, and that place is never a filesystem that survives a reboot.

That place is a 0600 file inside a 0700 directory on tmpfs, and not the kernel keyring. The
argument is one sentence: the threat this design defends against is the disk at rest --
theft, RMA, resale, a btrfs snapshot -- and against a process running as this uid a key file
adds no exposure whatsoever, because the fully decrypted SQLite cache is sitting in the same
directory. A keyring would buy unswappability for thirty-two bytes next to a twenty-megabyte
swappable cache, at the cost of a hand-rolled syscall wrapper no test can exercise and which
is unreachable from the container's user namespace. A key-holding daemon under `mlockall` is
the right answer for a system with no plaintext cache; this system has one by construction.
Revisit both the day the cache moves back onto disk.

There is no headless-boot mode and no stored passphrase, deliberately. A passphrase
recoverable from the disk is not a passphrase. If the box reboots at three in the morning,
receipts queue and the bank sync keeps appending; the web app returns when a human types it.
"""

from __future__ import annotations

import fcntl
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from spend import paths, seal
from spend.branding import NAME
from spend.seal import Locked

__all__ = ["Locked", "AlreadyInitialised", "init", "unlock", "lock", "is_unlocked",
           "require_identity", "identity_or_none", "expires_at", "exclusive", "shared"]

MIN_PASSPHRASE = 12


class AlreadyInitialised(RuntimeError):
    """There is already an identity here, and overwriting it would orphan the log."""


class Busy(RuntimeError):
    """Another process holds the store."""


def check_passphrase(phrase: str) -> None:
    """The passphrase is the security of the entire system.

    scrypt's work factor makes a strong one expensive to attack and does exactly nothing for
    a weak one, so the check belongs here rather than in a docstring nobody reads.
    """
    if len(phrase) < MIN_PASSPHRASE:
        raise ValueError(f"a passphrase needs at least {MIN_PASSPHRASE} characters")
    if phrase.isdigit():
        raise ValueError("a passphrase of digits only is a PIN; use words")


def init(phrase: str, extra_recipients: list[str] | None = None) -> tuple[str, str]:
    """Create the identity and the recipients file. Returns (public, backup_secret).

    Two identities, not one, and the second is not optional-by-default for a reason: there
    is no recovery here, no backdoor and no support line, and a forgotten passphrase
    destroys everything. The backup identity's secret is printed once and belongs on paper.
    It costs about fifty bytes per sealed file.
    """
    check_passphrase(phrase)
    if paths.identity_file().exists():
        raise AlreadyInitialised(
            f"{paths.identity_file()} already exists. Generating a new identity would "
            f"orphan every sealed file in {paths.log_dir()} with no way to read them back.")

    paths.config_dir().mkdir(parents=True, exist_ok=True)
    secret, public = seal.generate()
    backup_secret, backup_public = seal.generate()

    lines = ["# age recipients. Public keys: not secret, and readable while locked is the",
             "# point -- this is what lets the bank sync append data it cannot read.",
             public,
             "# The paper backup. Its secret was printed once by `spend init`.",
             backup_public]
    lines += [k.strip() for k in (extra_recipients or []) if k.strip()]

    _write_private(paths.recipients_file(), ("\n".join(lines) + "\n").encode(), mode=0o644)
    _write_private(paths.identity_file(), seal.wrap(secret, phrase), mode=0o600)
    return public, backup_secret


def _write_private(dest: Path, data: bytes, *, mode: int) -> None:
    tmp = dest.with_name(dest.name + ".part")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    tmp.rename(dest)


# --- lock state --------------------------------------------------------------------------

def is_unlocked() -> bool:
    try:
        if not paths.identity_runtime_file().exists():
            return False
    except paths.NoRuntimeDir:
        return False
    return not _expired()


def expires_at() -> datetime | None:
    try:
        raw = paths.expires_file().read_text().strip()
    except (OSError, paths.NoRuntimeDir):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _expired() -> bool:
    when = expires_at()
    return when is not None and datetime.now(UTC) >= when


def identity_or_none():
    return require_identity() if is_unlocked() else None


def require_identity():
    """The identity, or `Locked`. The single gate every reader passes through.

    Checks the deadline as well as the file, so a TTL that has passed locks the store even
    if the relock timer never fired -- a timer that dies with the process it protects is not
    a timer, so this is the authority and `systemd-run` is only the enforcer.
    """
    try:
        path = paths.identity_runtime_file()
    except paths.NoRuntimeDir as exc:
        raise Locked(str(exc)) from exc
    if not path.exists():
        raise Locked("the store is locked; run `spend unlock`")
    if _expired():
        lock()
        raise Locked("the unlock window expired; run `spend unlock`")
    return seal.load_identity(path.read_text())


def unlock(phrase: str, ttl: timedelta) -> None:
    """Decrypt the identity into tmpfs and arm the deadline. Does not replay."""
    try:
        wrapped = paths.identity_file().read_bytes()
    except OSError as exc:
        raise seal.SealError(
            f"no identity at {paths.identity_file()}; run `spend init` first") from exc
    secret = seal.unwrap(wrapped, phrase)          # raises SealError on a wrong passphrase
    seal.load_identity(secret)                     # and this proves it is really an identity

    paths.ensure_runtime()
    _write_private(paths.identity_runtime_file(), secret.encode(), mode=0o600)
    deadline = (datetime.now(UTC) + ttl).isoformat()
    _write_private(paths.expires_file(), deadline.encode(), mode=0o600)


def lock() -> None:
    """Forget the identity and everything derived from it.

    No flush step, and that is the payoff of appending to the log before writing the cache:
    nothing can exist in the cache and not in the log, so this is `rm -rf` with nothing at
    risk. Same ordering rule `service.ingest_bytes` already states about the file and the row.

    The identity's bytes are overwritten before the unlink. On tmpfs that genuinely clears
    the page -- unless it has been swapped, in which case the swapped copy is beyond reach.
    Do it anyway; it is not a guarantee, which is why encrypted swap is a prerequisite and
    why `doctor` checks for it.
    """
    try:
        root = paths.runtime_dir()
    except paths.NoRuntimeDir:
        return
    ident = paths.identity_runtime_file()
    try:
        if ident.exists():
            with open(ident, "r+b") as fh:
                n = fh.seek(0, os.SEEK_END)
                fh.seek(0)
                fh.write(b"\0" * n)
                fh.flush()
                os.fsync(fh.fileno())
    except OSError:
        pass
    shutil.rmtree(root, ignore_errors=True)


# --- the flock ---------------------------------------------------------------------------
# Not decoration. `unlock` deletes the database, and a live process holding that file ends up
# with a deleted inode and loses its writes with no error at all. This is what turns the
# README's "never run both deploy paths at once" into a loud failure instead of a silent one.

@contextmanager
def _flock(how: int) -> Iterator[None]:
    paths.ensure_runtime()
    path = paths.lock_file()
    with open(path, "a+b") as fh:
        try:
            fcntl.flock(fh.fileno(), how | fcntl.LOCK_NB)
        except OSError as exc:
            raise Busy(
                f"another {NAME} process holds {path}. Stop the service (or the "
                f"container) and try again.") from exc
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive() -> Iterator[None]:
    """Held by `unlock` and `lock`, which delete the cache out from under everyone."""
    with _flock(fcntl.LOCK_EX):
        yield


@contextmanager
def shared() -> Iterator[None]:
    """Held for its lifetime by anything long-lived: `serve`, the worker, a sync."""
    with _flock(fcntl.LOCK_SH):
        yield
