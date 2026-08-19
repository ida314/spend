# spend-tracker

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
it. `transactions` and `line_items` are derived, and `spend-tracker rebuild` recomputes
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
uv sync
uv run spend-tracker doctor            # what did this process actually resolve?
uv run spend-tracker ingest ~/r.jpg
uv run spend-tracker extract
uv run spend-tracker serve             # http://127.0.0.1:8089
uv run pytest                          # no GPU, no network, no sir
```

Deploy as rootless systemd `--user` units, then publish on the tailnet:

```bash
./scripts/service-install.sh
tailscale serve --service=svc:spend-tracker --https=443 http://127.0.0.1:8089
```

## Where things are

| | |
|---|---|
| database | `~/.local/share/spend-tracker/spendtracker.db` |
| receipts | `~/.local/share/spend-tracker/receipts/<ab>/<sha256>.jpg` |
| render cache | `~/.cache/spend-tracker/render/` — derived, delete freely |
| config | `~/.config/spend-tracker/config.toml`, overridden by `SPENDTRACKER_*` |

Back up the first two. `SPENDTRACKER_HOME` repoints all of them at once, which is how the
tests avoid the real database.

## Reading, not seeing

Extraction runs OCR locally and sends the model text, even though the model behind `sir`
is a vision-language model that could read the photo directly. That is a router
limitation, not a model one, and it is written up in
[docs/spike-vision.md](docs/spike-vision.md). `SPENDTRACKER_RENDER_MODE=image` is wired
and waiting.

## Not built yet

Emailed receipts. The schema, the ingest call and the text path are all in place; the
contract we will need from `email-tracker` — which is itself unwritten — is recorded in
[docs/email-ingest.md](docs/email-ingest.md).
