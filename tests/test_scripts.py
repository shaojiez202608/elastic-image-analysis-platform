"""Ops scripts and the evaluation method: destructive-change guard, simulation (incl. a negative control),
report rendering, inference-image resolution, model packaging checks, optional real-model check."""

import io
import json
import os
import sys
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_destructive as cd  # noqa: E402
import package_model  # noqa: E402
import resolve_inference_image as rii  # noqa: E402

from imaging.report import comparison_markdown, timeline_svg  # noqa: E402
from imaging.sim import Layer, Phase, SimConfig, fixed, simulate  # noqa: E402

DIFF = """Stack proto-imaging-inference
Resources
[~] AWS::SageMaker::EndpointConfig EndpointConfig EndpointConfig replace
Stack proto-imaging-storage
[-] AWS::S3::Bucket Images Images2D38C313 destroy
"""


def test_destructive_guard(monkeypatch):
    f = cd.parse(DIFF)
    assert {x.action for x in f} == {"replace", "destroy"}
    rc, _ = cd.decide(f, False, {})
    assert rc == 3
    assert cd.decide(f, False, {"ALLOW_DESTRUCTIVE": "1"})[0] == 0
    assert cd.decide(f, True, {"ALLOW_DESTRUCTIVE": "1"})[0] == 3  # retained profile: data store needs a 2nd flag
    assert cd.decide(f, True, {"ALLOW_DESTRUCTIVE": "1", "ALLOW_DATA_REPLACEMENT": "1"})[0] == 0
    assert cd.decide(cd.parse("Stack x\n[+] AWS::SQS::Queue Q Q\n"), True, {})[0] == 0


CFG = SimConfig(workers=Layer(0, 3, 45, 60, 120), endpoint=Layer(0, 2, 300, 60, 120), dispatch_seconds=0.5,
                inference_seconds=2.0, concurrency_per_instance=2, worker_steps=((1, 1), (40, 2)),
                target_backlog_per_instance=4)
LOAD = [Phase("burst", 600, count=60), Phase("idle", 900)]


def test_elastic_vs_fixed_relative_relations():
    e = simulate(LOAD, CFG).summary()
    f = simulate(LOAD, fixed(CFG, 2, 2), tail_seconds=1800).summary()
    assert e["items"] == f["items"] == 60
    assert e["instance_seconds"] < f["instance_seconds"]  # elastic provisions less over the window
    assert e["inference_utilisation"] > f["inference_utilisation"]
    assert e["queue_wait_p95_s"] > f["queue_wait_p95_s"]  # and pays the start-up delay in waiting time
    assert e["scaling_events"] > 0 and f["scaling_events"] == 0


def test_scale_from_zero_negative_control():
    from dataclasses import replace
    small = [Phase("burst", 60, count=2), Phase("idle", 600)]
    woke = simulate(small, CFG, tail_seconds=600)
    stuck = simulate(small, replace(CFG, scale_from_zero=False), tail_seconds=600)
    assert woke.summary()["items"] == 2
    assert stuck.summary()["items"] == 0  # backlog below the target never wakes the endpoint without the step policy


def test_timeline_svg_is_valid_xml_with_tooltips():
    svg = timeline_svg([{"t": 0, "a": 1}, {"t": 15, "a": 3}], [("a", "Queue <depth>")],
                       [{"t": 10, "label": "endpoint 0->1"}])
    root = ET.fromstring(svg)  # nosec B314 - parses the SVG this test just generated, not external input
    titles = [e.text for e in root.iter("{http://www.w3.org/2000/svg}title")]
    assert any("endpoint 0-&gt;1" in (t or "") or "endpoint 0->1" in (t or "") for t in titles)
    assert "Queue &lt;depth&gt;" in svg


def test_comparison_is_relative_only():
    md = comparison_markdown({"x": 10}, {"x": 5}, [("x", "reading")])
    assert "0.50x" in md and "| x |" in md


def test_resolve_inference_image():
    table = {"inference_graviton": {"versions": {"2.6.0": {"py_versions": ["py312"], "repository": "pytorch-inference-arm64",
                                                         "container_version": {"cpu": "ubuntu22.04"},
                                                         "registries": {"us-west-2": "111122223333"}}}},
             "inference": {"versions": {}}}
    uri = rii.resolve(table, region="us-west-2", architecture="arm64", accelerator="cpu", version="2.6.0", py="py312")
    assert uri == "111122223333.dkr.ecr.us-west-2.amazonaws.com/pytorch-inference-arm64:2.6.0-cpu-py312-ubuntu22.04-sagemaker"
    with pytest.raises(SystemExit):
        rii.resolve(table, region="eu-west-1", architecture="arm64", accelerator="cpu", version="2.6.0", py="py312")


def test_model_package_refuses_wrong_checksum(tmp_path):
    with pytest.raises(SystemExit):
        package_model.verify(b"not the weights")
    digest = package_model.build_tar(b"weights", tmp_path / "m.tar.gz")
    names = tarfile.open(tmp_path / "m.tar.gz").getnames()
    assert names == ["weights.bin", "code/inference.py"] and len(digest) == 64
    assert package_model.code_hash() == package_model.code_hash()


def test_inference_handler_contract_without_torch(monkeypatch):
    sys.path.insert(0, str(ROOT / "model" / "code"))
    import inference
    body, ctype = inference.output_fn([{"class_id": 1, "score": 0.5}])
    assert ctype == "application/json" and json.loads(body)["model"] == "resnet18.a1_in1k"
    assert inference._params("image/jpeg; fault=timeout") == ("image/jpeg", {"fault": "timeout"})


@pytest.mark.model
@pytest.mark.skipif(not os.environ.get("MODEL_WEIGHTS"), reason="set MODEL_WEIGHTS=<PATH_TO_DOWNLOADED_WEIGHTS>")
def test_real_model_output_passes_the_indexer_validator(tmp_path):
    sys.path.insert(0, str(ROOT / "model" / "code"))
    import inference
    from imaging.model_output import parse_model_output
    from imaging.synthetic import good_image
    from imaging.validation import Limits, validate_image
    (tmp_path / "weights.bin").write_bytes(Path(os.environ["MODEL_WEIGHTS"]).read_bytes())
    m = inference.model_fn(str(tmp_path))
    v = validate_image(good_image(3, (640, 480)), "image/jpeg", Limits(10**7, 10**8, 32, 8192, 512))
    body, _ = inference.output_fn(inference.predict_fn(inference.input_fn(v.normalized, "image/jpeg"), m))
    assert len(parse_model_output(body.encode(), num_classes=1000, max_k=5).top_k) == 5
    with pytest.raises(ValueError):
        inference.input_fn(io.BytesIO(b"x").getvalue(), "text/html")


def test_compare_runs_extends_the_shorter_run_to_a_common_window(tmp_path):
    import compare_runs

    d = tmp_path / "fixed"
    d.mkdir()
    (d / "timeline.csv").write_text("t,endpoint_current,dispatcher_running,indexer_running\n0,2,2,1\n15,2,2,1\n")
    s = {"observed_window_s": 30, "items_analyzed": 10}
    compare_runs.extend_to_window(d, s, 90)
    # 2 samples x 15 s x 2 instances = 1 minute observed, plus 2 instances for the missing 60 s = 3 minutes
    assert s["endpoint_instance_minutes_common_window"] == 3.0
    assert s["items_per_endpoint_instance_minute_common_window"] == round(10 / 3, 3)
