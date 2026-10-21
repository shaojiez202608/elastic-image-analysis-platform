import pytest

from imaging.search_query import BadRequest, decode_cursor, encode_cursor, parse_request, to_dsl

CATS = ("general", "batch")


def p(params):
    return parse_request(params, categories=CATS, num_classes=1000)


def test_full_query_dsl():
    req = p({"status": "analyzed", "category": "batch", "tag": "x1", "top1_class": "7", "class": "9",
             "min_score": "0.25", "uploaded_after": "2026-01-01T00:00:00Z", "uploaded_before": "2026-02-01T00:00:00+00:00",
             "size": "50"})
    dsl = to_dsl(req)
    flt = dsl["query"]["bool"]["filter"]
    assert {"term": {"top_classes": 9}} in flt and {"range": {"top1_score": {"gte": 0.25}}} in flt
    assert dsl["size"] == 50 and dsl["sort"][1] == {"image_id": {"order": "asc"}}


@pytest.mark.parametrize("params,field,kw", [
    ({"status": "deleted"}, "status", "enum"),
    ({"category": "other"}, "category", "enum"),
    ({"tag": "<script>"}, "tag", "pattern"),
    ({"top1_class": "1000"}, "top1_class", "range"),
    ({"class": "-1"}, "class", "type"),
    ({"min_score": "2"}, "min_score", "range"),
    ({"min_score": "NaN"}, "min_score", "range"),
    ({"uploaded_after": "yesterday"}, "uploaded_after", "format"),
    ({"uploaded_after": "2026-01-01T00:00:00"}, "uploaded_after", "timezone_required"),
    ({"size": "101"}, "size", "range"),
    ({"cursor": "!!!"}, "cursor", "format"),
    ({"script": "x"}, "script", "unknown_parameter"),
])
def test_rejections(params, field, kw):
    with pytest.raises(BadRequest) as e:
        p(params)
    assert (e.value.field, e.value.keyword) == (field, kw)


def test_cursor_roundtrip_and_tamper():
    c = encode_cursor([1700000000000, "a" * 32])
    assert decode_cursor(c) == (1700000000000, "a" * 32)
    import base64
    bad = base64.urlsafe_b64encode(b'[1, "not-an-id"]').decode()
    with pytest.raises(BadRequest):
        decode_cursor(bad)
    with pytest.raises(BadRequest):
        decode_cursor("A" * 300)
    assert to_dsl(p({"cursor": c}))["search_after"] == [1700000000000, "a" * 32]


def test_empty_query_matches_all():
    assert to_dsl(p({}))["query"] == {"match_all": {}}
