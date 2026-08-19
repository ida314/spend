"""The background loop that reads queued receipts.

One receipt at a time, in the API process. Concurrency of one is not a simplification:
requests would only queue deeper inside `sir`, and on a box where one GPU is shared,
several receipts in flight can hold a model resident that another service is waiting to
swap out. The queue is `store.pending_receipt_ids`, derived rather than stored, so a
receipt is retried until it succeeds and nothing has to be re-enqueued by hand.

Failure is backed off, never dropped. If `sir` is unreachable the interval grows to five
minutes and the receipts sit there; the list page says so rather than showing an empty
list that looks like "no spending".
"""

from __future__ import annotations

import asyncio
import logging

from spend import config, store
from spend.extract.base import Extractor
from spend.service import extract_one

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, extractor: Extractor):
        self.extractor = extractor
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._backoff = 0.0
        self.last_error: str | None = None

    def nudge(self) -> None:
        """Called after an upload so a receipt is read now rather than at the next tick."""
        self._wake.set()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="spend-worker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            try:
                did_work = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop outlives any one receipt
                log.exception("worker tick failed")
                did_work = False

            delay = config.WORKER_IDLE_SECONDS if did_work is not False else max(
                config.WORKER_IDLE_SECONDS, self._backoff)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except (TimeoutError, asyncio.TimeoutError):
                pass
            self._wake.clear()

    async def _tick(self) -> bool:
        conn = store.connect()
        try:
            pending = store.pending_receipt_ids(conn)
            if not pending:
                self._backoff = 0.0
                return False
            rid = pending[0]
            result = await extract_one(conn, rid, self.extractor)
        finally:
            conn.close()

        if result.ok:
            self._backoff = 0.0
            self.last_error = None
            log.info("extracted receipt %s in %sms", rid, result.latency_ms)
            return True

        # A backend that is down fails instantly, which without a backoff would spin
        # through the whole queue burning a log line per receipt per second.
        self._backoff = min(max(self._backoff * 2, 15.0), config.WORKER_BACKOFF_MAX)
        self.last_error = result.note
        log.warning("receipt %s: %s (retrying in %.0fs)", rid, result.note, self._backoff)
        return False
