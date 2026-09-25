"""The web app: a list, a detail page, and a form to fix what the model got wrong.

Server-rendered Jinja, no build step, and the small amount of JavaScript present submits
forms — it does not build the page. That is deliberate: the only client is a phone, and a
page that renders without JS is a page that works on a bad connection in a shop doorway.

No authentication. The app binds loopback and the tailnet is the door, which is the same
posture as job-tracker's dashboard. Worth naming because this surface accepts uploads:
if that ever stops feeling right, a bearer token belongs in `_guard` below and nowhere
else.
"""

from __future__ import annotations

import calendar
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from spend import config, keys, paths, seal, store
from spend.branding import NAME, TAGLINE
from spend.money import format_cents
from spend.schema import CATEGORIES
from spend.service import (
    IngestError,
    blob_bytes,
    correct,
    ingest_bytes,
    ingest_text,
    summary,
)

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["money"] = format_cents

EDITABLE = ("merchant", "merchant_location", "purchased_on", "total", "subtotal",
            "tax", "tip", "category")


def create_app(extractor=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        paths.ensure_dirs()
        conn = store.connect()
        ran = store.migrate(conn)
        conn.close()
        if ran:
            log.info("applied migrations: %s", ", ".join(ran))

        from spend.extract.sir import SirExtractor
        from spend.worker import Worker
        app.state.worker = Worker(extractor or SirExtractor())
        app.state.worker.start()
        try:
            yield
        finally:
            await app.state.worker.stop()

    app = FastAPI(title=NAME, description=TAGLINE, lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # --- pages ---------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, month: str | None = None, category: str | None = None,
              review: int = 0):
        conn = store.connect()
        try:
            where = ["t.deleted = 0"]
            params: list = []
            if month:
                where.append("substr(t.purchased_on, 1, 7) = ?")
                params.append(month)
            if category:
                where.append("t.category = ?")
                params.append(category)
            if review:
                where.append("t.status = 'needs_review'")

            rows = list(conn.execute(
                f"SELECT t.*, r.sha256 FROM transactions t JOIN receipts r ON r.id = t.receipt_id"
                f" WHERE {' AND '.join(where)}"
                f" ORDER BY COALESCE(t.purchased_on, '') DESC, t.receipt_id DESC", params))

            months = [r[0] for r in conn.execute(
                "SELECT DISTINCT substr(purchased_on,1,7) m FROM transactions"
                " WHERE deleted=0 AND purchased_on IS NOT NULL ORDER BY m DESC")]
            health = summary(conn)
        finally:
            conn.close()

        # Grouped in Python rather than SQL: the page wants a running total per month and
        # a heading between groups, which is presentation, not a query.
        groups: OrderedDict[str, dict] = OrderedDict()
        for r in rows:
            key = (r["purchased_on"] or "")[:7] or "undated"
            g = groups.setdefault(key, {"label": _month_label(key), "rows": [], "total": 0})
            g["rows"].append(r)
            if r["total_cents"] and r["status"] in ("ok", "needs_review"):
                g["total"] += r["total_cents"]

        return templates.TemplateResponse(request, "list.html", {
            "groups": groups, "months": months, "categories": CATEGORIES,
            "sel_month": month, "sel_category": category, "review": review,
            "health": health, "worker_error": getattr(request.app.state.worker,
                                                      "last_error", None),
        })

    @app.get("/r/{receipt_id}", response_class=HTMLResponse)
    def detail(request: Request, receipt_id: int):
        conn = store.connect()
        try:
            receipt = store.get_receipt(conn, receipt_id)
            if receipt is None:
                raise HTTPException(404, "no such receipt")
            txn = conn.execute("SELECT * FROM transactions WHERE receipt_id=?",
                               (receipt_id,)).fetchone()
            items = list(conn.execute(
                "SELECT * FROM line_items WHERE receipt_id=? ORDER BY line_no", (receipt_id,)))
            extractions = store.extractions_for(conn, receipt_id)
            corrections = store.corrections_for(conn, receipt_id)
        finally:
            conn.close()

        spoken = None
        if receipt["mime"] == "text/plain":
            try:
                spoken = blob_bytes(receipt).decode("utf-8", "replace")
            except (keys.Locked, seal.SealError):
                spoken = "(locked)"

        return templates.TemplateResponse(request, "detail.html", {
            "receipt": receipt, "txn": txn, "items": items,
            "extractions": list(reversed(extractions)), "corrections": corrections,
            "categories": CATEGORIES, "editable": EDITABLE, "spoken": spoken,
        })

    @app.get("/img/{sha256}")
    def image(sha256: str):
        """Addressed by content hash, not by receipt id.

        The old `/r/{id}/image` was served `immutable, max-age=31536000`, which tells the
        phone never to revalidate -- a bet that a receipt's integer id never changes. Ids are
        now assigned by replay, and although replay is deterministic, a URL cached forever is
        not a good place to rest on that. Hashing the URL makes the header correct by
        construction rather than correct-if-nothing-shifts.
        """
        if not sha256.isalnum() or len(sha256) != 64:
            raise HTTPException(404, "no such receipt")
        conn = store.connect()
        try:
            row = conn.execute("SELECT * FROM receipts WHERE sha256=?", (sha256,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(404, "no such receipt")
        try:
            data = blob_bytes(row)
        except keys.Locked as exc:
            raise HTTPException(503, str(exc)) from exc
        except seal.SealError as exc:
            raise HTTPException(410, f"the file behind this receipt is unreadable: {exc}") from exc
        # Private, because a receipt is nobody else's business to cache. Worth naming: the
        # phone's own HTTP cache now holds plaintext receipt images. That is the user's own
        # device and it is fine, but it should be said rather than discovered.
        return Response(data, media_type=row["mime"], headers={
            "ETag": f'"{sha256}"',
            "Cache-Control": "private, max-age=31536000, immutable"})

    # --- actions -------------------------------------------------------------------

    @app.post("/upload")
    async def upload(request: Request, files: list[UploadFile]):
        conn = store.connect()
        added, skipped, last = 0, [], None
        try:
            for f in files:
                data = await f.read()
                if not data:
                    continue
                try:
                    rid, is_new = ingest_bytes(
                        conn, data, mime=f.content_type or "", source="upload")
                except IngestError as exc:
                    skipped.append(f"{f.filename}: {exc}")
                    continue
                conn.commit()
                added += is_new
                last = rid
        finally:
            conn.close()

        request.app.state.worker.nudge()
        if added == 1 and last and not skipped:
            return RedirectResponse(f"/r/{last}", status_code=303)
        query = "?skipped=" + "; ".join(skipped) if skipped else ""
        return RedirectResponse(f"/{query}", status_code=303)

    @app.post("/speak")
    def speak(request: Request, text: str = Form("")):
        """A receipt in words. The phone keyboard's mic does the listening."""
        conn = store.connect()
        try:
            rid, _ = ingest_text(conn, text)
            conn.commit()
        except IngestError as exc:
            return RedirectResponse(f"/?skipped={exc}", status_code=303)
        finally:
            conn.close()
        request.app.state.worker.nudge()
        return RedirectResponse(f"/r/{rid}", status_code=303)

    @app.post("/r/{receipt_id}/correct")
    async def apply_correction(request: Request, receipt_id: int):
        form = await request.form()
        changes = {k: (str(v).strip() or None) for k, v in form.items() if k in EDITABLE}
        changes |= {k: (str(v).strip() or None) for k, v in form.items()
                    if k.startswith("item.") and k.count(".") == 2}
        conn = store.connect()
        try:
            if store.get_receipt(conn, receipt_id) is None:
                raise HTTPException(404, "no such receipt")
            correct(conn, receipt_id, changes)
        finally:
            conn.close()
        return RedirectResponse(f"/r/{receipt_id}", status_code=303)

    @app.post("/r/{receipt_id}/reextract")
    def reextract(request: Request, receipt_id: int):
        """Queue another read. The existing answer stays on the log either way."""
        conn = store.connect()
        try:
            if store.get_receipt(conn, receipt_id) is None:
                raise HTTPException(404, "no such receipt")
            # An extraction the projector will ignore, recorded so the receipt re-enters
            # the pending queue without any table being mutated.
            store.insert_extraction(
                conn, receipt_id=receipt_id, status="invalid", model=config.MODEL,
                prompt_version="requeue", render_mode=config.RENDER_MODE,
                note="re-read requested")
            conn.commit()
        finally:
            conn.close()
        request.app.state.worker.nudge()
        return RedirectResponse(f"/r/{receipt_id}", status_code=303)

    @app.post("/r/{receipt_id}/delete")
    def delete(receipt_id: int):
        conn = store.connect()
        try:
            if store.get_receipt(conn, receipt_id) is None:
                raise HTTPException(404, "no such receipt")
            correct(conn, receipt_id, {"deleted": "1"})
        finally:
            conn.close()
        return RedirectResponse("/", status_code=303)

    # --- health --------------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/doctor")
    def doctor(request: Request):
        conn = store.connect()
        try:
            body = summary(conn) | {
                "schema_version": store.schema_version(conn),
                "model": config.MODEL, "sir": config.SIR_BASE_URL,
                "render_mode": config.RENDER_MODE,
                "worker_last_error": getattr(request.app.state.worker, "last_error", None),
                "db": str(paths.db_path()), "blobs": str(paths.blobs_dir()),
                "locked": not keys.is_unlocked(),
            }
        finally:
            conn.close()
        return JSONResponse(body)

    return app


def _month_label(key: str) -> str:
    if key == "undated":
        return "No date"
    y, m = key.split("-")
    return f"{calendar.month_name[int(m)]} {y}"


app = create_app
