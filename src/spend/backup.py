"""Copy the ciphertext somewhere else.

There is nothing here to protect any more, and that is the point: everything under
`data_dir()` is an age file, so a backup is a file copy, the destination is safe on a drive
you leave at someone else's house, and **this runs while the store is locked**. The nightly
timer needs no key and no passphrase.

Two arguments this module used to make are now obsolete, and they are worth naming rather
than quietly deleting. `VACUUM INTO` existed because the database was irreplaceable and in
WAL mode; the database is now a cache rebuilt from the log at every unlock, so there is
nothing to vacuum and nothing lost by not copying it. And the atomic single-file swap existed
to protect one destination file that was rewritten every run; nothing here is ever rewritten.

What survives is the rule that made the receipt copy correct, now applied to two trees: names
are content hashes, so a file already there under the same name is already the right file.
Size is checked as well as name, because a run killed mid-copy leaves a short file under the
right name and content addressing alone would call that one done forever. Nothing is ever
deleted from the destination -- an extra file there means the source lost something, and a
backup is the wrong place to propagate that.

`identity.age` is copied too. It is passphrase-wrapped, which is exactly what `age -p` is
for, and losing the only copy of it is a far likelier way to lose everything than someone
brute-forcing scrypt off a stolen drive. The output says so, every run.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from spend import paths, seal


@dataclass(frozen=True)
class Result:
    dest: Path
    logs: int
    blobs: int
    present: int
    keys: list[str]


def _mirror(src_root: Path, dst_root: Path) -> tuple[int, int]:
    copied = present = 0
    if not src_root.exists():
        return 0, 0
    for src in sorted(src_root.rglob("*")):
        if not src.is_file() or src.suffix == ".part":
            continue
        dst = dst_root / src.relative_to(src_root)
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            present += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    return copied, present


def run(dest: Path | None = None) -> Result:
    dest = dest or paths.backup_dir()
    log_root = paths.log_dir()
    if not log_root.exists():
        # Backing up nothing over a good backup is the failure this cannot have.
        raise FileNotFoundError(f"no log at {log_root}; nothing to back up")

    dest.mkdir(parents=True, exist_ok=True)
    logs, present_a = _mirror(log_root, dest / "log")
    blobs, present_b = _mirror(paths.blobs_dir(), dest / "blobs")

    copied_keys = []
    for src in (paths.identity_file(), paths.recipients_file()):
        if src.exists():
            dst = dest / "config" / src.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied_keys.append(src.name)

    return Result(dest=dest, logs=logs, blobs=blobs, present=present_a + present_b,
                  keys=copied_keys)


def plaintext_under(root: Path) -> list[Path]:
    """Any file in a supposedly-ciphertext tree that is not an age file.

    Used by `spend verify` and by the test that walks a finished backup. Cheap -- it reads
    twenty-one bytes per file -- and it is the only check that would actually catch a
    regression where something started writing plaintext into the data root.
    """
    out = []
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        with f.open("rb") as fh:
            if fh.read(len(seal.MAGIC)) != seal.MAGIC:
                out.append(f)
    return out
