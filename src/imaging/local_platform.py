"""Local mode: the whole pipeline in one process, using the same Dispatcher/Indexer/Runner code as
the ECS services, with local stand-ins for S3, SQS, SNS, DynamoDB, SageMaker AI and OpenSearch.

Time is a manual clock, so retries with delays, visibility timeouts and DLQ redrive run instantly
and deterministically.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from .adapters.local import FakeInference, LocalObjectStore, LocalQueue, ManualClock, MemorySearch
from .jobs import MemoryJobStore
from .pipeline import Dispatcher, Indexer, iso
from .ports import Metrics
from .runner import Runner
from .search_query import parse_request
from .settings import Settings
from .uploads import create_upload, object_key, parse_upload

BUCKET = "local-images"


class LocalPlatform:
    def __init__(self, settings: Settings | None = None, *, max_receive: int = 4, bad_output: str | None = None):
        s = settings or Settings()
        self.s = replace(s, images_bucket=BUCKET, fault_injection=True if settings is None else s.fault_injection)
        self.clock = ManualClock()
        vt = self.s.visibility_timeout_seconds
        self.ingest_dlq = LocalQueue("ingest-dlq", self.clock)
        self.results_dlq = LocalQueue("results-dlq", self.clock)
        self.ingest = LocalQueue("ingest", self.clock, visibility_timeout=vt, max_receive=max_receive, dlq=self.ingest_dlq)
        self.results = LocalQueue("results", self.clock, visibility_timeout=vt, max_receive=max_receive,
                                  dlq=self.results_dlq)
        self.store = LocalObjectStore()
        self.store.on_put = self._s3_event
        self.jobs = MemoryJobStore()
        self.search_index = MemorySearch()
        self.inference = FakeInference(self.store, self.results, self.clock, output_bucket=BUCKET,
                                       k=self.s.top_k, num_classes=self.s.num_classes, bad_output=bad_output)
        self.metrics = Metrics()
        self.dispatcher = Dispatcher(self.s, store=self.store, jobs=self.jobs, inference=self.inference,
                                     results=self.results, metrics=self.metrics, clock=self.clock)
        self.indexer = Indexer(self.s, store=self.store, jobs=self.jobs, index=self.search_index, ingest=self.ingest,
                               dlq=self.ingest_dlq, metrics=self.metrics, clock=self.clock)
        self.emitted: list[str] = []
        self.dispatch_runner = Runner(self.ingest, self.dispatcher.handle, service="dispatcher",
                                      namespace=self.s.metrics_namespace, metrics=self.metrics, wait_seconds=0,
                                      emit=self.emitted.append)
        self.index_runner = Runner(self.results, self.indexer.handle, service="indexer",
                                   namespace=self.s.metrics_namespace, metrics=self.metrics, wait_seconds=0,
                                   emit=self.emitted.append)
        self.counters: dict[str, float] = {}

    # S3 -> SQS event notification for uploads/ (same shape as the real event, key URL-encoded)
    def _s3_event(self, bucket: str, key: str, size: int) -> None:
        if not key.startswith("uploads/"):
            return
        self.ingest.send(json.dumps({"Records": [{
            "eventVersion": "2.1", "eventSource": "aws:s3", "eventName": "ObjectCreated:Post",
            "eventTime": iso(self.clock()),
            "s3": {"bucket": {"name": bucket}, "object": {"key": key, "size": size}}}]}))

    def upload(self, data: bytes, content_type: str, *, category: str = "general", tags: list[str] | None = None,
               fault: str = "", declared_size: int | None = None) -> str:
        body: dict[str, Any] = {"content_type": content_type, "size_bytes": declared_size or max(1, len(data)),
                                "category": category, "tags": tags or []}
        if fault:
            body["fault"] = fault
        req = parse_upload(body, categories=self.s.categories, max_bytes=self.s.max_upload_bytes,
                           fault_injection=self.s.fault_injection)
        job = create_upload(req, self.jobs, now_iso=iso(self.clock()))
        # The presigned POST would enforce content-length-range; the local store accepts anything so
        # the worker-side checks can be exercised too.
        self.store.put(BUCKET, object_key(job.image_id, content_type), data, content_type)
        return job.image_id

    def step(self, seconds: float = 1.0, inference_capacity: int | None = None) -> None:
        self.dispatch_runner.run_once()
        self.inference.pump(inference_capacity)
        self.index_runner.run_once()
        self._collect()
        self.clock.advance(seconds)

    def _collect(self) -> None:
        for line in self.emitted:
            doc = json.loads(line)
            for m in doc["_aws"]["CloudWatchMetrics"][0]["Metrics"]:
                if m["Unit"] == "Count":
                    self.counters[m["Name"]] = self.counters.get(m["Name"], 0) + doc[m["Name"]]
        self.emitted.clear()

    def idle(self) -> bool:
        return self.ingest.depth() == 0 and self.results.depth() == 0 and not self.inference.backlog

    def run_until_idle(self, max_steps: int = 2000, seconds: float = 5.0) -> int:
        for i in range(max_steps):
            if self.idle():
                return i
            self.step(seconds)
        raise RuntimeError("pipeline did not become idle")

    def search(self, params: dict[str, str] | None = None) -> dict[str, Any]:
        req = parse_request(params, categories=self.s.categories, num_classes=self.s.num_classes)
        return self.search_index.search(req)
