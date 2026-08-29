# spend

Photograph a receipt, or let it pull your statements — get a categorised transaction
either way. Runs on the homelab, reads with the box's own model, and keeps everything it
stores encrypted at rest.

```
iPhone ──tailnet──> :8089 ─┐                 SimpleFIN ─┐   CSV/OFX drop ─┐
                           │                            │                 │
                RapidOCR ──┤  ~0.8s                     └───── sources/ ───┘
                sir ───────┤  ~35s                              │
                Qwen3.6-27B│                                    │
                           ▼                                    ▼
                   ┌──────────────────────────────────────────────────┐
                   │  log/<ab>/<sha256>.age   append-only, sealed      │
                   │  blobs/<ab>/<sha256>.age the photographs          │
                   └──────────────────────────────────────────────────┘
                           │  spend unlock: decrypt, replay
                           ▼
                   SQLite on tmpfs — a cache, gone at `spend lock`
                           │
        + corrections + categories.toml + merchants.toml + flows.toml
                           ▼
     transactions · feed_transactions · accounts   derived, rebuildable
                           │
                           ▼
                   spend agent build → a directory an agent reads
```

## The two things worth knowing

**Model output is truth; a transaction is a projection.**

`receipts`, `extractions`, `corrections` and the four `feed_*` tables are append-only —
nothing in this codebase issues an `UPDATE` or `DELETE` against them, and a test traces
every statement to prove it. `transactions`, `feed_transactions`, `line_items` and
`accounts` are derived, and `spend rebuild` recomputes all of them from the log.

That is not ceremony; it is what makes the fallible parts safe to improve:

- Re-read a receipt with a better prompt and the merchant you fixed by hand stays fixed.
- Edit `rules/categories.toml`, run `rebuild`, and eight months of history recategorise.
- Fix a bug in how a total is parsed and every historical row is correct — no backfill, no
  migration, no re-running the GPU.

The third one is not hypothetical. The first receipt this ever read contained `2 @ 0.745`,
a sub-cent unit price that the money parser read as `$745.00`. The fix was six lines in
`money.py` and a `rebuild`; no receipt was re-extracted.

**The database is a cache. The sealed log is the only thing that must survive.**

That is the same idea one step further. Truth lives on disk as age-encrypted files, one per
event, and SQLite is rebuilt from them into tmpfs at `spend unlock` and deleted at
`spend lock`. Nothing durable is ever plaintext.

The shape has a payoff beyond privacy: age encrypts to a *public* key, so the nightly bank
pull holds nothing secret. It appends sealed transactions all night and is structurally
incapable of reading one back. Your statements keep arriving while the store is locked.

**Lose the passphrase and the data is gone.** There is no backdoor, no reset and no support
line. `spend init` prints a second, paper-backup key exactly once — write it down. That is
the only thing standing between a forgotten passphrase and losing everything.

See [docs/encryption.md](docs/encryption.md), which also carries the one prerequisite this
cannot do for you: **encrypted swap**. tmpfs pages get paged out, and an unencrypted
swapfile puts the decrypted ledger back on the disk. `spend doctor` checks it every run.

## Failure is absence, never a wrong answer

Every `sir_client` error, every unparseable response, and every unreadable file becomes a
recorded failed attempt and **zero** transactions. A receipt the model could not read
shows up with its photo and an open edit form — the correction path doubles as manual
entry, so the app stays useful with the model down.

A page that shows nothing says why: the backlog and the last error, never a bare empty
list that could equally mean "you spent nothing" or "inference has been down for a week".

## Running it

```bash
spend init                                # once. Write the printed backup key on PAPER.
spend unlock                              # decrypts, replays the log, starts the service
docker compose up -d --build
docker compose exec spend spend doctor    # what did this process actually resolve?
tailscale serve --service=svc:spend --https=443 http://127.0.0.1:8089
```

Then open `https://spend.<your-tailnet>.ts.net/` and add it to the home screen. The
container publishes on the host's `127.0.0.1` only — the tailnet is the door, and
`tailscale serve` on the host is what opens it.

The image exists for one reason: OCR. `rapidocr` brings `onnxruntime` and `opencv`, which
want system libraries the box does not otherwise have, and `sir-client` comes from git
rather than PyPI. Pinning that in an image stops the deploy box's Python and a laptop's
Python from being two separate questions.

`sir` is assumed to be on the host at `:8000`, reached as `host.docker.internal`. Point it
elsewhere with `SPEND_SIR_BASE_URL` in a `.env` beside `compose.yaml`.

```bash
docker compose logs -f spend              # what is it doing
docker compose ps                         # healthcheck hits /healthz
mkdir -p ~/backups/spend && docker compose run --rm backup
```

Backups are one-shot and scheduled from the host, because the host already has a timer and
a container that sleeps until 03:30 is a worse one:

```cron
30 3 * * * cd /srv/spend && docker compose run --rm backup
```

### Without Docker

`uv` and rootless systemd `--user` units still work and are still supported — for a box
with no Docker, and for developing against the real database:

```bash
uv sync --extra dev
uv run spend init                      # once
uv run spend unlock
uv run spend ingest ~/r.jpg
uv run spend extract
uv run spend serve                     # http://127.0.0.1:8089
uv run pytest                          # no GPU, no network, no sir

./scripts/service-install.sh           # units + a nightly backup timer
```

The container bind-mounts the same `~/.local/share/spend` these commands use, so the two
paths see one database and one set of receipts. Switching is stopping one and starting the
other — no export, no import. Do not run both at once: each carries the extraction worker,
and two of those send the same receipt to `sir` twice. Worse now — `spend unlock` deletes
the cache out from under whatever is holding it. `keys.exclusive()` makes that fail loudly
rather than silently, but do not rely on it.

## Where things are

Three roots, divided by their relationship with **the key**.

| | |
|---|---|
| sealed log | `~/.local/share/spend/log/<ab>/<sha256>.age` — the only irreplaceable thing |
| sealed photographs | `~/.local/share/spend/blobs/<ab>/<sha256>.age` |
| statement drop folder | `~/.local/share/spend/drop/inbox/` — CSV and OFX go here |
| recipients | `~/.config/spend/recipients.txt` — public keys, not secret |
| identity | `~/.config/spend/identity.age` — wrapped in your passphrase |
| accounts | `~/.config/spend/accounts.toml` — which native account is which of yours |
| cache, workspace | `$XDG_RUNTIME_DIR/spend/` — plaintext, tmpfs, gone at `lock` |
| backups | `~/backups/spend/` — ciphertext, so this runs while locked |

`spend backup` copies the first two plus the wrapped identity. There is no `VACUUM INTO`
any more and no database in a backup: the database is a cache, so a backup is a file copy
of things that are already encrypted, and the nightly timer never holds a key.

`SPEND_HOME` repoints everything at once, which is how the tests avoid the real store. The
container sets `XDG_DATA_HOME`, `XDG_CONFIG_HOME` and `SPEND_RUNTIME_DIR` instead, and
bind-mounts the host's own tmpfs at `/run/spend` so that `spend unlock` on the host unlocks
the container too — one passphrase and one cache, not two that can disagree.

## Reading, not seeing

Extraction runs OCR locally and sends the model text, even though the model behind `sir`
is a vision-language model that could read the photo directly. That is a router
limitation, not a model one, and it is written up in
[docs/spike-vision.md](docs/spike-vision.md). `SPEND_RENDER_MODE=image` is wired
and waiting.

## Statements

```bash
spend feeds connect <setup-token>   # once, from bridge.simplefin.org
spend feeds sync                    # the nightly timer runs this
spend feeds import                  # CSV/OFX dropped in ~/.local/share/spend/drop/inbox
```

Capital One over SimpleFIN, Apple Card by monthly export — **take the OFX, not the CSV**, it
carries stable ids. Both land in `feed_transactions`, which is kept deliberately separate
from receipt-derived `transactions`: matching the two is a later feature, and the fields
that make that join cheap are already indexed.

Every row carries `net_spend_cents`, which is zero for transfers, card payments and income.
Sum it over anything, with no `WHERE` clause, and the answer is right. That matters because
a card payment appears in the ledger **twice** — one leg per account — and neither is
spending. [docs/bank-feeds.md](docs/bank-feeds.md) works the example through.

## Handing it to an agent

```bash
spend agent build
cd $(spend agent path)
```

A directory holding partitioned JSONL for grep, a rebuilt `ledger.db` for aggregation,
eight worked queries, and a `CLAUDE.md` that states the sign convention and the units
before an agent touches a number. It lives on tmpfs and is gone at `spend lock`.

Receipt-derived rows are materialised alongside the statements, in separate files, and
every one of them nets to zero — enforced by a `CHECK` constraint. The two streams describe
overlapping reality and are **not** deduplicated, so the arithmetic is what stops an agent
adding a photographed lunch to the statement line for the same lunch.

## Not built yet

Emailed receipts. The schema, the ingest call and the text path are all in place; the
contract we will need from `email-tracker` — which is itself unwritten — is recorded in
[docs/email-ingest.md](docs/email-ingest.md).

Matching receipts to statement lines. `queries/07-receipt-candidates.sql` proposes pairs
and asserts nothing; `duplicate_suspect` is the primitive the real matcher will reuse.
