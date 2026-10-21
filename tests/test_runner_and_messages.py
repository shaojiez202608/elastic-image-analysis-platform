import json

from imaging.adapters.local import LocalQueue, ManualClock
from imaging.messages import classify_failure, parse_ingest, parse_result
from imaging.ports import Metrics
from imaging.runner import Runner, backoff_seconds, emf_line


def test_backoff_caps():
    assert [backoff_seconds(n, 10) for n in (1, 2, 3)] == [10, 20, 40]
    assert backoff_seconds(30, 10) == 900


def test_runner_retries_failures_then_queue_redrives_to_dlq():
    clock = ManualClock()
    dlq = LocalQueue("dlq", clock)
    q = LocalQueue("q", clock, visibility_timeout=30, max_receive=3, dlq=dlq)
    q.send("poison")
    out = []

    def handler(msg):
        raise RuntimeError("secret detail that must not be logged")

    r = Runner(q, handler, service="t", namespace="ns", metrics=Metrics(), wait_seconds=0, emit=out.append)
    for _ in range(10):
        r.run_once()
        clock.advance(1000)
    assert q.depth() == 0 and dlq.depth() == 1
    doc = json.loads(out[0])
    assert doc["MessageErrors"] == 1 and doc["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "ns"


def test_runner_logs_class_name_only(caplog):
    clock = ManualClock()
    q = LocalQueue("q", clock)
    q.send("x")
    r = Runner(q, lambda m: (_ for _ in ()).throw(ValueError("customer text")), service="t", namespace="ns",
               metrics=Metrics(), wait_seconds=0, emit=lambda _: None)
    with caplog.at_level("WARNING"):
        r.run_once()
    assert "ValueError" in caplog.text and "customer text" not in caplog.text


def test_emf_empty():
    assert emf_line("ns", "svc", Metrics(), 0) is None


def test_s3_event_keys_are_url_decoded_and_foreign_keys_ignored():
    body = json.dumps({"Records": [
        {"eventName": "ObjectCreated:Post", "eventTime": "t", "s3": {"bucket": {"name": "b"},
         "object": {"key": "uploads/" + "a" * 32 + "/original.jpg", "size": 3}}},
        {"eventName": "ObjectCreated:Put", "s3": {"bucket": {"name": "b"}, "object": {"key": "uploads/x%2F..%2Fy.jpg"}}},
        {"eventName": "ObjectRemoved:Delete", "s3": {"bucket": {"name": "b"}, "object": {"key": "k"}}},
        {"eventName": "ObjectCreated:Put"},
    ]})
    kinds = [type(x).__name__ for x in parse_ingest(body)]
    assert kinds == ["UploadEvent", "Ignored", "Ignored", "Unparseable"]


def test_retry_message_validation():
    assert type(parse_ingest(json.dumps({"type": "retry", "image_id": "a" * 32, "attempt": 2}))[0]).__name__ == "RetryRequest"
    assert type(parse_ingest(json.dumps({"type": "retry", "image_id": "a" * 32, "attempt": 1}))[0]).__name__ == "Unparseable"
    assert type(parse_ingest("x" * (300 * 1024))[0]).__name__ == "Unparseable"


def test_notification_parsing_raw_and_enveloped():
    ev = {"eventSource": "aws:sagemaker", "eventName": "InferenceResult", "invocationStatus": "Failed",
          "inferenceId": "b" * 32 + "-a2", "failureReason": "ModelInvocationTimeExceeded"}
    for body in (json.dumps(ev), json.dumps({"Type": "Notification", "Message": json.dumps(ev)})):
        n = parse_result(body)
        assert (n.image_id, n.attempt, n.completed) == ("b" * 32, 2, False)
    assert classify_failure("ModelInvocationTimeExceeded") == "timeout"
    assert classify_failure("Received server error (500) from model") == "model_error"
    assert classify_failure("Request expired (TTL)") == "request_expired"
    assert classify_failure("something new") == "other"
