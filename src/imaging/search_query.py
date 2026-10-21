"""Search API request model: strict parameter validation, OpenSearch DSL, cursor pagination.

Only listed parameters are accepted, every value is validated, and the DSL is built from the
validated object (no user string is ever interpolated into a query string). Pagination uses
`search_after` on (uploaded_at desc, image_id asc), encoded as an opaque, validated cursor.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .documents import STATUSES

RE_TAG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
RE_CATEGORY = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
RE_ID = re.compile(r"^[0-9a-f]{32}$")
ALLOWED = {"status", "category", "tag", "top1_class", "class", "min_score", "uploaded_after",
           "uploaded_before", "size", "cursor"}
MAX_PAGE = 100


class BadRequest(Exception):  # noqa: N818
    def __init__(self, field: str, keyword: str):
        super().__init__(f"{field}:{keyword}")
        self.field, self.keyword = field, keyword


@dataclass(frozen=True)
class SearchRequest:
    status: str | None = None
    category: str | None = None
    tag: str | None = None
    top1_class: int | None = None
    any_class: int | None = None
    min_score: float | None = None
    uploaded_after: str | None = None
    uploaded_before: str | None = None
    size: int = 20
    after: tuple[int, str] | None = None


def _iso(field: str, v: str) -> str:
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise BadRequest(field, "format") from None
    if dt.tzinfo is None:
        raise BadRequest(field, "timezone_required")
    return dt.isoformat()


def _int(field: str, v: str, lo: int, hi: int) -> int:
    if not re.fullmatch(r"\d{1,6}", v or ""):
        raise BadRequest(field, "type")
    n = int(v)
    if not lo <= n <= hi:
        raise BadRequest(field, "range")
    return n


def encode_cursor(sort: list[Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps([int(sort[0]), str(sort[1])]).encode()).decode().rstrip("=")


def decode_cursor(raw: str) -> tuple[int, str]:
    if len(raw) > 200:
        raise BadRequest("cursor", "max_length")
    try:
        val = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, binascii.Error):
        raise BadRequest("cursor", "format") from None
    if (not isinstance(val, list) or len(val) != 2 or isinstance(val[0], bool) or not isinstance(val[0], int)
            or not isinstance(val[1], str) or not RE_ID.match(val[1])):
        raise BadRequest("cursor", "format")
    return val[0], val[1]


def parse_request(params: dict[str, str] | None, *, categories: tuple[str, ...], num_classes: int) -> SearchRequest:
    params = params or {}
    unknown = sorted(set(params) - ALLOWED)
    if unknown:
        raise BadRequest(unknown[0], "unknown_parameter")
    kw: dict[str, Any] = {}
    if "status" in params:
        if params["status"] not in STATUSES:
            raise BadRequest("status", "enum")
        kw["status"] = params["status"]
    if "category" in params:
        if not RE_CATEGORY.match(params["category"]) or params["category"] not in categories:
            raise BadRequest("category", "enum")
        kw["category"] = params["category"]
    if "tag" in params:
        if not RE_TAG.match(params["tag"]):
            raise BadRequest("tag", "pattern")
        kw["tag"] = params["tag"]
    if "top1_class" in params:
        kw["top1_class"] = _int("top1_class", params["top1_class"], 0, num_classes - 1)
    if "class" in params:
        kw["any_class"] = _int("class", params["class"], 0, num_classes - 1)
    if "min_score" in params:
        if not re.fullmatch(r"(0(\.\d{1,6})?|1(\.0{1,6})?)", params["min_score"]):
            raise BadRequest("min_score", "range")
        kw["min_score"] = float(params["min_score"])
    for f in ("uploaded_after", "uploaded_before"):
        if f in params:
            kw[f] = _iso(f, params[f])
    if "size" in params:
        kw["size"] = _int("size", params["size"], 1, MAX_PAGE)
    if "cursor" in params:
        kw["after"] = decode_cursor(params["cursor"])
    return SearchRequest(**kw)


def to_dsl(req: SearchRequest) -> dict[str, Any]:
    flt: list[dict[str, Any]] = []
    for field, value in (("status", req.status), ("category", req.category), ("tags", req.tag),
                         ("top1_class", req.top1_class), ("top_classes", req.any_class)):
        if value is not None:
            flt.append({"term": {field: value}})
    if req.min_score is not None:
        flt.append({"range": {"top1_score": {"gte": req.min_score}}})
    rng = {}
    if req.uploaded_after:
        rng["gte"] = req.uploaded_after
    if req.uploaded_before:
        rng["lt"] = req.uploaded_before
    if rng:
        flt.append({"range": {"uploaded_at": rng}})
    body: dict[str, Any] = {
        "size": req.size, "track_total_hits": True,
        "query": {"bool": {"filter": flt}} if flt else {"match_all": {}},
        "sort": [{"uploaded_at": {"order": "desc", "missing": "_last"}}, {"image_id": {"order": "asc"}}],
    }
    if req.after:
        body["search_after"] = [req.after[0], req.after[1]]
    return body


def page_response(hits: list[dict[str, Any]], total: int, size: int) -> dict[str, Any]:
    """hits: [{"_source": {...}, "sort": [...]}] -> API response with a next cursor."""
    items = [h["_source"] for h in hits]
    nxt = encode_cursor(hits[-1]["sort"]) if len(hits) == size and hits[-1].get("sort") else None
    return {"total": total, "items": items, "next_cursor": nxt}
