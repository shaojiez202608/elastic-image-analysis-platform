"""Query/upload API (API Gateway REST, IAM auth, Lambda proxy integration).

Routes:
  POST /uploads              -> creates the job record and returns a presigned POST (FR-1)
  GET  /images/{image_id}    -> job status (DynamoDB) and the search document if present
  GET  /search?<filters>     -> metadata/result search with cursor pagination (FR-4)

The caller identity is the IAM principal verified by API Gateway (requestContext.identity);
nothing in the request body is treated as identity. Validation errors return 400 with the field
name and a keyword only. If the search domain is unavailable the API answers 200 with
`degraded: true` (and the job status where it has one) instead of a 5xx, and records a metric.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import boto3
from botocore.config import Config

from imaging.adapters.aws import OpenSearchIndex
from imaging.jobs import DynamoJobStore
from imaging.messages import RE_IMAGE_ID
from imaging.search_query import BadRequest, parse_request
from imaging.settings import Settings
from imaging.timeutil import iso
from imaging.uploads import create_upload, parse_upload, presigned_post

log = logging.getLogger()
log.setLevel(logging.INFO)
MAX_BODY = 4096
_clients: dict[str, Any] = {}


def _settings() -> Settings:
    return Settings.from_env()


def _jobs(s: Settings):
    if "jobs" not in _clients:
        _clients["jobs"] = DynamoJobStore(boto3.resource("dynamodb").Table(s.jobs_table))
    return _clients["jobs"]


def _search(s: Settings):
    if "search" not in _clients:
        _clients["search"] = OpenSearchIndex(s.search_endpoint, s.region)
    return _clients["search"]


def _s3():
    if "s3" not in _clients:
        # SigV4 presigning; the regional endpoint keeps the POST URL in the bucket's region.
        _clients["s3"] = boto3.client("s3", config=Config(signature_version="s3v4"),
                                      endpoint_url=f"https://s3.{os.environ.get('AWS_REGION', 'us-east-1')}.amazonaws.com")
    return _clients["s3"]


def _resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"Content-Type": "application/json",
                                               "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            "body": json.dumps(body)}


def _metric(name: str) -> None:
    ns = os.environ.get("METRICS_NAMESPACE", "proto-imaging")
    print(json.dumps({"_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [
        {"Namespace": ns, "Dimensions": [["Service"]], "Metrics": [{"Name": name, "Unit": "Count"}]}]},
        "Service": "api", name: 1}))


def handler(event: dict[str, Any], context: Any = None, *, deps: dict[str, Any] | None = None) -> dict[str, Any]:
    s = (deps or {}).get("settings") or _settings()
    jobs = (deps or {}).get("jobs") or None
    search = (deps or {}).get("search") or None
    s3 = (deps or {}).get("s3") or None
    resource, method = event.get("resource", ""), event.get("httpMethod", "")
    try:
        if resource == "/uploads" and method == "POST":
            raw = event.get("body") or ""
            if event.get("isBase64Encoded") or len(raw) > MAX_BODY:
                raise BadRequest("$", "body")
            try:
                body = json.loads(raw)
            except ValueError:
                raise BadRequest("$", "not_json") from None
            req = parse_upload(body, categories=s.categories, max_bytes=s.max_upload_bytes,
                               fault_injection=s.fault_injection)
            job = create_upload(req, jobs or _jobs(s), now_iso=iso(time.time()))
            expires = int(os.environ.get("PRESIGN_EXPIRES_SECONDS", "300"))
            post = presigned_post(s3 or _s3(), bucket=s.images_bucket, job=job, max_bytes=req.size_bytes,
                                  expires=expires)
            log.info(json.dumps({"event": "upload_created", "image_id": job.image_id}))
            return _resp(201, {"image_id": job.image_id, "upload": post, "expires_in": expires})
        if resource == "/images/{image_id}" and method == "GET":
            iid = (event.get("pathParameters") or {}).get("image_id", "")
            if not RE_IMAGE_ID.match(iid):
                raise BadRequest("image_id", "pattern")
            job = (jobs or _jobs(s)).get(iid)
            if job is None:
                return _resp(404, {"error": "not_found"})
            out: dict[str, Any] = {"image_id": iid, "status": job.status, "attempt": job.attempt,
                                   "reason": job.reason or None}
            try:
                out["document"] = (search or _search(s)).get(iid)
            except Exception as exc:  # noqa: BLE001 - degrade, never 5xx
                log.warning(json.dumps({"event": "search_unavailable", "error": type(exc).__name__}))
                _metric("ApiSearchDegraded")
                out.update(document=None, degraded=True)
            return _resp(200, out)
        if resource == "/search" and method == "GET":
            req = parse_request(event.get("queryStringParameters") or {}, categories=s.categories,
                                num_classes=s.num_classes)
            try:
                return _resp(200, (search or _search(s)).search(req))
            except Exception as exc:  # noqa: BLE001 - degrade, never 5xx
                log.warning(json.dumps({"event": "search_unavailable", "error": type(exc).__name__}))
                _metric("ApiSearchDegraded")
                return _resp(200, {"total": 0, "items": [], "next_cursor": None, "degraded": True})
        return _resp(404, {"error": "not_found"})
    except BadRequest as exc:
        return _resp(400, {"error": "bad_request", "field": exc.field, "keyword": exc.keyword})
    except Exception as exc:  # noqa: BLE001 - e.g. DynamoDB unavailable: answer without a 5xx
        log.warning(json.dumps({"event": "api_dependency_error", "error": type(exc).__name__}))
        _metric("ApiDependencyErrors")
        return _resp(200, {"error": "temporarily_unavailable", "degraded": True})
