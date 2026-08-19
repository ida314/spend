"""Copy the two irreplaceable things somewhere else.

The database and the receipt originals. Everything else in the tree is derived: the render
cache rebuilds itself, and `transactions` and `line_items` come back from `rebuild`.

Two things this does that a `cp -r` does not:

`VACUUM INTO` rather than a file copy, because the database is in WAL mode. Copying the
`.db` without its `-wal` alongside produces a file that opens cleanly and is missing the
most recent writes — a backup that fails only when you need it. `VACUUM INTO` takes a read
transaction and writes a consistent single file, so it is safe to run against a database
the service has open.

Receipts are copied only when absent, because their names are the sha256 of their bytes: a
file already there under the same name is already the right file. Nothing is deleted from
the destination — receipts are never removed from the live tree (deletion is a flag on the
projection), so an extra file at the destination means the source lost something, and a
backup is the wrong place to propagate that.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from spend import paths


@dataclass(frozen=True)
class Result:
    db: Path
    receipts: Path
    copied: int
    present: int


def run(dest: Path | None = None) -> Result:
    dest = dest or paths.backup_dir()
    src_db = paths.db_path()
    if not src_db.exists():
        raise FileNotFoundError(f"no database at {src_db}; nothing to back up")

    dest.mkdir(parents=True, exist_ok=True)
    db_out = dest / src_db.name
    # VACUUM INTO refuses to overwrite, and the rename is what makes the swap atomic: an
    # interrupted run leaves the previous good backup in place rather than a half file.
    tmp = db_out.with_name(db_out.name + ".tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(src_db, timeout=30.0)
    try:
        conn.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        conn.close()
    tmp.replace(db_out)

    src_root = paths.receipts_dir()
    dst_root = dest / src_root.name
    copied = present = 0
    for src in sorted(src_root.rglob("*")) if src_root.exists() else []:
        if not src.is_file():
            continue
        dst = dst_root / src.relative_to(src_root)
        # Size as well as name: a run killed mid-copy leaves a short file under the right
        # name, and content addressing alone would call that one done forever.
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            present += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1

    return Result(db=db_out, receipts=dst_root, copied=copied, present=present)
