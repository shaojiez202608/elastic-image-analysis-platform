"""End-to-end in local mode: D1 (basic flow), D3 (failure handling), D4 (search + paging)."""

import json

import pytest

from imaging import jobs as J
from imaging.local_platform import LocalPlatform
from imaging.messages import inference_id
from imaging.settings import Settings
from imaging.synthetic import BAD_CASES, bad_file, good_image


@pytest.fixture
def p():
    return LocalPlatform()


def test_d1_basic_flow(p):
    ids = [p.upload(good_image(i), "image/jpeg", category="batch", tags=["demo"]) for i in range(5)]
    assert p.ingest.depth() == 5
    p.run_until_idle()
    for i in ids:
        doc = p.search_index.get(i)
        assert doc["status"] == "analyzed" and doc["category"] == "batch" and doc["tags"] == ["demo"]
        assert 0 <= doc["top1_class"] < 1000 and 0 <= doc["top1_score"] <= 1
        assert p.jobs.get(i).status == J.DONE
    assert p.search({"status": "analyzed"})["total"] == 5


def test_d3_bad_files_do_not_block_good_files(p):
    bad = {}
    for case in sorted(BAD_CASES):
        data, ctype, reason = bad_file(case)
        bad[p.upload(data, ctype)] = reason
    good = [p.upload(good_image(100 + i, fmt="PNG" if i % 2 else "JPEG"), "image/png" if i % 2 else "image/jpeg")
            for i in range(4)]
    p.run_until_idle()
    for iid, reason in bad.items():
        assert p.jobs.get(iid).status == J.REJECTED
        doc = p.search_index.get(iid)
        assert doc["status"] == "rejected" and doc["reject_reason"] == reason
    assert [p.jobs.get(i).status for i in good] == [J.DONE] * 4
    assert p.ingest_dlq.depth() == 0  # rejected files never go to the DLQ
    assert p.inference.invocations == 4  # bad files never reach the model


def test_d3_timeout_retries_then_dlq(p):
    iid = p.upload(good_image(1), "image/jpeg", fault="timeout")
    ok = p.upload(good_image(2), "image/jpeg")
    p.run_until_idle()
    job = p.jobs.get(iid)
    assert job.status == J.FAILED and job.attempt == p.s.max_inference_attempts and job.reason == "timeout"
    assert p.search_index.get(iid)["status"] == "failed"
    dead = [json.loads(b) for b in p.ingest_dlq.bodies()]
    assert dead == [{"type": "failed", "image_id": iid, "attempts": 3, "reason": "timeout"}]
    assert p.jobs.get(ok).status == J.DONE
    assert p.counters["InferenceRetries"] == 2


def test_d3_transient_error_recovers_on_retry(p):
    iid = p.upload(good_image(3), "image/jpeg", fault="error_once")
    p.run_until_idle()
    job = p.jobs.get(iid)
    assert job.status == J.DONE and job.attempt == 2
    assert p.search_index.get(iid)["attempts"] == 2


def test_d3_duplicate_deliveries_do_not_duplicate_writes(p):
    iid = p.upload(good_image(4), "image/jpeg")
    first_event = p.ingest.bodies()[0]
    p.run_until_idle()
    writes = p.counters.get("IndexWrites")
    # replay the S3 event and the SNS notification
    p.ingest.send(first_event)
    note = {"Type": "Notification", "Message": json.dumps({
        "eventSource": "aws:sagemaker", "eventName": "InferenceResult", "invocationStatus": "Completed",
        "inferenceId": inference_id(iid, 1),
        "responseParameters": {"outputLocation": f"s3://local-images/async-output/{inference_id(iid, 1)}.out"}})}
    p.results.send(json.dumps(note))
    p.run_until_idle()
    assert p.counters.get("IndexWrites") == writes
    assert p.counters["DuplicateDeliveries"] >= 2
    assert p.inference.invocations == 1
    assert len(p.search_index.docs) == 1
    # a direct re-put with the same version is a no-op too (external versioning)
    assert p.search_index.put(iid, {"image_id": iid}, 1).result == "duplicate"


def test_d3_search_outage_retries_then_succeeds(p):
    iid = p.upload(good_image(5), "image/jpeg")
    p.search_index.fail_next = 2
    p.run_until_idle()
    assert p.search_index.get(iid)["status"] == "analyzed"
    assert p.counters["MessageErrors"] == 2
    assert p.results_dlq.depth() == 0


def test_d3_persistent_search_outage_goes_to_results_dlq():
    p = LocalPlatform(max_receive=3)
    p.upload(good_image(6), "image/jpeg")
    p.search_index.fail_next = 10**6
    p.run_until_idle()
    assert p.results_dlq.depth() == 1


def test_unparseable_messages_are_acknowledged(p):
    p.ingest.send("not json")
    p.ingest.send(json.dumps({"Event": "s3:TestEvent"}))
    p.results.send(json.dumps({"Type": "Notification", "Message": "{broken"}))
    p.results.send(json.dumps({"eventSource": "aws:sagemaker", "eventName": "InferenceResult",
                               "inferenceId": "someone-else"}))
    p.run_until_idle()
    assert p.ingest_dlq.depth() == 0 and p.results_dlq.depth() == 0
    assert p.counters["UnparseableMessages"] == 3 and p.counters["IgnoredMessages"] == 1


@pytest.mark.parametrize("mode", ["nan", "range", "text"])
def test_invalid_model_output_is_recorded_not_indexed_raw(mode, caplog):
    p = LocalPlatform(bad_output=mode)
    iid = p.upload(good_image(7), "image/jpeg")
    with caplog.at_level("INFO"):
        p.run_until_idle()
    doc = p.search_index.get(iid)
    assert doc["status"] == "failed" and doc["failure_reason"] == "invalid_model_output"
    assert "INSTRUCTIONS" not in json.dumps(doc) and "INSTRUCTIONS" not in caplog.text


def test_fault_injection_disabled_rejects_fault_field():
    from imaging.search_query import BadRequest
    p = LocalPlatform(Settings(fault_injection=False))
    with pytest.raises(BadRequest):
        p.upload(good_image(8), "image/jpeg", fault="timeout")


def test_d4_search_filters_and_paging(p):
    ids = []
    for i in range(23):
        ids.append(p.upload(good_image(200 + i), "image/jpeg", category="archive" if i % 2 else "batch",
                            tags=["even"] if i % 2 == 0 else ["odd"]))
        p.clock.advance(1)
    p.run_until_idle()
    seen, cursor = [], None
    while True:
        params = {"category": "batch", "size": "5"}
        if cursor:
            params["cursor"] = cursor
        page = p.search(params)
        assert page["total"] == 12
        seen += [d["image_id"] for d in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 12 == len(set(seen))
    times = [p.search_index.get(i)["uploaded_at"] for i in seen]
    assert times == sorted(times, reverse=True)
    doc = p.search_index.get(ids[0])
    hit = p.search({"class": str(doc["top_classes"][-1]), "tag": "even"})
    assert ids[0] in [d["image_id"] for d in hit["items"]]
    assert all(d["top1_score"] >= 0.3 for d in p.search({"min_score": "0.3"})["items"])
