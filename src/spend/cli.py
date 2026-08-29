"""`spend` — the terminal half.

Everything the web app can do, plus the things that only make sense from a shell: a
one-off ingest, a batch re-extraction, a rebuild, a backup, and `doctor`, which prints
what the process actually resolved rather than what the config file says it should have.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from spend import config, keys, paths, seal, store
from spend.branding import NAME, SLUG, TAGLINE
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
    """The database, or a clear reason why not.

    Everything except `init`, `lock`, `backup` and `doctor` goes through here, so "the store
    is locked" is answered in one place and in one sentence rather than as whatever error
    SQLite happens to raise about a file that is not there.
    """
    paths.ensure_dirs()
    try:
        keys.require_identity()
    except keys.Locked as exc:
        print(f"{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    conn = store.connect()
    store.migrate(conn)
    return conn


# --- the key ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    import getpass
    paths.ensure_dirs()
    if paths.identity_file().exists():
        print(f"{paths.identity_file()} already exists. Generating a new identity would "
              f"orphan every sealed file in {paths.log_dir()}.", file=sys.stderr)
        return 1
    # The render cache used to live on the unencrypted disk, holding downscaled receipt
    # JPEGs and their OCR text. Nothing about the word "cache" suggests that is sensitive,
    # which is exactly why it is worth refusing to start on top of one. Only for the real
    # profile: a scratch SPEND_HOME has nothing to do with what is in the user's ~/.cache.
    stale = Path.home() / ".cache" / SLUG
    if paths.home() is None and any(f.is_file() for f in stale.rglob("*")):
        print(f"{stale} still holds plaintext receipt renders from before encryption.\n"
              f"Remove it first: rm -rf {stale}", file=sys.stderr)
        return 1

    print(f"{NAME} keeps everything on disk sealed with age. This makes the key.\n")
    phrase = getpass.getpass("passphrase: ")
    if phrase != getpass.getpass("again:      "):
        print("they do not match", file=sys.stderr)
        return 1
    try:
        public, backup_secret = keys.init(phrase, args.recipient)
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    print(f"\nrecipients  {paths.recipients_file()}")
    print(f"identity    {paths.identity_file()}  (wrapped in your passphrase)")
    print(f"public key  {public}")
    print("\n" + "=" * 78)
    print("WRITE THIS DOWN, ON PAPER, NOW. It is printed once and never again.")
    print("There is no recovery here: no backdoor, no reset, no support line. If you")
    print("forget the passphrase, this key is the only way back to your own data.")
    print("=" * 78)
    print(f"\n  {backup_secret}\n")
    tail = backup_secret[-6:]
    for _ in range(3):
        try:
            typed = input("type the last six characters back to confirm: ").strip()
        except EOFError:
            print("\n(not a terminal, so nothing to confirm against)")
            break
        if typed == tail:
            print("\ngood. Keep it somewhere that is not this computer.")
            return 0
        print("that is not it.")
    print("\nNot confirmed. The key above is still valid and still your only backup — "
          "write it down.", file=sys.stderr)
    return 1


def cmd_unlock(args) -> int:
    import getpass
    from datetime import timedelta

    from spend import replay
    paths.ensure_dirs()
    if not paths.identity_file().exists():
        print(f"no identity at {paths.identity_file()}; run `{NAME} init` first",
              file=sys.stderr)
        return 1
    ttl = timedelta(hours=args.ttl if args.ttl is not None else config.LOCK_TTL_HOURS)
    try:
        with keys.exclusive():
            keys.unlock(getpass.getpass("passphrase: "), ttl)
            conn, stats = replay.fresh(keys.require_identity())
            conn.close()
    except seal.SealError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    except keys.Busy as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    print(f"replayed {stats.line()}")
    for path, why in stats.bad:
        print(f"  UNREADABLE  {path.name}  {why}", file=sys.stderr)
    for orphan in stats.orphans:
        print(f"  orphaned    {orphan}", file=sys.stderr)
    when = keys.expires_at()
    print(f"unlocked until {when:%H:%M} ({ttl})")
    _arm_relock(ttl)
    _service("start")
    return 0


def cmd_lock(args) -> int:
    _service("stop")
    try:
        with keys.exclusive():
            keys.lock()
    except keys.Busy as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    print("locked. The database is gone; the log is not.")
    return 0


def cmd_verify(args) -> int:
    """Open every sealed file. The only check that would notice slow corruption."""
    from spend import backup, ledger
    try:
        identity = keys.require_identity()
    except keys.Locked as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    good, bad = ledger.events(identity)
    print(f"log       {len(good)} events readable")
    blobs = ok = 0
    for f in paths.blobs_dir().rglob("*.age"):
        blobs += 1
        try:
            seal.open_file(f, identity)
            ok += 1
        except seal.SealError as exc:
            print(f"  UNREADABLE  {f.name}  {exc}", file=sys.stderr)
    print(f"blobs     {ok} of {blobs} readable")
    for root in (paths.log_dir(), paths.blobs_dir()):
        for f in backup.plaintext_under(root):
            print(f"  PLAINTEXT   {f}", file=sys.stderr)
    for path, why in bad:
        print(f"  UNREADABLE  {path.name}  {why}", file=sys.stderr)
    return 1 if bad or ok != blobs else 0


def _arm_relock(ttl) -> None:
    """A timer, not a thread. One that dies with the process it protects is not a timer."""
    import shutil
    import subprocess
    if not shutil.which("systemd-run"):
        return
    subprocess.run(
        ["systemd-run", "--user", "--quiet", f"--on-active={int(ttl.total_seconds())}",
         "--unit=spend-relock", sys.argv[0], "lock"],
        check=False, capture_output=True)


def _service(action: str) -> None:
    """Start or stop whichever deploy path this box runs. Never both -- see the README."""
    import subprocess
    if config.DEPLOY == "systemd":
        subprocess.run(["systemctl", "--user", action, f"{NAME}.service"],
                       check=False, capture_output=True)
    elif config.DEPLOY == "compose":
        subprocess.run(["docker", "compose", action, NAME], check=False, capture_output=True)


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
    print(f"log       {r.logs} new")
    print(f"blobs     {r.blobs} new, {r.present} already there")
    print(f"          -> {r.dest}")
    if r.keys:
        print(f"\nkeys      {', '.join(r.keys)} are in this backup. identity.age is wrapped\n"
              f"          in your passphrase, so the backup is only as strong as that.")
    return 0


def swap_report() -> tuple[bool, list[str]]:
    """Is every swap device encrypted?

    tmpfs pages are swappable, so a plaintext cache on tmpfs plus an unencrypted swap file
    means the decrypted ledger reaches the platter anyway. That makes this the one check
    that decides whether the rest of this feature is true, and `doctor` is where a check
    that decides something belongs -- it prints what the box actually is, not what the
    config file hoped.
    """
    lines, safe = [], True
    try:
        raw = Path("/proc/swaps").read_text().splitlines()[1:]
    except OSError:
        return True, ["  swap           (cannot read /proc/swaps)"]
    if not raw:
        return True, ["  swap           none"]
    for entry in raw:
        name, kind = entry.split()[0], entry.split()[1]
        encrypted = name.startswith("/dev/mapper/") or name.startswith("/dev/zram")
        safe &= encrypted
        mark = "" if encrypted else "   NOT ENCRYPTED"
        lines.append(f"  swap           {name} ({kind}){mark}")
    return safe, lines


def cmd_doctor(args) -> int:
    from spend import ledger
    from spend.service import summary
    print(f"{NAME}  —  {TAGLINE}\n")

    unlocked = keys.is_unlocked()
    print("store")
    print(f"  {'state':<14} {'unlocked' if unlocked else 'LOCKED'}")
    if unlocked and (when := keys.expires_at()):
        print(f"  {'relocks at':<14} {when.astimezone():%H:%M}")
    print(f"  {'log':<14} {ledger.count()} sealed events")
    swap_ok, swap_lines = swap_report()
    for line in swap_lines:
        print(line)
    if not swap_ok:
        print("\n  Plaintext on tmpfs can be paged out to an unencrypted swap device, so")
        print("  the encryption below protects a powered-off disk and not much else.")
        print("  See docs/encryption.md for the crypttab change that fixes it.\n")

    print("\npaths")
    for label, value in (("db", _db_or_locked()), ("blobs", paths.blobs_dir()),
                         ("log", paths.log_dir()), ("config", paths.config_file())):
        exists = "" if Path(value).exists() else "   (absent)"
        print(f"  {label:<14} {value}{exists}")

    if not unlocked:
        print(f"\nLocked, so there is nothing else to report. Run `{NAME} unlock`.")
        return 1

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

    feeds_ok = _doctor_feeds(conn)
    return 0 if reachable and swap_ok and feeds_ok else 1


def _doctor_feeds(conn) -> bool:
    """The feed half. Returns False when a human needs to do something.

    Four things count as "a human needs to do something", and all four are silent otherwise:
    a connection that needs reauthorising, an account the feed reports that accounts.toml
    does not name, an account whose data has gone stale, and a workspace older than the data
    it claims to describe.
    """
    import json as _json
    from datetime import UTC, datetime, timedelta

    from spend.money import format_cents
    from spend.sources import simplefin

    ok = True
    print("\nfeeds")
    print(f"  {'simplefin':<14} {simplefin.redact(simplefin.access_url())}")

    for source in ("simplefin", "drop"):
        polls = store.feed_polls_all(conn, source)
        good = [p for p in polls if p["outcome"] in ("ok", "partial")]
        last = max((p["started_at"] for p in good), default=None)
        if last:
            age = datetime.now(UTC) - datetime.fromisoformat(last)
            print(f"  {source + ' last ok':<14} {last[:19]}   ({age.days}d ago)")
        else:
            print(f"  {source + ' last ok':<14} never")
        failures = [p for p in polls if p["outcome"] == "error"]
        if failures:
            note = failures[-1]["note"] or ""
            print(f"  {source + ' last error':<14} {note[:60]}")
        errors = [e for p in polls[-5:] if p["errors"]
                  for e in _json.loads(p["errors"]) if str(e.get("code", "")).startswith("con.")]
        if errors:
            ok = False
            print(f"  {'ACTION':<14} {errors[-1]['msg']}")
            print(f"  {'':<14} reconnect at the Bridge: `{NAME} feeds connect <token>`")

    today = datetime.now(UTC).date().isoformat()
    used = len([p for p in store.feed_polls_all(conn, "simplefin")
                if (p["started_at"] or "")[:10] == today])
    print(f"  {'calls today':<14} {used} of 20   (protocol ceiling 24)")
    waiting = len(list((paths.drop_dir() / "inbox").glob("*"))) \
        if (paths.drop_dir() / "inbox").exists() else 0
    print(f"  {'drop inbox':<14} {paths.drop_dir() / 'inbox'}   {waiting} waiting")

    accounts = list(conn.execute("SELECT * FROM accounts ORDER BY unmapped DESC, account_key"))
    if accounts:
        print("\naccounts")
        stale_after = timedelta(days=config.FEED_STALE_DAYS)
        for a in accounts:
            balance = (format_cents(a["balance_cents"], a["currency"])
                       if a["balance_cents"] is not None else "—")
            marks = []
            if a["unmapped"]:
                marks.append("NOT IN accounts.toml")
                ok = False
            if a["last_seen_at"]:
                age = datetime.now(UTC) - datetime.fromisoformat(a["last_seen_at"])
                if age > stale_after and not a["unmapped"]:
                    marks.append(f"STALE {age.days}d")
                    ok = False
            print(f"  {a['account_key']:<20} {a['kind']:<9} {balance:>12}   "
                  f"{a['covers_from'] or '?'} .. {a['covers_to'] or '?'}"
                  f"{'   ' + ', '.join(marks) if marks else ''}")

    counts = store.feed_counts(conn)
    print("\nfeed store")
    for key in ("polls", "records", "transactions", "pending", "needs_review",
                "duplicate_suspect"):
        print(f"  {key:<20} {counts[key]}")

    print("\nagent workspace")
    try:
        built = paths.agent_dir() / "MANIFEST.json"
        if built.exists():
            manifest = _json.loads(built.read_text())
            since = [p for p in store.feed_polls_all(conn)
                     if (p["started_at"] or "") > manifest["built_at"]]
            note = f"   ({len(since)} polls since -- run `{NAME} agent build`)" if since else ""
            if since:
                ok = False
            print(f"  {'built':<20} {manifest['built_at'][:19]}{note}")
            for warning in manifest.get("warnings", []):
                print(f"  {'warning':<20} {warning}")
        else:
            print(f"  {'built':<20} never   (`{NAME} agent build`)")
    except (OSError, ValueError, paths.NoRuntimeDir) as exc:
        print(f"  {'built':<20} unreadable: {exc}")
    return ok


def _db_or_locked() -> Path | str:
    try:
        return paths.db_path()
    except paths.NoRuntimeDir:
        return "(no runtime directory)"


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

    n = sub.add_parser("init", help="generate the key this store is sealed with")
    n.add_argument("--recipient", action="append", default=[],
                   help="an extra age public key that may also open the log")
    n.set_defaults(func=cmd_init)

    u = sub.add_parser("unlock", help="decrypt the store and rebuild the cache")
    u.add_argument("--ttl", type=float, help="hours before it relocks itself")
    u.set_defaults(func=cmd_unlock)

    k = sub.add_parser("lock", help="forget the key and delete the cache")
    k.set_defaults(func=cmd_lock)

    vfy = sub.add_parser("verify", help="open every sealed file and report what will not")
    vfy.set_defaults(func=cmd_verify)

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

    b = sub.add_parser("backup", help="copy the sealed log and blobs somewhere safe")
    b.add_argument("dest", nargs="?", help=f"destination directory "
                                          f"(default {paths.backup_dir()})")
    b.set_defaults(func=cmd_backup)

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_serve)

    feeds = sub.add_parser("feeds", help="bank and card statements")
    fsub = feeds.add_subparsers(dest="feeds_cmd", required=True)

    fc = fsub.add_parser("connect", help="one-time SimpleFIN setup-token exchange")
    fc.add_argument("token")
    fc.set_defaults(func=cmd_feeds_connect)

    fs = fsub.add_parser("sync", help="pull whatever is new")
    fs.add_argument("--since", help="ISO date; ask from here instead of the planned window")
    fs.add_argument("--backfill", action="store_true", help="start again from the beginning")
    fs.add_argument("--dry-run", action="store_true", help="print the window and stop")
    fs.set_defaults(func=cmd_feeds_sync)

    fi = fsub.add_parser("import", help="import CSV/OFX exports (default: the inbox)")
    fi.add_argument("paths", nargs="*")
    fi.set_defaults(func=cmd_feeds_import)

    fa = fsub.add_parser("accounts", help="what the feeds have seen, and how it is mapped")
    fa.set_defaults(func=cmd_feeds_accounts)

    fl = fsub.add_parser("log", help="the poll history, one line each")
    fl.add_argument("-n", "--number", type=int, default=20)
    fl.add_argument("--source")
    fl.set_defaults(func=cmd_feeds_log)

    agent = sub.add_parser("agent", help="the directory an agent reads")
    asub = agent.add_subparsers(dest="agent_cmd", required=True)

    ab = asub.add_parser("build", help="materialise the workspace")
    ab.add_argument("--dest", help="somewhere other than the runtime directory")
    ab.add_argument("--no-rebuild", action="store_true",
                    help="skip reprojecting first; use what is already in the cache")
    ab.set_defaults(func=cmd_agent_build)

    ap = asub.add_parser("path", help="print the workspace path, for `cd $(spend agent path)`")
    ap.set_defaults(func=cmd_agent_path)

    aq = asub.add_parser("sql", help="run a query (or a queries/*.sql file) against the ledger")
    aq.add_argument("query", help="SQL, a path, or a name under queries/")
    aq.add_argument("--dest")
    aq.add_argument("--json", action="store_true")
    aq.set_defaults(func=cmd_agent_sql)

    d = sub.add_parser("doctor", help="print what this process resolved")
    d.set_defaults(func=cmd_doctor)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())


# --- feeds ---------------------------------------------------------------------------------
# Nested one level, unlike everything else. Five verbs flattened would read `feed-sync`,
# `feed-connect`, `feed-accounts`, and that is worse than one level of nesting. Nothing else
# in this CLI has enough verbs to earn a group.

def cmd_feeds_connect(args) -> int:
    from spend.sources import simplefin
    paths.ensure_dirs()
    try:
        url = simplefin.connect(args.token)
    except simplefin.ConnectError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    dest = simplefin.store_access_url(url)
    print(f"connected   {simplefin.redact(url)}")
    print(f"saved to    {dest}  (mode 0600)\n")

    poll = simplefin.SimpleFIN(url).poll(None)
    if poll.outcome == "error":
        print("The credential is saved, but the first look failed:", file=sys.stderr)
        for e in poll.errors:
            print(f"  [{e.code}] {e.msg}", file=sys.stderr)
        return 1

    print("It can see these accounts. Paste what you want into "
          f"{paths.accounts_rules_file()}\nand edit the names and kinds:\n")
    for a in poll.accounts:
        slug = _slugify(a.org_name, a.name)
        kind = "credit" if (a.balance_cents or 0) < 0 else "checking"
        print(f"[{slug}]")
        print(f'name        = "{a.name or slug}"')
        print(f'kind        = "{kind}"          # checking | savings | credit | other')
        print(f'institution = "{a.org_name or ""}"')
        print(f'native      = ["{a.native_id}"]')
        print()
    print(f"Then: {NAME} feeds sync")
    return 0


def _slugify(*parts: str | None) -> str:
    import re
    text = "-".join(p for p in parts if p)
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-") or "account"


def cmd_feeds_sync(args) -> int:
    from datetime import date

    from spend import service
    from spend.sources import base, simplefin
    conn = _open()
    source = simplefin.SimpleFIN()
    if not source.url:
        print(f"not connected. Run `{NAME} feeds connect <setup-token>` first.",
              file=sys.stderr)
        return 1

    history = [dict(r) for r in store.feed_polls_all(conn, simplefin.NAME)]
    if args.since:
        window = base.Window(start=date.fromisoformat(args.since),
                             end=date.today())          # noqa: DTZ011 - a local calendar day
    elif args.backfill:
        window = base.plan_window([], date.today())     # noqa: DTZ011
    else:
        window = base.plan_window(history, date.today())  # noqa: DTZ011

    if window is None:
        used = len([p for p in history if p["started_at"][:10] == date.today().isoformat()])  # noqa: DTZ011
        print(f"{used} of {base.MAX_CALLS_DAY} calls used today; the window opens again "
              f"at 00:00 local.")
        return 0
    if args.dry_run:
        print(f"would ask for {window.start} .. {window.end}")
        return 0

    poll = source.poll(window)
    summary = service.record_poll(conn, poll)
    service.reproject_feeds(conn)

    print(f"{window.start} .. {window.end}   {summary['records']} seen, "
          f"{summary['new']} new   [{poll.outcome}]")
    for e in poll.errors:
        stream = sys.stderr if e.needs_a_human else sys.stdout
        print(f"  [{e.code}] {e.msg}", file=stream)
    if poll.needs_a_human:
        print(f"\nA connection needs reauthorising at the Bridge. Until it is, that account "
              f"returns\nnothing and looks exactly like a quiet week.", file=sys.stderr)
        return 1
    return 0 if poll.outcome != "error" else 1


def cmd_feeds_import(args) -> int:
    from spend import service
    from spend.sources import drop
    conn = _open()
    files = [Path(p).expanduser() for p in args.paths] if args.paths else drop.inbox()
    if not files:
        print(f"nothing in {paths.drop_dir() / 'inbox'}")
        return 0

    failures = 0
    for path in files:
        blob_sha = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
        # Claimed by its bytes, before anything is parsed. This is what makes re-dropping an
        # overlapping monthly export by accident a no-op rather than a duplicate.
        if store.feed_poll_for_file(conn, blob_sha):
            print(f"{path.name}: already imported; sealing and removing the plaintext")
            drop.archive(path, blob_sha)
            continue

        poll = drop.read_file(path)
        if poll.outcome == "error":
            for e in poll.errors:
                print(f"{path.name}: [{e.code}] {e.msg}", file=sys.stderr)
            drop.archive(path, blob_sha, unrecognised=True)
            failures += 1
            continue

        summary = service.record_poll(conn, poll)
        # Committed first, moved second. A crash between them leaves the file in the inbox
        # and the records in; the next run re-hashes it, finds the poll, and just archives.
        drop.archive(path, blob_sha)
        print(f"{path.name}: {summary['records']} rows, {summary['new']} new "
              f"[{poll.detail.get('dialect')}]  -> sealed {blob_sha[:12]}")
        for e in poll.errors:
            print(f"  [{e.code}] {e.msg}", file=sys.stderr)

    service.reproject_feeds(conn)
    return 1 if failures else 0


def cmd_feeds_accounts(args) -> int:
    from spend.money import format_cents
    conn = _open()
    rows = list(conn.execute("SELECT * FROM accounts ORDER BY unmapped DESC, account_key"))
    if not rows:
        print("no accounts yet. Run `feeds sync` or `feeds import`.")
        return 0
    for r in rows:
        balance = format_cents(r["balance_cents"], r["currency"]) if r["balance_cents"] is not None else "—"
        covers = f"{r['covers_from'] or '?'} .. {r['covers_to'] or '?'}"
        mark = "   NOT IN accounts.toml" if r["unmapped"] else ""
        print(f"{r['account_key']:<22} {r['kind']:<9} {balance:>12}   {covers}{mark}")
    return 1 if any(r["unmapped"] for r in rows) else 0


def cmd_feeds_log(args) -> int:
    conn = _open()
    for r in store.feed_polls_recent(conn, args.source, args.number):
        window = f"{r['window_from'] or '—'} .. {r['window_to'] or '—'}"
        errors = f"  {r['errors']}" if r["errors"] else ""
        print(f"{r['started_at'][:19]}  {r['source']:<10} {r['outcome']:<8} {window:<26}"
              f" {r['records_seen']:>4} seen {r['records_new']:>4} new{errors}")
    return 0


# --- the agent workspace ---------------------------------------------------------------------

def cmd_agent_build(args) -> int:
    from spend import service
    from spend.agent import workspace
    conn = _open()
    if not args.no_rebuild:
        service.rebuild(conn)
    dest = Path(args.dest).expanduser() if args.dest else paths.agent_dir()
    manifest = workspace.build(conn, dest)

    bank, receipts = manifest["coverage"]["bank"], manifest["coverage"]["receipts"]
    print(f"{dest}")
    print(f"  bank      {bank['rows']:>5} rows   {bank['from'] or '—'} .. {bank['to'] or '—'}")
    print(f"  receipts  {receipts['rows']:>5} rows   "
          f"{receipts['from'] or '—'} .. {receipts['to'] or '—'}")
    for warning in manifest["warnings"]:
        print(f"  warning   {warning}", file=sys.stderr)
    print(f"\nPoint an agent at it. It reads CLAUDE.md first.")
    return 0


def cmd_agent_path(args) -> int:
    try:
        print(paths.agent_dir())
    except paths.NoRuntimeDir as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    return 0


def cmd_agent_sql(args) -> int:
    """Run a query against the workspace ledger without needing the sqlite3 binary.

    The `sqlite3` CLI is the nicer tool and CLAUDE.md reaches for it first, but it is a
    separate package that is not installed everywhere -- and an agent that finds
    `command not found` where the instructions promised a database tends to give up rather
    than reach for Python. This is the floor: `spend` is by definition installed.
    """
    import sqlite3 as sql
    db_path = (Path(args.dest).expanduser() if args.dest else paths.agent_dir()) / "ledger.db"
    if not db_path.exists():
        print(f"no workspace at {db_path.parent}; run `{NAME} agent build`", file=sys.stderr)
        return 1

    query = args.query
    candidate = Path(query).expanduser()
    if not candidate.exists():
        candidate = db_path.parent / "queries" / query
    if candidate.exists():
        query = candidate.read_text()
    query = query.strip().rstrip(";")

    db = sql.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sql.Row
    try:
        rows = db.execute(query).fetchall()
    except sql.Error as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    if not rows:
        print("(no rows)")
        return 0
    if args.json:
        for row in rows:
            print(json.dumps(dict(row), sort_keys=True, default=str))
        return 0

    names = list(rows[0].keys())
    widths = [max(len(n), max(len(str(r[n]) if r[n] is not None else "") for r in rows))
              for n in names]
    print("  ".join(n.ljust(w) for n, w in zip(names, widths, strict=True)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join((str(row[n]) if row[n] is not None else "").ljust(w)
                        for n, w in zip(names, widths, strict=True)))
    return 0
