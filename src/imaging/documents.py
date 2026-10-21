"""Search documents: one document per image, ID = image_id (idempotent overwrite target).

The index mapping is strict, so a field that is not listed here can never be written. The same
mapping is installed by the search bootstrap custom resource and used by the in-memory index.
"""

from __future__ import annotations

from typing import Any

from .jobs import Job
from .model_output import ModelOutput

INDEX_NAME = "images-v1"
STATUSES = ("analyzed", "rejected", "failed")

MAPPING: dict[str, Any] = {
    "settings": {"index": {"number_of_shards": 1}},
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "image_id": {"type": "keyword"},
            "status": {"type": "keyword"},
            "category": {"type": "keyword"},
            "tags": {"type": "keyword"},
            "content_type": {"type": "keyword"},
            "size_bytes": {"type": "long"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "uploaded_at": {"type": "date"},
            "processed_at": {"type": "date"},
            "attempts": {"type": "integer"},
            "model": {"type": "keyword"},
            "top1_class": {"type": "integer"},
            "top1_score": {"type": "float"},
            "top_classes": {"type": "integer"},
            "top_scores": {"type": "float", "index": False},
            "reject_reason": {"type": "keyword"},
            "failure_reason": {"type": "keyword"},
            "end_to_end_ms": {"type": "long"},
        },
    },
}
FIELDS = frozenset(MAPPING["mappings"]["properties"])


def _base(job: Job, status: str, processed_at: str, end_to_end_ms: int | None) -> dict[str, Any]:
    doc = {
        "image_id": job.image_id, "status": status, "category": job.category, "tags": list(job.tags),
        "content_type": job.content_type, "size_bytes": job.size_bytes, "width": job.width,
        "height": job.height, "processed_at": processed_at, "attempts": job.attempt,
    }
    if job.uploaded_at:
        doc["uploaded_at"] = job.uploaded_at
    if end_to_end_ms is not None:
        doc["end_to_end_ms"] = max(0, int(end_to_end_ms))
    return doc


def analyzed_doc(job: Job, out: ModelOutput, processed_at: str, end_to_end_ms: int | None) -> dict[str, Any]:
    doc = _base(job, "analyzed", processed_at, end_to_end_ms)
    doc.update(model=out.model, top1_class=out.top1.class_id, top1_score=out.top1.score,
               top_classes=[p.class_id for p in out.top_k], top_scores=[p.score for p in out.top_k])
    return _checked(doc)


def rejected_doc(job: Job, reason: str, processed_at: str) -> dict[str, Any]:
    doc = _base(job, "rejected", processed_at, None)
    doc["reject_reason"] = reason
    return _checked(doc)


def failed_doc(job: Job, reason_code: str, processed_at: str, end_to_end_ms: int | None) -> dict[str, Any]:
    doc = _base(job, "failed", processed_at, end_to_end_ms)
    doc["failure_reason"] = reason_code
    return _checked(doc)


def _checked(doc: dict[str, Any]) -> dict[str, Any]:
    unknown = set(doc) - FIELDS
    if unknown:
        raise ValueError(f"fields not in the mapping: {sorted(unknown)}")
    return doc
