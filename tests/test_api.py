"""API handler: upload presign, status, search, validation errors, degraded paths (never 5xx)."""

import json

import boto3
import pytest
from moto import mock_aws

from api.handler import handler
from imaging import jobs as J
from imaging.adapters.local import MemorySearch
from imaging.jobs import Job, MemoryJobStore
from imaging.settings import Settings

S = Settings(images_bucket="bkt-images", fault_injection=False, max_upload_bytes=1000)


class Broken:
    def get(self, *_):
        raise ConnectionError("down")

    def search(self, *_):
        raise ConnectionError("down")


@pytest.fixture
def deps():
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        yield {"settings": S, "jobs": MemoryJobStore(), "search": MemorySearch(), "s3": s3}


def call(deps, resource, method, body=None, params=None, path=None):
    ev = {"resource": resource, "httpMethod": method, "body": json.dumps(body) if body is not None else None,
          "queryStringParameters": params, "pathParameters": path,
          "requestContext": {"identity": {"userArn": "arn:aws:iam::111122223333:role/caller"}}}
    r = handler(ev, deps=deps)
    return r["statusCode"], json.loads(r["body"])


def test_upload_returns_pinned_presigned_post(deps):
    code, body = call(deps, "/uploads", "POST", {"content_type": "image/png", "size_bytes": 500, "category": "batch",
                                                  "tags": ["b", "a"]})
    assert code == 201
    fields = body["upload"]["fields"]
    assert fields["key"] == f"uploads/{body['image_id']}/original.png" and fields["Content-Type"] == "image/png"
    policy = json.loads(__import__("base64").b64decode(fields["policy"]))
    assert ["content-length-range", 1, 500] in policy["conditions"]
    job = deps["jobs"].get(body["image_id"])
    assert job.status == J.PENDING and job.tags == ("a", "b") and job.category == "batch"


@pytest.mark.parametrize("body,field,kw", [
    ({"content_type": "image/gif", "size_bytes": 5}, "content_type", "enum"),
    ({"content_type": "image/png", "size_bytes": 5000}, "size_bytes", "range"),
    ({"content_type": "image/png", "size_bytes": 0}, "size_bytes", "range"),
    ({"content_type": "image/png", "size_bytes": True}, "size_bytes", "type"),
    ({"content_type": "image/png", "size_bytes": 5, "category": "nope"}, "category", "enum"),
    ({"content_type": "image/png", "size_bytes": 5, "tags": ["UPPER"]}, "tags", "pattern"),
    ({"content_type": "image/png", "size_bytes": 5, "tags": ["a"] * 6}, "tags", "max_items"),
    ({"content_type": "image/png", "size_bytes": 5, "filename": "../etc/passwd"}, "filename", "pattern"),
    ({"content_type": "image/png", "size_bytes": 5, "fault": "timeout"}, "fault", "disabled"),
    ({"content_type": "image/png", "size_bytes": 5, "owner": "someone"}, "owner", "unknown_field"),
])
def test_upload_validation(deps, body, field, kw):
    code, out = call(deps, "/uploads", "POST", body)
    assert code == 400 and (out["field"], out["keyword"]) == (field, kw)


def test_upload_rejects_non_json_and_large_body(deps):
    ev = {"resource": "/uploads", "httpMethod": "POST", "body": "x" * 5000}
    assert handler(ev, deps=deps)["statusCode"] == 400
    ev["body"] = "{bad"
    assert json.loads(handler(ev, deps=deps)["body"])["keyword"] == "not_json"


def test_status_and_search(deps):
    iid = "c" * 32
    deps["jobs"].create(Job(iid, J.DONE, uploaded_at="2026-01-01T00:00:00Z"))
    deps["search"].put(iid, {"image_id": iid, "status": "analyzed", "uploaded_at": "2026-01-01T00:00:00Z",
                             "top1_score": 0.5, "top_classes": [1]}, 1)
    code, out = call(deps, "/images/{image_id}", "GET", path={"image_id": iid})
    assert code == 200 and out["status"] == J.DONE and out["document"]["status"] == "analyzed"
    assert call(deps, "/images/{image_id}", "GET", path={"image_id": "d" * 32})[0] == 404
    assert call(deps, "/images/{image_id}", "GET", path={"image_id": "../x"})[0] == 400
    code, out = call(deps, "/search", "GET", params={"status": "analyzed", "min_score": "0.4"})
    assert code == 200 and out["total"] == 1
    code, out = call(deps, "/search", "GET", params={"q": "*"})
    assert code == 400 and out["keyword"] == "unknown_parameter"


def test_search_outage_degrades_without_5xx(deps):
    deps["search"] = Broken()
    iid = "e" * 32
    deps["jobs"].create(Job(iid, J.INVOKED))
    code, out = call(deps, "/search", "GET", params={})
    assert code == 200 and out["degraded"] is True and out["items"] == []
    code, out = call(deps, "/images/{image_id}", "GET", path={"image_id": iid})
    assert code == 200 and out["degraded"] is True and out["status"] == J.INVOKED


def test_dependency_error_degrades(deps):
    class BadJobs:
        def create(self, *_):
            raise ConnectionError("ddb down")

    deps["jobs"] = BadJobs()
    code, out = call(deps, "/uploads", "POST", {"content_type": "image/png", "size_bytes": 5})
    assert code == 200 and out["degraded"] is True and "ddb" not in json.dumps(out)


def test_unknown_route(deps):
    assert call(deps, "/admin", "DELETE")[0] == 404
