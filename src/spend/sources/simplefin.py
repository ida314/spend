"""SimpleFIN Bridge: the automatic half.

`urllib.request`, deliberately. This is one HTTP GET on a daily timer, `cli._probe_sir`
already reaches for urllib for exactly this kind of call, and adding a runtime HTTP client
would change the container image for a request that happens once a night.

## The credential

The Access URL *is* the credential -- it carries HTTP Basic auth inline, `https://user:pass@
host/path`. It lives in one file at mode 0600 and is never written to the database, never
logged, and never printed except redacted. A test asserts the first of those by tracing every
statement the app issues.

It has to be readable while the store is locked, because the whole point of the nightly sync
is that it runs without you. `docs/encryption.md` is honest about what that costs: it is a
0600 file on an unencrypted disk, so use a read-only token and treat it as revocable rather
than as secret.

## The one contract violation to avoid

**Never treat an empty `transactions` array as a quiet week without reading `errlist`.** A
`con.auth` on the Capital One connection comes back as HTTP 200 with zero transactions and is
indistinguishable from a frugal week. `docs/email-ingest.md` already writes down the same
rule for the mail path; it is the same mistake with a different feed, and it is silent both
times. Every error is recorded on the poll row, and a `con.*` makes the command exit non-zero
because nothing but a human redoing the setup-token dance will fix it.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from spend import paths
from spend.money import MoneyError, to_cents
from spend.sources.base import Account, FeedError, Poll, Record, Window

NAME = "simplefin"
TIMEOUT = 60.0
USER_AGENT = "spend/1 (+https://github.com/ida314/spend)"


class ConnectError(RuntimeError):
    """The one-time setup-token exchange failed. A human has to make a new token."""


# --- the credential ------------------------------------------------------------------------

def access_url() -> str | None:
    import os
    if raw := os.environ.get("SPEND_SIMPLEFIN_ACCESS_URL"):
        return raw.strip()
    try:
        return paths.access_file().read_text().strip() or None
    except OSError:
        return None


def store_access_url(url: str) -> Path:
    dest = paths.access_file()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_text(url.strip() + "\n")
    tmp.chmod(0o600)
    tmp.rename(dest)
    return dest


def redact(url: str | None) -> str:
    """For `doctor` and for logs. The password is the whole credential."""
    if not url:
        return "(not connected)"
    return re.sub(r"//[^@/]+@", "//***@", url)


def connect(setup_token: str) -> str:
    """Exchange a single-use setup token for an Access URL.

    The token is base64 of a claim URL; POST an empty body to it and the response body *is*
    the Access URL. Single use -- a second attempt is a 403, and the message says so, because
    "403" on its own sends people looking for the wrong problem.
    """
    try:
        claim = base64.b64decode(setup_token.strip(), validate=True).decode().strip()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ConnectError("that does not look like a SimpleFIN setup token") from exc
    if not claim.startswith("https://"):
        raise ConnectError(f"the token decodes to {claim!r}, which is not an https URL")

    request = urllib.request.Request(claim, data=b"", method="POST",
                                     headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            url = response.read().decode().strip()
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise ConnectError(
                "that token has already been claimed. Setup tokens are single use; "
                "make a new one at the Bridge.") from exc
        raise ConnectError(f"the Bridge answered {exc.code} to the claim") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ConnectError(f"could not reach the Bridge: {exc}") from exc

    if not url.startswith("https://"):
        raise ConnectError(f"the Bridge returned {url[:60]!r}, which is not an access URL")
    return url


# --- polling ---------------------------------------------------------------------------------

def _instant(unix) -> str | None:
    if not unix:
        return None
    try:
        return datetime.fromtimestamp(int(unix), UTC).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def _cents(raw) -> tuple[int | None, str | None]:
    if raw is None or str(raw).strip() == "":
        return None, None
    try:
        return to_cents(str(raw)), None
    except MoneyError as exc:
        return None, str(exc)


class SimpleFIN:
    """One Bridge connection. Total contract: `poll` never raises."""

    name = NAME

    def __init__(self, url: str | None = None):
        self.url = url or access_url()

    def poll(self, window: Window | None) -> Poll:
        started = datetime.now(UTC).isoformat()
        if not self.url:
            return Poll(source=NAME, outcome="error", window=window,
                        errors=(FeedError(code="gen.unconfigured",
                                          msg="no access URL; run `spend feeds connect`"),),
                        note="not connected", detail={"started_at": started})

        query = {"version": "2", "pending": "1"}
        if window:
            query["start-date"] = str(int(datetime(
                window.start.year, window.start.month, window.start.day,
                tzinfo=UTC).timestamp()))
            query["end-date"] = str(int(datetime(
                window.end.year, window.end.month, window.end.day,
                tzinfo=UTC).timestamp()))
        target = (f"{self.url.rstrip('/')}/accounts?{urllib.parse.urlencode(query)}")

        began = time.monotonic()
        try:
            request = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                status, body = response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return self._failed(window, started, f"gen.http-{exc.code}",
                                f"the Bridge answered {exc.code}", status=exc.code,
                                body=exc.read().decode(errors="replace")[:4000])
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return self._failed(window, started, "gen.transport", str(exc))

        latency = int((time.monotonic() - began) * 1000)
        try:
            # parse_float=str, and it is load-bearing. The spec says amounts are numeric
            # strings; implementations emit bare JSON numbers. This guarantees the exact
            # decimal literal reaches `to_cents` either way, so no float is ever constructed
            # anywhere on this path.
            payload = json.loads(body, parse_float=str)
        except ValueError as exc:
            return self._failed(window, started, "gen.parse", str(exc), status=status,
                                body=body[:4000], latency_ms=latency)

        return self._read(payload, window, started, status, latency)

    def _failed(self, window, started, code, msg, *, status=None, body=None,
                latency_ms=None) -> Poll:
        return Poll(source=NAME, outcome="error", window=window, http_status=status,
                    errors=(FeedError(code=code, msg=msg),), note=msg,
                    # Exactly what came back, kept even when bad. The same rule
                    # `extractions.raw_response` follows, for the same reason.
                    detail={"started_at": started, "body": body, "latency_ms": latency_ms})

    def _read(self, payload: dict, window, started, status, latency) -> Poll:
        errors = tuple(
            FeedError(code=str(e.get("code") or "gen.unknown"), msg=str(e.get("msg") or ""),
                      account_id=e.get("account_id"), conn_id=e.get("conn_id"))
            for e in payload.get("errlist", []) or []
            if isinstance(e, dict))

        accounts, records = [], []
        for raw in payload.get("accounts", []) or []:
            if not isinstance(raw, dict):
                continue
            native = str(raw.get("id") or "")
            if not native:
                continue
            org = raw.get("org") or {}
            balance, _ = _cents(raw.get("balance"))
            available, _ = _cents(raw.get("available-balance"))
            accounts.append(Account(
                native_id=native, name=raw.get("name"),
                org_name=org.get("name") or org.get("domain"),
                org_domain=org.get("domain"), org_id=org.get("id"),
                currency=raw.get("currency") or "USD",
                balance_cents=balance, available_balance_cents=available,
                balance_at=_instant(raw.get("balance-date")),
                raw={k: v for k, v in raw.items() if k != "transactions"}))

            for txn in raw.get("transactions", []) or []:
                if not isinstance(txn, dict):
                    continue
                cents, why = _cents(txn.get("amount"))
                # `posted == 0` is how the protocol says "pending"; a falsy posted with an
                # explicit pending flag means the same thing.
                posted = _instant(txn.get("posted"))
                pending = bool(txn.get("pending")) or posted is None
                records.append(Record(
                    native_account=native, external_id=str(txn.get("id") or ""),
                    description=str(txn.get("description") or txn.get("payee") or ""),
                    amount_cents=cents, posted_at=posted,
                    transacted_at=_instant(txn.get("transacted_at")),
                    pending=pending, currency=raw.get("currency") or "USD",
                    payee=txn.get("payee"), memo=txn.get("memo"),
                    reject_reason=why, raw=txn))

        records = [r for r in records if r.external_id]
        unparseable = tuple(
            FeedError(code="gen.amount", msg=f"{r.description}: {r.reject_reason}",
                      account_id=r.native_account)
            for r in records if r.amount_cents is None)
        errors = errors + unparseable

        return Poll(
            source=NAME,
            # 'partial' with records is normal, not a contradiction: the accounts that
            # worked are still in the payload and throwing them away would lose real data.
            outcome="partial" if errors else "ok",
            accounts=tuple(accounts), records=tuple(records), errors=errors,
            window=window, http_status=status,
            detail={"started_at": started, "latency_ms": latency,
                    "accounts": len(accounts)})
