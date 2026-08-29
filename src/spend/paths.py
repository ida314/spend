"""Where things live on disk, and which of them the key protects.

Three roots, because the three kinds of data have three different relationships with the
key. That is a change of thesis: they used to be divided by their relationship with backup.

`data_dir()` is ciphertext and only ciphertext, forever. Every byte under it is an age file,
so it is safe to back up, safe to sync, safe to hand to a stranger. It holds the sealed log
and the sealed receipt originals, and it is the only thing that must survive.

`runtime_dir()` is plaintext and lives on tmpfs. The SQLite cache, the render cache and the
decrypted identity all live here, they all die at `spend lock`, and none of them is worth
backing up because every one of them is rebuildable from the log.

`config_dir()` is hand-written and mixed: `recipients.txt` is a public key and not a secret,
`identity.age` is the secret and is wrapped in a passphrase, and the SimpleFIN access URL is
a credential that has to be readable while locked -- see docs/encryption.md, which is honest
about what that last one does and does not buy.

`SPEND_HOME` overrides all of them at once. That exists for tests and for throwaway
profiles; it is one variable rather than four so that pointing the whole app at a scratch
directory cannot half-succeed and leave a test writing into the real database.
"""

from __future__ import annotations

import os
from pathlib import Path

from spend.branding import ENV_PREFIX, SLUG


class NoRuntimeDir(RuntimeError):
    """There is nowhere safe to put plaintext."""


def _env(name: str) -> str | None:
    return os.environ.get(ENV_PREFIX + name)


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def home() -> Path | None:
    """The single override. When set, every other path hangs off it."""
    raw = _env("HOME")
    return Path(raw).expanduser() if raw else None


def data_dir() -> Path:
    """Ciphertext only. Nothing readable is ever written under here."""
    if h := home():
        return h / "data"
    return _xdg("XDG_DATA_HOME", ".local/share") / SLUG


def config_dir() -> Path:
    if h := home():
        return h / "config"
    return _xdg("XDG_CONFIG_HOME", ".config") / SLUG


def runtime_dir() -> Path:
    """Plaintext, on tmpfs, gone at lock.

    There is deliberately no fall back to `/tmp`. `/tmp` is on the same unencrypted disk as
    everything else, and silently writing the whole decrypted cache there is precisely the
    failure this design exists to prevent -- so it is refused here, at the layer where it
    cannot be missed, rather than discovered later by someone grepping their own disk.

    Under `SPEND_HOME` it lands on real disk, and that is fine and deliberate: a test has
    nothing to protect, and a test that needed a tmpfs could not run anywhere.
    """
    if raw := _env("RUNTIME_DIR"):
        return Path(raw).expanduser()
    # SPEND_HOME before XDG_RUNTIME_DIR, because SPEND_HOME promises to move everything at
    # once. The other order would let a test with SPEND_HOME set write its cache into the
    # real store's runtime directory, which is the exact silent failure the autouse fixture
    # in conftest exists to prevent.
    if h := home():
        return h / "run"
    if xdg := os.environ.get("XDG_RUNTIME_DIR"):
        return Path(xdg) / SLUG
    raise NoRuntimeDir(
        "XDG_RUNTIME_DIR is unset, so there is no tmpfs to decrypt into. Set "
        f"{ENV_PREFIX}RUNTIME_DIR to a directory that is not on persistent storage. "
        "Falling back to /tmp would write your decrypted spending to the disk this "
        "whole design exists to keep it off."
    )


# --- ciphertext ----------------------------------------------------------------------------

def log_dir() -> Path:
    """The sealed event log. Append-only, content-addressed, never rewritten."""
    return data_dir() / "log"


def blobs_dir() -> Path:
    """Sealed receipt originals. Immutable once written."""
    if raw := _env("BLOBS"):
        return Path(raw).expanduser()
    return data_dir() / "blobs"


def drop_dir() -> Path:
    """Where CSV and OFX exports are dropped for import.

    Its own variable, so it can be a Syncthing folder or an iCloud mirror without moving
    anything else. Files here are plaintext until they are imported, because they arrive
    from outside; `spend feeds import` seals them and archives them.
    """
    if raw := _env("DROP"):
        return Path(raw).expanduser()
    return data_dir() / "drop"


def sharded(root: Path, sha256: str, suffix: str = ".age") -> Path:
    """Content-addressed, sharded two levels deep.

    A flat directory is fine until it isn't, and the migration once it isn't means rewriting
    every stored path. Two hex characters is 256 buckets, which keeps directory listings
    usable well past any volume one household produces -- of receipts, and now also of log
    events, which arrive several times faster.
    """
    return root / sha256[:2] / f"{sha256}{suffix}"


def blob_path(sha256: str) -> Path:
    return sharded(blobs_dir(), sha256)


def log_path(sha256: str) -> Path:
    return sharded(log_dir(), sha256)


# --- config --------------------------------------------------------------------------------

def config_file() -> Path:
    return config_dir() / "config.toml"


def recipients_file() -> Path:
    """age public keys, one per line. Not a secret; this is what lets a locked box append."""
    if raw := _env("RECIPIENTS"):
        return Path(raw).expanduser()
    return config_dir() / "recipients.txt"


def identity_file() -> Path:
    """The age identity, wrapped in your passphrase. The one irreplaceable file."""
    return config_dir() / "identity.age"


def access_file() -> Path:
    """The SimpleFIN access URL. Embeds HTTP Basic credentials; mode 0600."""
    return config_dir() / "simplefin.access"


def accounts_rules_file() -> Path:
    """Local account identity, hand-edited. See rules/accounts.toml for the shipped default."""
    return config_dir() / "accounts.toml"


# --- runtime -------------------------------------------------------------------------------

def db_path() -> Path:
    """The cache. Deleted at lock and rebuilt from the log at unlock."""
    if raw := _env("DB"):
        return Path(raw).expanduser()
    return runtime_dir() / f"{SLUG}.db"


def render_dir() -> Path:
    """Downscaled renders and OCR text, keyed by content hash.

    On tmpfs now, not `~/.cache`. It used to be derived-and-therefore-unimportant; it is in
    fact plaintext receipt content, which makes it exactly as sensitive as the originals and
    is easy to miss because nothing about the word "cache" suggests it.
    """
    return runtime_dir() / "render"


def identity_runtime_file() -> Path:
    return runtime_dir() / "identity"


def expires_file() -> Path:
    return runtime_dir() / "expires_at"


def lock_file() -> Path:
    return runtime_dir() / ".lock"


def agent_dir() -> Path:
    """The materialised workspace an agent reads. Plaintext, tmpfs, gone at lock."""
    if raw := _env("AGENT_DIR"):
        return Path(raw).expanduser()
    return runtime_dir() / "agent"


# --- backup --------------------------------------------------------------------------------

def backup_dir() -> Path:
    """Where `spend backup` writes. Not under SPEND_HOME on purpose.

    A backup that lands inside the tree it is backing up is not a backup, so this one root
    ignores the single override and takes its own variable. In the container it is a mount
    point; under systemd it is `~/backups/spend`.
    """
    if raw := _env("BACKUP_DIR"):
        return Path(raw).expanduser()
    return Path.home() / "backups" / SLUG


def ensure_dirs() -> None:
    """Create the ciphertext roots. Deliberately does not touch the runtime root.

    The runtime root is created by `spend unlock`, with mode 0700, and its absence is how
    every other command knows the store is locked. Creating it here would make a locked
    store look unlocked to anything that only checked for the directory.
    """
    for d in (data_dir(), log_dir(), blobs_dir(), config_dir(),
              drop_dir() / "inbox", drop_dir() / "archive", drop_dir() / "unrecognised"):
        d.mkdir(parents=True, exist_ok=True)


def ensure_runtime() -> Path:
    """Create the plaintext root, private. Called by `unlock` and by nothing else."""
    r = runtime_dir()
    r.mkdir(parents=True, exist_ok=True, mode=0o700)
    r.chmod(0o700)
    render_dir().mkdir(parents=True, exist_ok=True)
    return r
