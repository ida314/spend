"""Settings, resolved from the environment once at import.

Module-level constants read from a `SPEND_` namespace, the same shape as
`jobtracker/config.py`. There is no dotenv: this service is started by systemd, and a unit
file is a better place to read a service's environment than a file the unit has to be
told about.

`config.toml` is read first where it exists, so a value can be set once by hand instead of
threaded through a unit file; the environment wins over it, because overriding a setting
for one invocation is what an override is for.
"""

from __future__ import annotations

import os
import tomllib

from spend.branding import ENV_PREFIX
from spend.paths import config_file

_file: dict = {}
if config_file().exists():
    _file = tomllib.loads(config_file().read_text())


def _get(name: str, default):
    if raw := os.environ.get(ENV_PREFIX + name):
        return raw
    return _file.get(name.lower(), default)


# --- inference -------------------------------------------------------------------------
# The tag every caller on this box sends. `sir` routes on it and forwards it unchanged.
MODEL = _get("MODEL", "nvidia/Qwen3.6-27B-NVFP4")

# Where `sir` is. `sir_client` reads SIR_BASE_URL itself; this is set into the environment
# by `serve` when given, so one variable configures the app rather than two.
SIR_BASE_URL = _get("SIR_BASE_URL", os.environ.get("SIR_BASE_URL", "http://127.0.0.1:8000"))

# Generous, because a queued request behind a model swap on this box waits minutes, not
# seconds, and a receipt that arrives late is worth strictly more than one that timed out.
EXTRACT_TIMEOUT = float(_get("EXTRACT_TIMEOUT", 300))

# --- rendering -------------------------------------------------------------------------
# 'ocr'   — RapidOCR on this box, then the text to the model. The only mode that works
#           today: `sir` types ChatMessage.content as `str`, so a content-block list with
#           an image in it is rejected before it reaches vLLM. See docs/spike-vision.md.
# 'image' — send the photo itself. The model supports it (Qwen3VLProcessor, a real ViT);
#           the router does not yet. Kept wired so this becomes a one-word change.
RENDER_MODE = _get("RENDER_MODE", "ocr")

# An iPhone photo is ~4000px on the long edge. Neither OCR nor a vision tower gains
# anything from that, and both pay for it, so a render is capped before either sees it.
MAX_EDGE = int(_get("MAX_EDGE", 1600))
JPEG_QUALITY = int(_get("JPEG_QUALITY", 88))

# --- server ----------------------------------------------------------------------------
# Loopback. The tailnet is the door: `tailscale serve --service=svc:spend`.
# Binding this to a routable address would publish an upload endpoint to the LAN.
HOST = _get("HOST", "127.0.0.1")
PORT = int(_get("PORT", 8089))

# One receipt at a time. Parallel extraction would only queue deeper inside `sir`, and on
# a box where one GPU is shared it can push another service's model out of residency.
WORKER_CONCURRENCY = 1

# Backoff when `sir` is unreachable. Receipts stay queued; nothing is dropped.
WORKER_IDLE_SECONDS = 5.0
WORKER_BACKOFF_MAX = 300.0

MAX_UPLOAD_BYTES = int(_get("MAX_UPLOAD_BYTES", 25 * 1024 * 1024))

# --- the store ---------------------------------------------------------------------
# The render cache is plaintext receipt content on a tmpfs shared with the database,
# so it is budgeted rather than unbounded. A 1600px JPEG is ~300 KB, so this is a few
# hundred receipts -- far more than the worker's backlog, which is the working set.
RENDER_CACHE_BYTES = int(_get("RENDER_CACHE_BYTES", 64 * 1024 * 1024))

# How long an unlock lasts before the store relocks itself.
LOCK_TTL_HOURS = float(_get("LOCK_TTL_HOURS", 8))

# Which deploy path `unlock`/`lock` should start and stop: systemd | compose | none.
# Explicit, because sniffing it would guess wrong on a box that has both installed --
# and this repo says in four places that both must never run at once.
DEPLOY = _get("DEPLOY", "none")

# How long an account may go without new data before `doctor` calls it stale.
# Three days: the Bridge refreshes daily, so two consecutive misses is a signal
# and one is a bank having a slow night.
FEED_STALE_DAYS = int(_get("FEED_STALE_DAYS", 3))

TZ = _get("TZ", os.environ.get("TZ", "America/New_York"))
