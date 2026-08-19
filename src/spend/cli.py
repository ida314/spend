"""`spend` — the terminal half.

Everything the web app can do, plus the things that only make sense from a shell: a
one-off ingest, a batch re-extraction, a rebuild, a backup, and `doctor`, which prints
what the process actually resolved rather than what the config file says it should have.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from spend import config, paths, store
from spend.branding import NAME, TAGLINE
from spend.money import format_cents


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    # httpx logs a line per request, and `sir`'s job protocol polls once a second while a
    # request is queued. At INFO that buries the one line per receipt this command exists
    # to print, so it is raised unless -v was asked for.
    logging.getLogger("httpx").setLevel(logging.DEBUG if verbose else logging.WARNING)


def _open():
    paths.ensure_dirs()
    conn = store.connect()
    store.migrate(conn)
    return conn


def cmd_ingest(args) -> int:
    from spend.service import IngestError, ingest_file
    conn = _open()
    added = 0
    for raw in args.paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            print(f"skip {path}: not a file", file=sys.stderr)
            continue
        try:
            rid, is_new = ingest_file(conn, path)
        except IngestError as exc:
            print(f"skip {path.name}: {exc}", file=sys.stderr)
            continue
        conn.commit()
        added += is_new
        print(f"{'added' if is_new else 'already had'} #{rid}  {path.name}")
    print(f"\n{added} new. Run `{NAME} extract` to read them.")
    return 0


def cmd_extract(args) -> int:
    from spend.extract.sir import SirExtractor
    from spend.service import extract_one

    conn = _open()
    ids = [args.receipt] if args.receipt else store.pending_receipt_ids(conn)
    if args.all:
        ids = store.receipt_ids(conn)
    if not ids:
        print("nothing to extract")
        return 0

    extractor = SirExtractor()

    async def run() -> int:
        failed = 0
        for rid in ids:
            result = await extract_one(conn, rid, extractor)
            mark = {"ok": "ok", "invalid": "??", "error": "!!"}[result.status]
            note = "" if result.ok else f"  {result.note}"
            secs = f"{(result.latency_ms or 0) / 1000:5.1f}s"
            txn = conn.execute(
                "SELECT merchant, total_cents, currency FROM transactions WHERE receipt_id=?",
                (rid,)).fetchone()
            desc = (f"{txn['merchant'] or '—'}  "
                    f"{format_cents(txn['total_cents'], txn['currency'])}") if txn else ""
            print(f"[{mark}] #{rid} {secs}  {desc}{note}", flush=True)
            failed += not result.ok
        return failed

    failed = asyncio.run(run())
    if failed:
        print(f"\n{failed} of {len(ids)} did not extract. They stay queued; "
              f"run again when `sir` is healthy.", file=sys.stderr)
    return 0


def cmd_rebuild(args) -> int:
    from spend.service import rebuild
    conn = _open()
    n = rebuild(conn)
    print(f"rebuilt {n} receipts from the log")
    return 0


def cmd_serve(args) -> int:
    import uvicorn
    from spend.web.api import create_app
    host, port = args.host or config.HOST, args.port or config.PORT
    print(f"{NAME} on http://{host}:{port}  ({TAGLINE})", file=sys.stderr)
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
    return 0


def cmd_backup(args) -> int:
    from spend import backup
    dest = Path(args.dest).expanduser() if args.dest else paths.backup_dir()
    try:
        r = backup.run(dest)
    except (FileNotFoundError, OSError) as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"db        {r.db}")
    print(f"receipts  {r.copied} copied, {r.present} already there  ->  {r.receipts}")
    return 0


def cmd_doctor(args) -> int:
    from spend.service import summary
    print(f"{NAME}  —  {TAGLINE}\n")
    print("paths")
    for label, value in (("db", paths.db_path()), ("receipts", paths.receipts_dir()),
                         ("render cache", paths.render_dir()), ("config", paths.config_file())):
        exists = "" if Path(value).exists() else "   (absent)"
        print(f"  {label:<14} {value}{exists}")

    print("\ninference")
    print(f"  {'model':<14} {config.MODEL}")
    print(f"  {'sir':<14} {config.SIR_BASE_URL}")
    print(f"  {'render mode':<14} {config.RENDER_MODE}")

    reachable, detail = _probe_sir()
    print(f"  {'reachable':<14} {'yes' if reachable else 'NO'}  {detail}")

    conn = _open()
    print(f"\nstore (schema v{store.schema_version(conn)})")
    s = summary(conn)
    for k in ("receipts", "extractions", "corrections", "transactions", "backlog",
              "needs_review"):
        print(f"  {k:<14} {s[k]}")
    print(f"  {'counted sum':<14} {format_cents(s['total_cents'])} over {s['counted']} rows")
    if s["last_error"]:
        e = s["last_error"]
        print(f"\n  last failure  {e['at']}  [{e['status']}]  {e['note']}")
    if s["backlog"] and not reachable:
        print(f"\n  {s['backlog']} receipts are waiting and `sir` is unreachable.")
    return 0 if reachable else 1


def _probe_sir() -> tuple[bool, str]:
    import urllib.error
    import urllib.request
    url = config.SIR_BASE_URL.rstrip("/") + "/healthz"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return True, f"{url} -> {r.status}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, f"{url} -> {exc}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog=NAME, description=TAGLINE)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("ingest", help="record receipt files")
    i.add_argument("paths", nargs="+")
    i.set_defaults(func=cmd_ingest)

    e = sub.add_parser("extract", help="read queued receipts with the model")
    e.add_argument("--receipt", type=int, help="just this one")
    e.add_argument("--all", action="store_true",
                   help="re-read every receipt, including ones already extracted")
    e.set_defaults(func=cmd_extract)

    r = sub.add_parser("rebuild", help="re-derive every transaction from the log")
    r.set_defaults(func=cmd_rebuild)

    b = sub.add_parser("backup", help="copy the database and receipts somewhere safe")
    b.add_argument("dest", nargs="?", help=f"destination directory "
                                          f"(default {paths.backup_dir()})")
    b.set_defaults(func=cmd_backup)

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_serve)

    d = sub.add_parser("doctor", help="print what this process resolved")
    d.set_defaults(func=cmd_doctor)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
