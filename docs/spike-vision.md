# Can the box read a receipt photo? (M0, 2026-08-18)

Run before any code in this repo existed, to decide whether extraction sends the model an
image or text. Two questions, two different answers.

## 1. Does the model do vision? Yes.

`nvidia/Qwen3.6-27B-NVFP4`, from its own `config.json`:

```
architectures       ['Qwen3_5ForConditionalGeneration']
model_type          qwen3_5
image_token_id      248056
video_token_id      248057
language_model_only False
vision_config       depth 27, hidden 1152, patch 16, num_position_embeddings 2304
processor_class     Qwen3VLProcessor   (preprocessor_config.json)
```

A real 27-layer vision tower. The `--limit-mm-per-prompt.image 8` and
`--mm-processor-cache-gb 4` flags in `stupid-inference-router/deploy/compose.yaml` were
correct, not vestigial — the engine was started expecting images.

## 2. Can we send it one through `sir`? No.

```
POST http://gx10:8000/v1/chat/completions
{"messages":[{"role":"user","content":[{"type":"image_url","image_url":{...}}, ...]}]}

{"detail":[{"type":"string_type","loc":["body","messages",0,"content"],
            "msg":"Input should be a valid string"}]}
```

`stupid-inference-router/src/sir/schemas.py:35` types `ChatMessage.content` as
`str | None`. The OpenAI content-block list — the only way a multimodal request is
written — fails validation inside the router, before anything reaches vLLM.

This is narrow. `ChatCompletionRequest` carries `extra="allow"`, so `response_format`,
`guided_json` and every other backend extra pass through untouched; we depend on that and
it works. It is only `messages[].content` that is typed, and only that field blocks this.

A fix was written and verified in a throwaway clone (widen to
`str | list[dict[str, Any]] | None`, teach `render_prompt` to flatten a part list — that
function feeds only the mock's word-count estimate when a backend reports no usage). All
162 of `sir`'s tests passed, and the base64 image survived `to_generation_request`
byte-for-byte. **It was not applied.** `sir` fronts every service on this box, and
changing it was out of scope for a spending tracker.

## What ships

Local OCR, then the text to the same model.

`render.py` runs RapidOCR (`rapidocr-onnxruntime` — pip only, no tesseract, no sudo, no
GPU), reconstructs visual lines from the box geometry, and sends that. Measured on this
laptop: ~0.8 s per receipt, CPU.

Reconstructing the lines is the part that matters. RapidOCR returns boxes in no useful
order, and a flat list of fragments loses the one relationship the model needs — that a
description and the amount to its right are the same purchase. Clustering by vertical
overlap and sorting each cluster by x turns 41 fragments back into 22 lines.

End to end, against the live model on gx10:

| receipt | result |
|---|---|
| clean grocery receipt, 8 items | every field correct, 42 s |
| restaurant receipt: rotated 1.6°, blurred, sensor noise, uneven lighting, JPEG q76 | every field correct including tip, 30 s |

Both passed the arithmetic check (items + tax + tip = printed total) with no correction.

## When to revisit

`RENDER_MODE=image` is written, tested to the point of building a correct request body,
and one environment variable away. The day `sir` stops typing `content` as a string, flip
it and compare against the OCR path on the same receipts — the extractions table keeps
`render_mode` on every row precisely so that comparison is a query.

The case for switching: OCR is the weakest link here. It cannot read handwriting, it
struggles with faded thermal paper, and it has no idea what a receipt is — the vision
tower does.
