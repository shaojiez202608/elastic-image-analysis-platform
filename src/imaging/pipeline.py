"""Dispatcher (ingest queue -> validation -> asynchronous inference) and indexer (results -> search).

Delivery is at-least-once everywhere (S3 events, SQS, SNS). Each step therefore does its side
effect first and the conditional state transition second; a crash in between leads to a
redelivery whose side effect is idempotent downstream (same inference ID, same document ID and
version), and whose transition is then applied once.

Return values: `ACK` deletes the message. Exceptions leave it on the queue; the runner sets a
backoff visibility timeout, and the queue's redrive policy moves it to the DLQ after
maxReceiveCount deliveries. Permanent problems (bad file, malformed message) are ACKed and
recorded, so they never block the queue.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

from . import jobs as J
from .documents import analyzed_doc, failed_doc, rejected_doc
from .jobs import Job, JobStore
from .messages import (
    Ignored,
    InferenceNotification,
    RejectedNotice,
    RetryRequest,
    Unparseable,
    UploadEvent,
    classify_failure,
    inference_id,
    parse_ingest,
    parse_result,
)
from .model_output import MAX_OUTPUT_BYTES, OutputInvalid, parse_model_output
from .ports import Inference, Message, Metrics, ObjectStore, Queue, SearchIndex, TooLarge, TransientError
from .settings import Settings
from .timeutil import iso, parse_iso
from .validation import ImageRejected, validate_image

log = logging.getLogger("imaging")
ACK = "ack"
FAULTS = ("timeout", "error", "error_once")


def staging_key(image_id: str) -> str:
    return f"staging/{image_id}.jpg"


class NotYetScheduled(TransientError):
    """A retry request arrived before the indexer recorded RETRY_SCHEDULED; try again later."""


class Dispatcher:
    def __init__(self, s: Settings, *, store: ObjectStore, jobs: JobStore, inference: Inference,
                 results: Queue, metrics: Metrics, clock: Callable[[], float] = time.time):
        self.s, self.store, self.jobs, self.inference, self.results = s, store, jobs, inference, results
        self.m, self.now = metrics, clock

    def handle(self, msg: Message) -> str:
        if msg.sent_at_ms:
            self.m.timing("IngestQueueWaitMs", self.now() * 1000 - msg.sent_at_ms)
        for item in parse_ingest(msg.body):
            if isinstance(item, UploadEvent):
                self._upload(item)
            elif isinstance(item, RetryRequest):
                self._retry(item)
            elif isinstance(item, Ignored):
                self.m.incr("IgnoredMessages")
            elif isinstance(item, Unparseable):
                self.m.incr("UnparseableMessages")
                log.warning(json.dumps({"event": "unparseable_ingest", "why": item.why}))
        return ACK

    # --- uploads -----------------------------------------------------------------------------
    def _upload(self, ev: UploadEvent) -> None:
        job = self.jobs.get(ev.image_id)
        if job is None:  # object written without the upload API (e.g. bulk copy): default metadata
            ctype = "image/png" if ev.key.endswith(".png") else "image/jpeg"
            self.jobs.create(Job(ev.image_id, J.PENDING, content_type=ctype,
                                 uploaded_at=ev.event_time or iso(self.now()), source="s3-event"))
            job = self.jobs.get(ev.image_id)
        if job is None or not self._claim(job, expect={J.PENDING, J.DISPATCHING}, attempt=0):
            self.m.incr("DuplicateDeliveries")
            return
        size = self.store.head_size(ev.bucket, ev.key)
        if size is None:
            return self._reject(job, "empty")
        if size > self.s.max_upload_bytes:
            return self._reject(job, "too_large")
        try:
            data = self.store.get_bounded(ev.bucket, ev.key, self.s.max_upload_bytes)
            img = validate_image(data, job.content_type, self.s.limits)
        except TooLarge:
            return self._reject(job, "too_large")
        except ImageRejected as exc:
            return self._reject(job, exc.reason)
        self.store.put(self.s.images_bucket, staging_key(job.image_id), img.normalized, img.normalized_type)
        self._invoke(job, 1, width=img.width, height=img.height, size_bytes=img.size_bytes)

    def _claim(self, job: Job, *, expect: set[str], attempt: int) -> bool:
        if job.status not in expect:
            return False
        now = self.now()
        return self.jobs.transition(job.image_id, expect=expect, to=J.DISPATCHING, expect_attempt=attempt,
                                    lease_expired_before=now, lease_until=now + self.s.visibility_timeout_seconds)

    def _reject(self, job: Job, reason: str) -> None:
        self.results.send(json.dumps({"type": "rejected", "image_id": job.image_id, "reason": reason}))
        self.jobs.transition(job.image_id, expect={J.DISPATCHING}, to=J.REJECTED, reason=reason, lease_until=0.0)
        self.m.incr("RejectedFiles")
        log.info(json.dumps({"event": "rejected", "image_id": job.image_id, "reason": reason}))

    def _invoke(self, job: Job, attempt: int, **fields) -> None:
        ctype = "image/jpeg"
        fault = job.fault if self.s.fault_injection and job.fault in FAULTS else ""
        if fault and not (fault == "error_once" and attempt > 1):
            ctype += f"; fault={fault.removesuffix('_once')}"
        self.inference.invoke_async(
            inference_id=inference_id(job.image_id, attempt),
            input_uri=f"s3://{self.s.images_bucket}/{staging_key(job.image_id)}",
            content_type=ctype, invocation_timeout_seconds=self.s.invocation_timeout_seconds)
        self.jobs.transition(job.image_id, expect={J.DISPATCHING}, to=J.INVOKED, attempt=attempt,
                             lease_until=0.0, invoked_at=iso(self.now()), **fields)
        self.m.incr("InferenceInvocations")
        if attempt > 1:
            self.m.incr("InferenceRetries")

    # --- retries -----------------------------------------------------------------------------
    def _retry(self, req: RetryRequest) -> None:
        job = self.jobs.get(req.image_id)
        prev = req.attempt - 1
        if job is None or job.attempt != prev:
            self.m.incr("DuplicateDeliveries")
            return
        if job.status == J.INVOKED:
            raise NotYetScheduled("retry arrived before RETRY_SCHEDULED was recorded")
        if not self._claim(job, expect={J.RETRY_SCHEDULED, J.DISPATCHING}, attempt=prev):
            self.m.incr("DuplicateDeliveries")
            return
        self._invoke(job, req.attempt)


class Indexer:
    def __init__(self, s: Settings, *, store: ObjectStore, jobs: JobStore, index: SearchIndex, ingest: Queue,
                 dlq: Queue, metrics: Metrics, clock: Callable[[], float] = time.time):
        self.s, self.store, self.jobs, self.index = s, store, jobs, index
        self.ingest, self.dlq, self.m, self.now = ingest, dlq, metrics, clock

    def handle(self, msg: Message) -> str:
        if msg.sent_at_ms:
            self.m.timing("ResultsQueueWaitMs", self.now() * 1000 - msg.sent_at_ms)
        item = parse_result(msg.body)
        if isinstance(item, Unparseable):
            self.m.incr("UnparseableMessages")
            log.warning(json.dumps({"event": "unparseable_result", "why": item.why}))
        elif isinstance(item, RejectedNotice):
            self._rejected(item)
        elif isinstance(item, InferenceNotification):
            self._notification(item)
        return ACK

    def _put(self, image_id: str, doc: dict, version: int) -> None:
        outcome = self.index.put(image_id, doc, version)
        self.m.incr("IndexDuplicates" if outcome.result == "duplicate" else "IndexWrites")

    def _e2e(self, job: Job) -> int | None:
        t0 = parse_iso(job.uploaded_at)
        return int((self.now() - t0) * 1000) if t0 else None

    def _rejected(self, n: RejectedNotice) -> None:
        job = self.jobs.get(n.image_id)
        if job is None:
            return
        self._put(job.image_id, rejected_doc(job, n.reason, iso(self.now())), 1)

    def _notification(self, n: InferenceNotification) -> None:
        job = self.jobs.get(n.image_id)
        if job is None or job.status != J.INVOKED or job.attempt != n.attempt:
            self.m.incr("DuplicateDeliveries")  # replay, or a late notification for a superseded attempt
            return
        if n.completed:
            try:
                raw = self.store.get_uri_bounded(n.output_location, MAX_OUTPUT_BYTES)
                out = parse_model_output(raw, num_classes=self.s.num_classes, max_k=self.s.top_k)
            except (OutputInvalid, TooLarge, ValueError) as exc:
                path = getattr(exc, "path", "$")
                keyword = getattr(exc, "keyword", "too_large" if isinstance(exc, TooLarge) else "location")
                log.warning(json.dumps({"event": "invalid_model_output", "image_id": job.image_id,
                                        "path": path, "keyword": keyword}))
                self.m.incr("InvalidModelOutputs")
                return self._final_failure(job, "invalid_model_output")
            e2e = self._e2e(job)
            self._put(job.image_id, analyzed_doc(job, out, iso(self.now()), e2e), job.attempt)
            self.jobs.transition(job.image_id, expect={J.INVOKED}, expect_attempt=job.attempt, to=J.DONE)
            self.m.incr("Analyzed")
            if e2e is not None:
                self.m.timing("EndToEndMs", e2e)
            return
        code = classify_failure(n.failure_reason)
        self.m.incr("InferenceFailures")
        log.info(json.dumps({"event": "inference_failed", "image_id": job.image_id, "attempt": job.attempt,
                             "code": code}))
        if job.attempt < self.s.max_inference_attempts:
            delay = min(900, self.s.retry_base_delay_seconds * 2 ** (job.attempt - 1))
            self.ingest.send(json.dumps({"type": "retry", "image_id": job.image_id, "attempt": job.attempt + 1}),
                             delay_seconds=delay)
            self.jobs.transition(job.image_id, expect={J.INVOKED}, expect_attempt=job.attempt,
                                 to=J.RETRY_SCHEDULED, reason=code)
            return
        self._final_failure(job, code)

    def _final_failure(self, job: Job, code: str) -> None:
        self._put(job.image_id, failed_doc(job, code, iso(self.now()), self._e2e(job)), job.attempt)
        self.dlq.send(json.dumps({"type": "failed", "image_id": job.image_id, "attempts": job.attempt,
                                  "reason": code}))
        self.jobs.transition(job.image_id, expect={J.INVOKED}, expect_attempt=job.attempt, to=J.FAILED,
                             reason=code)
        self.m.incr("FailedJobs")
