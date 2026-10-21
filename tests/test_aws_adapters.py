"""AWS adapters against moto (SQS, S3, DynamoDB) and botocore Stubber (SageMaker AI Runtime),
plus the OpenSearch client with a fake transport (SigV4 headers, versioning, error mapping)."""

import json

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from imaging import jobs as J
from imaging.adapters.aws import OpenSearchIndex, S3Store, SageMakerAsync, SqsQueue
from imaging.jobs import DynamoJobStore, Job
from imaging.ports import TooLarge, TransientError
from imaging.search_query import SearchRequest


@pytest.fixture
def aws():
    with mock_aws():
        yield


def test_sqs_roundtrip_with_attributes(aws):
    url = boto3.client("sqs").create_queue(QueueName="q")["QueueUrl"]
    q = SqsQueue(url)
    q.send("hello", delay_seconds=0)
    [m] = q.receive(10, 0)
    assert m.body == "hello" and m.receive_count == 1 and m.sent_at_ms > 0
    q.change_visibility(m, 0)
    [m2] = q.receive(10, 0)
    assert m2.receive_count == 2
    q.delete(m2)
    assert q.receive(10, 0) == []


def test_s3_bounded_reads(aws):
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket="bkt-test")
    st = S3Store()
    st.put("bkt-test", "k", b"x" * 100, "image/jpeg")
    assert st.head_size("bkt-test", "k") == 100 and st.head_size("bkt-test", "missing") is None
    assert st.get_bounded("bkt-test", "k", 100) == b"x" * 100
    with pytest.raises(TooLarge):
        st.get_bounded("bkt-test", "k", 99)
    assert st.get_uri_bounded("s3://bkt-test/k", 1000) == b"x" * 100
    with pytest.raises(ValueError):
        st.get_uri_bounded("https://evil.example.com/k", 10)


def test_dynamo_conditional_transitions(aws):
    ddb = boto3.resource("dynamodb")
    t = ddb.create_table(TableName="jobs", KeySchema=[{"AttributeName": "image_id", "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": "image_id", "AttributeType": "S"}],
                         BillingMode="PAY_PER_REQUEST")
    store = DynamoJobStore(t)
    iid = "a" * 32
    assert store.create(Job(iid, J.PENDING, tags=("x",), uploaded_at="2026-01-01T00:00:00Z"))
    assert not store.create(Job(iid, J.PENDING))
    assert store.transition(iid, expect={J.PENDING, J.DISPATCHING}, to=J.DISPATCHING, expect_attempt=0,
                            lease_expired_before=100.0, lease_until=200.0)
    # lease still valid -> a second claimer is refused
    assert not store.transition(iid, expect={J.PENDING, J.DISPATCHING}, to=J.DISPATCHING, expect_attempt=0,
                                lease_expired_before=150.0, lease_until=300.0)
    # lease expired -> claim allowed
    assert store.transition(iid, expect={J.PENDING, J.DISPATCHING}, to=J.DISPATCHING, expect_attempt=0,
                            lease_expired_before=250.0, lease_until=400.0)
    assert store.transition(iid, expect={J.DISPATCHING}, to=J.INVOKED, attempt=1, width=10)
    assert not store.transition(iid, expect={J.INVOKED}, expect_attempt=2, to=J.DONE)
    assert store.transition(iid, expect={J.INVOKED}, expect_attempt=1, to=J.DONE)
    job = store.get(iid)
    assert (job.status, job.attempt, job.width, job.tags) == (J.DONE, 1, 10, ("x",))
    assert not store.transition("b" * 32, expect={J.PENDING}, to=J.DONE)


def test_sagemaker_async_invocation_and_error_mapping():
    client = boto3.client("sagemaker-runtime", region_name="us-east-1")
    sm = SageMakerAsync("ep", client=client, allowed_bucket="bkt")
    with Stubber(client) as stub:
        stub.add_response("invoke_endpoint_async", {"InferenceId": "x-a1", "OutputLocation": "s3://bkt/async-output/x.out"},
                          {"EndpointName": "ep", "InputLocation": "s3://bkt/staging/x.jpg", "ContentType": "image/jpeg",
                           "Accept": "application/json", "InferenceId": "x-a1", "InvocationTimeoutSeconds": 30})
        r = sm.invoke_async(inference_id="x-a1", input_uri="s3://bkt/staging/x.jpg", content_type="image/jpeg",
                            invocation_timeout_seconds=30)
        assert r.output_location.endswith("x.out")
        stub.add_client_error("invoke_endpoint_async", service_error_code="ThrottlingException", http_status_code=400)
        with pytest.raises(TransientError):
            sm.invoke_async(inference_id="x-a2", input_uri="s3://bkt/staging/x.jpg", content_type="image/jpeg",
                            invocation_timeout_seconds=30)
    with pytest.raises(ValueError):
        sm.invoke_async(inference_id="x", input_uri="s3://other/staging/x.jpg", content_type="image/jpeg",
                        invocation_timeout_seconds=30)


class FakeTransport:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, url, headers, data):
        self.calls.append((method, url, headers, data))
        status, body = self.responses.pop(0)
        return status, json.dumps(body).encode()


def test_opensearch_versioned_put_and_errors():
    t = FakeTransport([(201, {"result": "created"}), (409, {"error": "version_conflict"}), (503, {}), (400, {})])
    os_ = OpenSearchIndex("vpc-x.us-east-1.es.amazonaws.com", "us-east-1", transport=t,
                          session=boto3.session.Session(aws_access_key_id="testing", aws_secret_access_key="testing"))
    assert os_.put("a" * 32, {"image_id": "a" * 32}, 2).result == "created"
    method, url, headers, _ = t.calls[0]
    assert method == "PUT" and url.endswith("version=2&version_type=external") and url.startswith("https://")
    assert "Authorization" in headers and "AWS4-HMAC-SHA256" in headers["Authorization"]
    assert os_.put("a" * 32, {"image_id": "a" * 32}, 2).result == "duplicate"
    with pytest.raises(TransientError):
        os_.put("a" * 32, {}, 3)
    with pytest.raises(RuntimeError):
        os_.put("a" * 32, {}, 3)


def test_opensearch_search_pages():
    hits = {"hits": {"total": {"value": 3}, "hits": [{"_source": {"image_id": "a" * 32}, "sort": [5, "a" * 32]}]}}
    t = FakeTransport([(200, hits)])
    os_ = OpenSearchIndex("https://vpc-x.es.amazonaws.com", "us-east-1", transport=t,
                          session=boto3.session.Session(aws_access_key_id="testing", aws_secret_access_key="testing"))
    page = os_.search(SearchRequest(size=1, status="analyzed"))
    assert page["total"] == 3 and page["next_cursor"]
    body = json.loads(t.calls[0][3])
    assert body["query"]["bool"]["filter"] == [{"term": {"status": "analyzed"}}]
