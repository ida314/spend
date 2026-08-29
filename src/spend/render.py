"""A receipt file, turned into something a model can read.

Two modes, one output type. `ocr` runs RapidOCR on this box and hands the model text;
`image` hands it the picture. Both produce a `Document`, so `extract` has one code path
and one prompt, and the email path's HTML body will join as a third source with no new
prompt to keep in step.

`ocr` is what ships. Not because it reads better — the model behind `sir` is a real
vision-language model and would read the photo directly — but because `sir` types
`ChatMessage.content` as `str` and rejects the content-block list that carries an image.
See docs/spike-vision.md. The `image` path below is written and correct; it is one
setting away from being usable the day the router stops filtering.
"""

from __future__ import annotations

import base64
import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps

from spend import config, paths

# RapidOCR loads ~15 MB of ONNX models and costs about a second to construct. One receipt
# at a time means one instance, built on first use so that `--help` and the migrations do
# not pay for an OCR engine they never call.
_ocr = None


@dataclass(frozen=True)
class Document:
    """What gets sent. `mode` is recorded on the extraction so a bad batch is traceable."""

    mode: str                       # 'ocr' | 'image' | 'text'
    text: str | None = None
    image_b64: str | None = None
    image_mime: str = "image/jpeg"
    meta: dict = field(default_factory=dict)


class RenderError(RuntimeError):
    """The file could not be turned into anything readable."""


def _register_heif() -> None:
    try:
        import pillow_heif  # noqa: PLC0415
    except ImportError:
        return
    pillow_heif.register_heif_opener()


def load_image(data: bytes) -> Image.Image:
    """From bytes, not a path. The receipt originals are sealed, so there is no plaintext
    file to open -- and taking bytes here removes the last place image content could be read
    from an unencrypted path by accident."""
    _register_heif()
    try:
        img = Image.open(io.BytesIO(data))
        # A phone writes the sensor's orientation into EXIF rather than rotating pixels.
        # OCR reads a sideways receipt as noise, so this is load-bearing, not cosmetic.
        img = ImageOps.exif_transpose(img)
        return img.convert("RGB")
    except OSError as exc:
        raise RenderError(f"cannot decode {len(data)} bytes of image: {exc}") from exc


def downscale(img: Image.Image, max_edge: int | None = None) -> Image.Image:
    max_edge = max_edge or config.MAX_EDGE
    if max(img.size) <= max_edge:
        return img
    scale = max_edge / max(img.size)
    return img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                      Image.LANCZOS)


def render_jpeg(data: bytes, sha256: str) -> bytes:
    """The downscaled JPEG both modes work from, cached by content hash.

    The cache is on tmpfs now and it holds plaintext receipt content, so it is budgeted
    rather than unbounded -- see `evict`.
    """
    cache = paths.render_dir() / f"{sha256}.jpg"
    if cache.exists():
        return cache.read_bytes()
    buf = io.BytesIO()
    downscale(load_image(data)).save(buf, "JPEG", quality=config.JPEG_QUALITY)
    jpeg = buf.getvalue()
    cache.parent.mkdir(parents=True, exist_ok=True)
    evict()
    cache.write_bytes(jpeg)
    return jpeg


def layout_text(result) -> str:
    """Rebuild visual lines from OCR boxes.

    RapidOCR returns boxes in no particular order, and a receipt read as a flat list of
    fragments loses the one relationship that matters: that a description and the amount
    to its right are the same purchase. Clustering by vertical overlap and sorting each
    cluster by x restores "SOURDOUGH LOAF  3.49" as one line, which is what the model is
    actually good at reading.

    The 0.6 threshold is a fraction of box height rather than a pixel count so it holds
    across the range of resolutions a phone camera produces.
    """
    boxes = []
    for box, text, _conf in result or []:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        boxes.append({"text": text, "x0": min(xs), "yc": (min(ys) + max(ys)) / 2,
                      "h": max(ys) - min(ys)})
    boxes.sort(key=lambda b: b["yc"])

    lines: list[list[dict]] = []
    current: list[dict] = []
    for b in boxes:
        if current and abs(b["yc"] - current[-1]["yc"]) > 0.6 * max(b["h"], current[-1]["h"]):
            lines.append(current)
            current = []
        current.append(b)
    if current:
        lines.append(current)

    return "\n".join(
        "  ".join(b["text"] for b in sorted(line, key=lambda b: b["x0"])) for line in lines
    )


def ocr_text(jpeg: bytes, sha256: str) -> str:
    cache = paths.render_dir() / f"{sha256}.txt"
    if cache.exists():
        return cache.read_text()

    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR  # noqa: PLC0415
        _ocr = RapidOCR()

    import numpy as np  # noqa: PLC0415
    result, _elapse = _ocr(np.array(Image.open(io.BytesIO(jpeg)).convert("RGB")))
    text = layout_text(result)
    if not text.strip():
        raise RenderError("OCR found no text in this image")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text)
    return text


def render(data: bytes, sha256: str, mode: str | None = None) -> Document:
    mode = mode or config.RENDER_MODE
    jpeg = render_jpeg(data, sha256)
    if mode == "image":
        return Document(mode="image", image_b64=base64.b64encode(jpeg).decode(),
                        meta={"bytes": len(jpeg)})
    if mode == "ocr":
        text = ocr_text(jpeg, sha256)
        return Document(mode="ocr", text=text,
                        meta={"lines": text.count("\n") + 1, "chars": len(text)})
    raise RenderError(f"unknown render mode {mode!r}")


def render_text(body: str) -> Document:
    """For a receipt that was already text — an emailed confirmation, later."""
    return Document(mode="text", text=body, meta={"chars": len(body)})


def evict(budget: int | None = None) -> int:
    """Keep the render cache inside its budget, oldest first. Returns bytes removed.

    It lives on a tmpfs measured in hundreds of megabytes and shared with the SQLite cache,
    so it cannot be allowed to grow to the size of the corpus. The working set is the
    worker's backlog, not the archive, and everything here is regenerable from a sealed blob.
    """
    budget = config.RENDER_CACHE_BYTES if budget is None else budget
    root = paths.render_dir()
    if not root.exists():
        return 0
    files = [(f.stat().st_mtime, f.stat().st_size, f) for f in root.iterdir() if f.is_file()]
    total = sum(size for _, size, _ in files)
    freed = 0
    for _, size, f in sorted(files):
        if total - freed <= budget:
            break
        f.unlink(missing_ok=True)
        freed += size
    return freed


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
