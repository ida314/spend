# syntax=docker/dockerfile:1

# Two stages. `uv` resolves and builds the venv; the runtime image carries that venv and
# nothing else but the shared libraries the wheels dlopen. The build reads `uv.lock`, so
# the image and `uv sync` on a laptop install the same resolution — "works under uv, broken
# in the container" is not a failure this can have quietly.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

# `sir-client` is a git dependency (pyproject, [tool.uv.sources]) and uv shells out to git
# to fetch it. Nothing else in the tree needs a compiler: every wheel here is prebuilt for
# both linux/amd64 and linux/arm64.
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies before source, and without the project itself: this layer is keyed on the
# lock file alone, so editing src/ does not re-fetch 20MB of onnxruntime and the OCR models.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --no-install-project

# `--no-editable` builds a real wheel and installs it into the venv. Without it uv drops a
# .pth pointing at /app/src, which does not exist in the runtime stage — the venv copies
# across fine and then `import spend` fails at the first command.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim-bookworm

# opencv comes in under rapidocr-onnxruntime and dlopens libGL and glib at import; onnxruntime
# wants libgomp. Everything else is inside the wheels. Missing these fails at the first
# receipt rather than at startup, which is why they are here and not discovered in prod.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# uid 1000 to match the usual single-user homelab box, so a bind-mounted volume is writable
# without chown. Override with compose's `user:` if yours differs.
RUN useradd --create-home --uid 1000 --user-group spend

COPY --from=build --chown=1000:1000 /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# The three roots stay three roots (see src/spend/paths.py): /var/lib is irreplaceable and
# is what you back up, /var/cache is derived and can be dropped on the floor, /etc is by
# hand. SPEND_HOME would collapse them into one, which would put the render cache inside
# the volume that has to survive.
ENV XDG_DATA_HOME=/var/lib \
    XDG_CACHE_HOME=/var/cache \
    XDG_CONFIG_HOME=/etc

# Pre-created and owned, because the process is not root and `ensure_dirs()` would be
# creating /etc/spend. A fresh named volume mounted over one of these inherits its
# ownership from the image; a bind mount does not, and is yours to chown.
RUN install -d -o 1000 -g 1000 /var/lib/spend /var/cache/spend /etc/spend /backups

# Loopback inside a container reaches nothing outside it. The isolation here is the
# published port, which compose pins to the host's 127.0.0.1 — see compose.yaml.
ENV SPEND_HOST=0.0.0.0 \
    SPEND_PORT=8089 \
    SPEND_BACKUP_DIR=/backups

USER spend
EXPOSE 8089

# No curl in slim, and adding it to answer one request every 30s is not a trade. `/healthz`
# is served by the app itself, so this proves the ASGI loop is alive, not just the process.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ['SPEND_PORT'] + '/healthz', timeout=4)"

ENTRYPOINT ["spend"]
CMD ["serve"]
