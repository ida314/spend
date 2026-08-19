"""The one extractor: the box's own model, reached through `sir`.

`sir` is the front door for inference here — it decides which model is GPU-resident and
queues around a swap, so a request may wait minutes before it generates a token. That is
why the timeout is measured in minutes and why `run_llm` is used rather than raw HTTP:
the client submits asynchronously and polls at the router's pace, so a slow answer is a
slow answer rather than a dropped connection.

Nothing here leaves the tailnet. No API key exists to leak.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from pydantic import ValidationError
from sir_client import run_llm
from sir_client.errors import SirClientError

from spend import config
from spend.extract.base import Extraction
from spend.render import Document
from spend.schema import PROMPT_VERSION, RECEIPT_JSON_SCHEMA, ReceiptData

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / f"{PROMPT_VERSION}.md"
SYSTEM_PROMPT = PROMPT_PATH.read_text()


def _user_content(doc: Document):
    if doc.mode in ("ocr", "text"):
        # Fenced so the model can tell the receipt from the instructions, and so a receipt
        # that happens to contain the word "ignore" is data rather than a second prompt.
        return f"<receipt>\n{doc.text}\n</receipt>"
    if doc.mode == "image":
        return [
            {"type": "image_url",
             "image_url": {"url": f"data:{doc.image_mime};base64,{doc.image_b64}"}},
            {"type": "text", "text": "Extract this receipt."},
        ]
    raise ValueError(f"unknown document mode {doc.mode!r}")


def build_body(doc: Document) -> dict:
    return {
        "model": config.MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_content(doc)},
        ],
        # Zero, because two runs over one receipt disagreeing is a bug report nobody can
        # act on. Re-extraction is meant to change an answer only when the prompt or the
        # model changed, and both of those are recorded on the row.
        "temperature": 0,
        "max_tokens": 2000,
        # Constrains generation rather than checking it afterwards, so a long receipt
        # cannot end in a truncated object that parses as far as it got. `sir` forwards
        # unknown fields untouched, which is what lets this reach vLLM at all.
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "receipt", "schema": RECEIPT_JSON_SCHEMA, "strict": True},
        },
    }


class SirExtractor:
    """Reads a rendered receipt with the model behind `sir`."""

    model = config.MODEL
    prompt_version = PROMPT_VERSION

    async def extract(self, doc: Document) -> Extraction:
        body = build_body(doc)
        started = time.monotonic()
        try:
            completion = await run_llm(config.MODEL, body, timeout=config.EXTRACT_TIMEOUT)
        except SirClientError as exc:
            # Every sir_client failure lands here: ModelNotRouted, JobFailed, JobLost,
            # JobCancelled, RequestTimeout, TransportError. All of them mean the same
            # thing to a receipt — no answer yet — and all of them leave it queued.
            return Extraction(status="error", note=f"{type(exc).__name__}: {exc}",
                              latency_ms=_ms(started))
        except Exception as exc:  # noqa: BLE001 - a bug here must not take the worker down
            log.exception("unexpected extraction failure")
            return Extraction(status="error", note=f"{type(exc).__name__}: {exc}",
                              latency_ms=_ms(started))

        latency = _ms(started)
        try:
            raw = completion["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            return Extraction(status="error", raw_response=json.dumps(completion)[:4000],
                              note=f"malformed completion: {exc}", latency_ms=latency)
        if not raw or not raw.strip():
            return Extraction(status="invalid", raw_response=raw,
                              note="model returned an empty response", latency_ms=latency)

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return Extraction(status="invalid", raw_response=raw,
                              note=f"not JSON: {exc}", latency_ms=latency)
        try:
            data = ReceiptData.model_validate(parsed)
        except ValidationError as exc:
            return Extraction(status="invalid", raw_response=raw,
                              note=f"schema mismatch: {exc.error_count()} errors",
                              latency_ms=latency)

        return Extraction(status="ok", raw_response=raw, data=data, latency_ms=latency)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
