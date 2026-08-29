"""age, and the only module that knows it.

The rule this module exists to enforce is that no byte leaves it for a path under
`data_dir()` except as age ciphertext. Nothing above this line imports pyrage, and nothing
below it knows what a receipt is.

The asymmetry is the point of the whole design. `seal()` needs only the recipients -- public
keys, sitting in a world-readable file -- so a process holding nothing secret can append to
the log all night. `unseal()` needs the identity, which exists only between `spend unlock`
and `spend lock`. That is what lets the bank sync run while the store is locked and be
structurally incapable of reading a single historical transaction, rather than merely
choosing not to.

`seal_to()` writes through a `.part` and renames. It refuses to overwrite, because a file
under `log/` or `blobs/` is created once and never touched again, and the cheapest place to
enforce that is the one function that creates them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pyrage
from pyrage import passphrase, x25519

from spend import paths

# What every age file starts with. Used by the tests that walk the data root asserting
# nothing plaintext is in it, and by `verify` to tell a truncated file from a foreign one.
MAGIC = b"age-encryption.org/v1"


class SealError(RuntimeError):
    """Encryption or decryption failed. Wraps pyrage so callers import one exception."""


class Locked(RuntimeError):
    """An identity was needed and the store is locked."""


# --- keys ------------------------------------------------------------------------------

def generate() -> tuple[str, str]:
    """A fresh identity. Returns (secret, public), both as age's own Bech32 strings."""
    ident = x25519.Identity.generate()
    return str(ident), str(ident.to_public())


def load_recipients(path: Path | None = None) -> list:
    """The public keys new data is sealed to. One per line; `#` comments allowed.

    More than one is normal and recommended: the second is the paper backup, and it is the
    only thing standing between a forgotten passphrase and total loss.
    """
    path = path or paths.recipients_file()
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise SealError(f"no recipients at {path}: run `spend init` first") from exc
    keys = [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]
    if not keys:
        raise SealError(f"{path} lists no recipients")
    try:
        return [x25519.Recipient.from_str(k) for k in keys]
    except Exception as exc:  # pyrage raises RecipientError, but be total about it
        raise SealError(f"{path} is not a list of age public keys: {exc}") from exc


def load_identity(secret: str) -> object:
    try:
        return x25519.Identity.from_str(secret.strip())
    except Exception as exc:
        raise SealError(f"not an age identity: {exc}") from exc


def wrap(secret: str, phrase: str) -> bytes:
    """Passphrase-wrap an identity for `identity.age`. scrypt; rage's own work factor."""
    try:
        return passphrase.encrypt(secret.encode(), phrase)
    except Exception as exc:
        raise SealError(f"could not wrap the identity: {exc}") from exc


def unwrap(data: bytes, phrase: str) -> str:
    try:
        return passphrase.decrypt(data, phrase).decode()
    except Exception as exc:
        raise SealError("wrong passphrase") from exc


# --- bytes -----------------------------------------------------------------------------

def seal(data: bytes, recipients: list | None = None) -> bytes:
    try:
        return pyrage.encrypt(data, recipients or load_recipients())
    except SealError:
        raise
    except Exception as exc:
        raise SealError(f"could not seal {len(data)} bytes: {exc}") from exc


def unseal(data: bytes, identity) -> bytes:
    if identity is None:
        raise Locked("the store is locked; run `spend unlock`")
    try:
        return pyrage.decrypt(data, [identity])
    except Exception as exc:
        raise SealError(f"could not open a sealed file: {exc}") from exc


# --- files -----------------------------------------------------------------------------

def seal_to(dest: Path, data: bytes, recipients: list | None = None) -> bool:
    """Seal `data` to `dest`. Returns False if it was already there.

    Never opens an existing path for writing. The `.part` and the rename are what make a
    half-written sealed file impossible to observe; the `exists()` check ahead of them is
    what makes re-appending an identical record a no-op rather than a rewrite, which is the
    property the whole append-only claim rests on.
    """
    if dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    blob = seal(data, recipients)
    tmp = dest.with_name(dest.name + ".part")
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.rename(dest)
    return True


def open_file(path: Path, identity) -> bytes:
    try:
        blob = path.read_bytes()
    except OSError as exc:
        raise SealError(f"cannot read {path}: {exc}") from exc
    if not blob.startswith(MAGIC):
        raise SealError(f"{path} is not an age file")
    return unseal(blob, identity)
