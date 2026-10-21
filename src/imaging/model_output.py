"""Validation of the model container's output before it reaches the search index.

The output object in S3 is treated as untrusted: it is read with a byte cap, parsed as JSON, and
only the expected fields survive. Failures carry a field path and a validation keyword only; the
raw output is never logged or copied into user-visible fields.

Expected shape (produced by model/code/inference.py):
    {"model": "<NAME>", "top_k": [{"class_id": <int>, "score": <float 0..1>}, ...]}
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

MAX_OUTPUT_BYTES = 64 * 1024
RE_MODEL = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class OutputInvalid(Exception):  # noqa: N818
    def __init__(self, path: str, keyword: str):
        super().__init__(f"{path}:{keyword}")
        self.path, self.keyword = path, keyword


@dataclass(frozen=True)
class Prediction:
    class_id: int
    score: float


@dataclass(frozen=True)
class ModelOutput:
    model: str
    top_k: tuple[Prediction, ...]

    @property
    def top1(self) -> Prediction:
        return self.top_k[0]


def _reject_constant(name: str):
    raise OutputInvalid("$", "non_finite_number")


def parse_model_output(raw: bytes, *, num_classes: int, max_k: int) -> ModelOutput:
    if len(raw) > MAX_OUTPUT_BYTES:
        raise OutputInvalid("$", "too_large")
    try:
        # parse_constant rejects NaN / Infinity / -Infinity, which json.loads accepts by default.
        doc = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except OutputInvalid:
        raise
    except (UnicodeDecodeError, ValueError):
        raise OutputInvalid("$", "not_json") from None
    if not isinstance(doc, dict):
        raise OutputInvalid("$", "type")
    model = doc.get("model")
    if not isinstance(model, str) or not RE_MODEL.match(model):
        raise OutputInvalid("$.model", "pattern")
    top_k = doc.get("top_k")
    if not isinstance(top_k, list) or not top_k:
        raise OutputInvalid("$.top_k", "type")
    if len(top_k) > max_k:
        raise OutputInvalid("$.top_k", "max_items")
    preds = []
    for i, item in enumerate(top_k):
        path = f"$.top_k[{i}]"
        if not isinstance(item, dict):
            raise OutputInvalid(path, "type")
        cid, score = item.get("class_id"), item.get("score")
        if isinstance(cid, bool) or not isinstance(cid, int):
            raise OutputInvalid(f"{path}.class_id", "type")
        if not 0 <= cid < num_classes:
            raise OutputInvalid(f"{path}.class_id", "range")
        if isinstance(score, bool) or not isinstance(score, int | float):
            raise OutputInvalid(f"{path}.score", "type")
        if not math.isfinite(score):
            raise OutputInvalid(f"{path}.score", "non_finite_number")
        if not 0.0 <= score <= 1.0:
            raise OutputInvalid(f"{path}.score", "range")
        preds.append(Prediction(cid, round(float(score), 6)))
    if any(a.score < b.score for a, b in zip(preds, preds[1:], strict=False)):
        raise OutputInvalid("$.top_k", "order")
    return ModelOutput(model, tuple(preds))
