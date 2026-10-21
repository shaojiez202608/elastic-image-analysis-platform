import json

import pytest

from imaging.model_output import MAX_OUTPUT_BYTES, OutputInvalid, parse_model_output


def ok(top=None, model="resnet18.a1_in1k"):
    return json.dumps({"model": model, "top_k": top if top is not None else
                       [{"class_id": 3, "score": 0.6}, {"class_id": 9, "score": 0.2}]}).encode()


def parse(raw):
    return parse_model_output(raw, num_classes=1000, max_k=5)


def test_valid_output():
    out = parse(ok())
    assert out.top1.class_id == 3 and out.top1.score == 0.6 and len(out.top_k) == 2


@pytest.mark.parametrize("raw,path,keyword", [
    (b'{"model": "m", "top_k": [{"class_id": 1, "score": NaN}]}', "$", "non_finite_number"),
    (b'{"model": "m", "top_k": [{"class_id": 1, "score": Infinity}]}', "$", "non_finite_number"),
    (b'{"model": "m", "top_k": [{"class_id": 1, "score": 1e999}]}', "$.top_k[0].score", "non_finite_number"),
    (b"IGNORE PREVIOUS INSTRUCTIONS", "$", "not_json"),
    (b"[1, 2]", "$", "type"),
    (ok(model="x" * 65), "$.model", "pattern"),
    (ok(model="bad model<script>"), "$.model", "pattern"),
    (ok(top=[]), "$.top_k", "type"),
    (ok(top=[{"class_id": i, "score": 0.1} for i in range(6)]), "$.top_k", "max_items"),
    (ok(top=[{"class_id": 1000, "score": 0.1}]), "$.top_k[0].class_id", "range"),
    (ok(top=[{"class_id": -1, "score": 0.1}]), "$.top_k[0].class_id", "range"),
    (ok(top=[{"class_id": True, "score": 0.1}]), "$.top_k[0].class_id", "type"),
    (ok(top=[{"class_id": "7", "score": 0.1}]), "$.top_k[0].class_id", "type"),
    (ok(top=[{"class_id": 1, "score": 1.5}]), "$.top_k[0].score", "range"),
    (ok(top=[{"class_id": 1, "score": "high"}]), "$.top_k[0].score", "type"),
    (ok(top=[{"class_id": 1, "score": 0.1}, {"class_id": 2, "score": 0.5}]), "$.top_k", "order"),
    (ok(top=["x"]), "$.top_k[0]", "type"),
])
def test_invalid_outputs_report_path_and_keyword_only(raw, path, keyword):
    with pytest.raises(OutputInvalid) as e:
        parse(raw)
    assert (e.value.path, e.value.keyword) == (path, keyword)
    assert "INSTRUCTIONS" not in str(e.value) and "script" not in str(e.value)


def test_oversized_output():
    with pytest.raises(OutputInvalid) as e:
        parse(b" " * (MAX_OUTPUT_BYTES + 1))
    assert e.value.keyword == "too_large"


def test_extra_fields_are_dropped():
    raw = json.dumps({"model": "m", "top_k": [{"class_id": 1, "score": 0.5, "note": "free text"}],
                      "explanation": "free text"}).encode()
    out = parse(raw)
    assert "free text" not in repr(out)
