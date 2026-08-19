"""Where things live on disk.

Three roots, because the three kinds of data have three different relationships with
backup. The database and the receipt files are irreplaceable: a receipt photo cannot be
recovered from anything else once the phone forgets it. The render cache is derived from
those files and can be deleted at any time. Config is hand-written.

`SPEND_HOME` overrides all of them at once. That exists for tests and for throwaway
profiles; it is one variable rather than three so that pointing the whole app at a scratch
directory cannot half-succeed and leave a test writing into the real database.
"""

from __future__ import annotations

import os
from pathlib import Path

from spend.branding import ENV_PREFIX, SLUG


def _env(name: str) -> str | None:
    return os.environ.get(ENV_PREFIX + name)


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def home() -> Path | None:
    """The single override. When set, every other path hangs off it."""
    raw = _env("HOME")
    return Path(raw).expanduser() if raw else None


def data_dir() -> Path:
    if h := home():
        return h / "data"
    return _xdg("XDG_DATA_HOME", ".local/share") / SLUG


def cache_dir() -> Path:
    if h := home():
        return h / "cache"
    return _xdg("XDG_CACHE_HOME", ".cache") / SLUG


def config_dir() -> Path:
    if h := home():
        return h / "config"
    return _xdg("XDG_CONFIG_HOME", ".config") / SLUG


def db_path() -> Path:
    if raw := _env("DB"):
        return Path(raw).expanduser()
    return data_dir() / "spend.db"


def receipts_dir() -> Path:
    """Originals. Immutable once written, and the thing to back up."""
    if raw := _env("RECEIPTS"):
        return Path(raw).expanduser()
    return data_dir() / "receipts"


def render_dir() -> Path:
    """Derived renders and OCR text, keyed by content hash. Safe to delete."""
    return cache_dir() / "render"


def config_file() -> Path:
    return config_dir() / "config.toml"


def backup_dir() -> Path:
    """Where `spend backup` writes. Not under SPEND_HOME on purpose.

    A backup that lands inside the tree it is backing up is not a backup, so this one root
    ignores the single override and takes its own variable. In the container it is a mount
    point; under systemd it is `~/backups/spend`.
    """
    if raw := _env("BACKUP_DIR"):
        return Path(raw).expanduser()
    return Path.home() / "backups" / SLUG


def receipt_path(sha256: str, ext: str) -> Path:
    """Content-addressed, sharded two levels deep.

    A flat directory is fine until it isn't, and the migration once it isn't means
    rewriting every stored path. Two hex characters is 256 buckets, which keeps directory
    listings usable well past any volume of receipts one household produces.
    """
    return receipts_dir() / sha256[:2] / f"{sha256}{ext}"


def ensure_dirs() -> None:
    for d in (data_dir(), receipts_dir(), render_dir(), config_dir()):
        d.mkdir(parents=True, exist_ok=True)
