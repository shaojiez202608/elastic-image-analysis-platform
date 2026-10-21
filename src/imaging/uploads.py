"""Upload request validation and the presigned POST (FR-1).

The client never chooses the object key: the API generates the image ID and the key
`uploads/<IMAGE_ID>/original.<jpg|png>`. The presigned POST pins the key, the Content-Type and a
content-length range, so S3 itself refuses other types or sizes. User metadata is validated against
fixed patterns and stored in the job record (not in S3 object metadata).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from .jobs import PENDING, Job, JobStore
from .search_query import BadRequest

EXT = {"image/jpeg": "jpg", "image/png": "png"}
RE_TAG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
RE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
ALLOWED_FIELDS = {"content_type", "size_bytes", "category", "tags", "filename", "fault"}
MAX_TAGS = 5


@dataclass(frozen=True)
class UploadRequest:
    content_type: str
    size_bytes: int
    category: str
    tags: tuple[str, ...]
    filename: str
    fault: str


def parse_upload(body: Any, *, categories: tuple[str, ...], max_bytes: int, fault_injection: bool) -> UploadRequest:
    if not isinstance(body, dict):
        raise BadRequest("$", "type")
    unknown = sorted(set(body) - ALLOWED_FIELDS)
    if unknown:
        raise BadRequest(unknown[0], "unknown_field")
    ctype = body.get("content_type")
    if ctype not in EXT:
        raise BadRequest("content_type", "enum")
    size = body.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int):
        raise BadRequest("size_bytes", "type")
    if not 1 <= size <= max_bytes:
        raise BadRequest("size_bytes", "range")
    category = body.get("category", "general")
    if category not in categories:
        raise BadRequest("category", "enum")
    tags = body.get("tags", [])
    if not isinstance(tags, list) or len(tags) > MAX_TAGS:
        raise BadRequest("tags", "max_items")
    if not all(isinstance(t, str) and RE_TAG.match(t) for t in tags):
        raise BadRequest("tags", "pattern")
    filename = body.get("filename", "")
    if filename and (not isinstance(filename, str) or not RE_FILENAME.match(filename)):
        raise BadRequest("filename", "pattern")
    fault = body.get("fault", "")
    if fault:
        if not fault_injection:
            raise BadRequest("fault", "disabled")
        if fault not in ("timeout", "error", "error_once"):
            raise BadRequest("fault", "enum")
    return UploadRequest(ctype, size, category, tuple(sorted(set(tags))), filename, fault)


def object_key(image_id: str, content_type: str) -> str:
    return f"uploads/{image_id}/original.{EXT[content_type]}"


def create_upload(req: UploadRequest, jobs: JobStore, *, now_iso: str, image_id: str | None = None) -> Job:
    image_id = image_id or uuid.uuid4().hex
    job = Job(image_id, PENDING, category=req.category, tags=req.tags, content_type=req.content_type,
              filename=req.filename, uploaded_at=now_iso, fault=req.fault, size_bytes=req.size_bytes)
    if not jobs.create(job):
        raise RuntimeError("image id collision")
    return job


def presigned_post(s3_client, *, bucket: str, job: Job, max_bytes: int, expires: int) -> dict[str, Any]:
    key = object_key(job.image_id, job.content_type)
    return s3_client.generate_presigned_post(
        Bucket=bucket, Key=key, Fields={"Content-Type": job.content_type},
        Conditions=[{"Content-Type": job.content_type}, ["content-length-range", 1, max_bytes]],
        ExpiresIn=expires)
