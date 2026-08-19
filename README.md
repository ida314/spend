# spend

Photograph a receipt; get a categorised transaction with its line items, and keep the
photograph next to it. Runs on the homelab, reads with the box's own model, and holds no
API keys because nothing leaves the tailnet.

```
iPhone ──tailnet──> :8089 ──> receipts/ on disk (content-addressed)
                       │
                  SQLite: receipts        append-only
                       │
              RapidOCR on this box        ~0.8s
                       │
              sir ──> Qwen3.6-27B         ~35s
                       │
                  extractions             append-only
                       │
        + corrections + categories.toml
                       ▼
            transactions, line_items      derived, rebuildable
```

## The one thing worth knowing

**Model output is truth; a transaction is a projection.**

`receipts`, `extractions` and `corrections` are append-only — nothing in this codebase
issues an `UPDATE` or `DELETE` against them, and a test traces every statement to prove
it. `transactions` and `line_items` are derived, and `spend rebuild` recomputes
all of them from the log.

That is not ceremony; it is what makes the fallible parts safe to improve:

- Re-read a receipt with a better prompt and the merchant you fixed by hand stays fixed.
- Edit `rules/categories.toml`, run `rebuild`, and eight months of history recategorise.
- Fix a bug in how a total is parsed and every historical row is correct — no backfill, no
  migration, no re-running the GPU.

The third one is not hypothetical. The first receipt this ever read contained `2 @ 0.745`,
a sub-cent unit price that the money parser read as `$745.00`. The fix was six lines in
`money.py` and a `rebuild`; no receipt was re-extracted.

## Failure is absence, never a wrong answer

Every `sir_client` error, every unparseable response, and every unreadable file becomes a
recorded failed attempt and **zero** transactions. A receipt the model could not read
shows up with its photo and an open edit form — the correction path doubles as manual
entry, so the app stays useful with the model down.

A page that shows nothing says why: the backlog and the last error, never a bare empty
list that could equally mean "you spent nothing" or "inference has been down for a week".

## Running it

```bash
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
uv sync
uv run spend ingest ~/r.jpg
uv run spend extract
uv run spend serve                     # http://127.0.0.1:8089
uv run pytest                          # no GPU, no network, no sir

./scripts/service-install.sh           # units + a nightly backup timer
```

The container bind-mounts the same `~/.local/share/spend` these commands use, so the two
paths see one database and one set of receipts. Switching is stopping one and starting the
other — no export, no import. Do not run both at once: each carries the extraction worker,
and two of those send the same receipt to `sir` twice.

## Where things are

| | |
|---|---|
| database | `~/.local/share/spend/spend.db` |
| receipts | `~/.local/share/spend/receipts/<ab>/<sha256>.jpg` |
| render cache | `~/.cache/spend/render/` — derived, delete freely |
| config | `~/.config/spend/config.toml`, overridden by `SPEND_*` |
| backups | `~/backups/spend/` — `spend backup`, nightly under either deploy path |

Back up the first two; `spend backup` does exactly that, and uses `VACUUM INTO` rather
than a file copy because a WAL database copied without its `-wal` opens cleanly and is
missing the last writes.

`SPEND_HOME` repoints the first four at once, which is how the tests avoid the real
database. The container sets the three XDG variables instead, so the same directories land
at `/var/lib/spend`, `/var/cache/spend` and `/etc/spend` inside it — the host paths above
are still where the bytes are.

## Reading, not seeing

Extraction runs OCR locally and sends the model text, even though the model behind `sir`
is a vision-language model that could read the photo directly. That is a router
limitation, not a model one, and it is written up in
[docs/spike-vision.md](docs/spike-vision.md). `SPEND_RENDER_MODE=image` is wired
and waiting.

## Not built yet

Emailed receipts. The schema, the ingest call and the text path are all in place; the
contract we will need from `email-tracker` — which is itself unwritten — is recorded in
[docs/email-ingest.md](docs/email-ingest.md).
