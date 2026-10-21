"""Message formats on the two queues.

Ingest queue (consumed by the dispatcher):
  - S3 event notifications (`Records[].s3`), sent by the images bucket for `uploads/` objects;
  - `s3:TestEvent` (sent once when the notification is configured; acknowledged and ignored);
  - retry messages written by the indexer: {"type": "retry", "image_id": ..., "attempt": n}.
Results queue (consumed by the indexer):
  - SageMaker AI asynchronous inference notifications delivered through SNS (envelope with
    `Type: Notification` and the JSON event in `Message`, unless raw delivery is enabled);
  - rejected-file messages from the dispatcher: {"type": "rejected", "image_id": ..., "reason": ...}.

Anything else is `Unparseable`: it is acknowledged, counted and never retried, so a malformed
message cannot block the queue.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import unquote_plus

RE_IMAGE_ID = re.compile(r"^[0-9a-f]{32}$")
RE_UPLOAD_KEY = re.compile(r"^uploads/(?P<id>[0-9a-f]{32})/original\.(?P<ext>jpg|png)$")
RE_INFERENCE_ID = re.compile(r"^(?P<id>[0-9a-f]{32})-a(?P<attempt>[1-9][0-9]?)$")
MAX_MESSAGE_BYTES = 256 * 1024


@dataclass(frozen=True)
class UploadEvent:
    bucket: str
    key: str
    image_id: str
    size: int
    event_time: str


@dataclass(frozen=True)
class RetryRequest:
    image_id: str
    attempt: int


@dataclass(frozen=True)
class InferenceNotification:
    image_id: str
    attempt: int
    completed: bool
    output_location: str
    failure_reason: str  # raw service text: classified, never logged or indexed
    event_time: str


@dataclass(frozen=True)
class RejectedNotice:
    image_id: str
    reason: str


@dataclass(frozen=True)
class Ignored:
    why: str  # test_event | foreign_key


@dataclass(frozen=True)
class Unparseable:
    why: str


def inference_id(image_id: str, attempt: int) -> str:
    return f"{image_id}-a{attempt}"


def _load(body: str):
    if len(body.encode("utf-8", "replace")) > MAX_MESSAGE_BYTES:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def parse_ingest(body: str) -> list:
    doc = _load(body)
    if not isinstance(doc, dict):
        return [Unparseable("not_json_object")]
    if doc.get("Event") == "s3:TestEvent":
        return [Ignored("test_event")]
    if doc.get("type") == "retry":
        iid, attempt = doc.get("image_id"), doc.get("attempt")
        if isinstance(iid, str) and RE_IMAGE_ID.match(iid) and isinstance(attempt, int) and 1 < attempt < 100:
            return [RetryRequest(iid, attempt)]
        return [Unparseable("bad_retry")]
    records = doc.get("Records")
    if not isinstance(records, list) or not records:
        return [Unparseable("no_records")]
    out = []
    for r in records:
        try:
            if not str(r["eventName"]).startswith("ObjectCreated:"):
                out.append(Ignored("not_object_created"))
                continue
            key = unquote_plus(r["s3"]["object"]["key"])  # S3 event keys are URL-encoded
            m = RE_UPLOAD_KEY.match(key)
            if not m:
                out.append(Ignored("foreign_key"))
                continue
            out.append(UploadEvent(r["s3"]["bucket"]["name"], key, m.group("id"),
                                   int(r["s3"]["object"].get("size", 0)), str(r.get("eventTime", ""))))
        except (KeyError, TypeError, ValueError):
            out.append(Unparseable("bad_record"))
    return out


def parse_result(body: str):
    doc = _load(body)
    if not isinstance(doc, dict):
        return Unparseable("not_json_object")
    if doc.get("Type") == "Notification" and isinstance(doc.get("Message"), str):
        doc = _load(doc["Message"])
        if not isinstance(doc, dict):
            return Unparseable("bad_sns_message")
    if doc.get("type") == "rejected":
        iid, reason = doc.get("image_id"), doc.get("reason")
        if isinstance(iid, str) and RE_IMAGE_ID.match(iid) and isinstance(reason, str) and len(reason) <= 64:
            return RejectedNotice(iid, reason)
        return Unparseable("bad_rejected")
    if doc.get("eventSource") != "aws:sagemaker" or doc.get("eventName") != "InferenceResult":
        return Unparseable("unknown_event")
    m = RE_INFERENCE_ID.match(str(doc.get("inferenceId", "")))
    if not m:
        return Unparseable("foreign_inference_id")
    status = doc.get("invocationStatus")
    resp = doc.get("responseParameters") or {}
    return InferenceNotification(
        image_id=m.group("id"), attempt=int(m.group("attempt")), completed=status == "Completed",
        output_location=str(resp.get("outputLocation", "")) if isinstance(resp, dict) else "",
        failure_reason=str(doc.get("failureReason", ""))[:2048], event_time=str(doc.get("eventTime", "")))


FAILURE_CODES = (
    (re.compile(r"timeout|timed out|time exceeded|InvocationTimeout", re.I), "timeout"),
    (re.compile(r"expired|ttl", re.I), "request_expired"),
    (re.compile(r"\b5\d\d\b|server error|ModelError|internal", re.I), "model_error"),
    (re.compile(r"\b4\d\d\b|client error|validation", re.I), "model_client_error"),
)


def classify_failure(reason: str) -> str:
    for rx, code in FAILURE_CODES:
        if rx.search(reason or ""):
            return code
    return "other"
