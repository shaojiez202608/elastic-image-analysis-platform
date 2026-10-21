"""Worker/API settings from environment variables (set by the CDK stacks from config/*.json)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .validation import Limits


def _int(env: dict, name: str, default: int) -> int:
    return int(env.get(name, default))


@dataclass(frozen=True)
class Settings:
    images_bucket: str = ""
    ingest_queue_url: str = ""
    results_queue_url: str = ""
    ingest_dlq_url: str = ""
    jobs_table: str = ""
    endpoint_name: str = ""
    search_endpoint: str = ""
    region: str = ""
    max_upload_bytes: int = 10 * 1024 * 1024
    max_pixels: int = 40_000_000
    min_side: int = 32
    max_side: int = 8192
    normalize_max_side: int = 512
    invocation_timeout_seconds: int = 120
    max_inference_attempts: int = 3
    retry_base_delay_seconds: int = 30
    visibility_timeout_seconds: int = 120
    receive_batch: int = 10
    num_classes: int = 1000
    top_k: int = 5
    fault_injection: bool = False
    metrics_namespace: str = "proto-imaging"
    categories: tuple[str, ...] = ("general", "batch", "interactive", "archive")

    @property
    def limits(self) -> Limits:
        return Limits(self.max_upload_bytes, self.max_pixels, self.min_side, self.max_side, self.normalize_max_side)

    @classmethod
    def from_env(cls, env: dict | None = None) -> Settings:
        e = dict(os.environ if env is None else env)
        d = cls()
        return cls(
            images_bucket=e.get("IMAGES_BUCKET", ""), ingest_queue_url=e.get("INGEST_QUEUE_URL", ""),
            results_queue_url=e.get("RESULTS_QUEUE_URL", ""), ingest_dlq_url=e.get("INGEST_DLQ_URL", ""),
            jobs_table=e.get("JOBS_TABLE", ""), endpoint_name=e.get("ENDPOINT_NAME", ""),
            search_endpoint=e.get("SEARCH_ENDPOINT", ""),
            region=e.get("AWS_REGION", e.get("AWS_DEFAULT_REGION", "")),
            max_upload_bytes=_int(e, "MAX_UPLOAD_BYTES", d.max_upload_bytes),
            max_pixels=_int(e, "MAX_PIXELS", d.max_pixels), min_side=_int(e, "MIN_SIDE", d.min_side),
            max_side=_int(e, "MAX_SIDE", d.max_side),
            normalize_max_side=_int(e, "NORMALIZE_MAX_SIDE", d.normalize_max_side),
            invocation_timeout_seconds=_int(e, "INVOCATION_TIMEOUT_SECONDS", d.invocation_timeout_seconds),
            max_inference_attempts=_int(e, "MAX_INFERENCE_ATTEMPTS", d.max_inference_attempts),
            retry_base_delay_seconds=_int(e, "RETRY_BASE_DELAY_SECONDS", d.retry_base_delay_seconds),
            visibility_timeout_seconds=_int(e, "VISIBILITY_TIMEOUT_SECONDS", d.visibility_timeout_seconds),
            receive_batch=_int(e, "RECEIVE_BATCH", d.receive_batch),
            num_classes=_int(e, "NUM_CLASSES", d.num_classes), top_k=_int(e, "TOP_K", d.top_k),
            fault_injection=e.get("FAULT_INJECTION", "false").lower() == "true",
            metrics_namespace=e.get("METRICS_NAMESPACE", d.metrics_namespace),
            categories=tuple(c for c in e.get("CATEGORIES", ",".join(d.categories)).split(",") if c),
        )
