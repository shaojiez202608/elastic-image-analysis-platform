"""AWS adapters: SQS, S3, SageMaker AI Runtime (InvokeEndpointAsync) and OpenSearch (SigV4 over HTTPS).

Throttling and availability errors are mapped to TransientError (the message is retried with
backoff); other client errors propagate and are retried by the queue until the DLQ.
"""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from typing import Any

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.exceptions import ClientError

from ..documents import INDEX_NAME
from ..ports import AsyncInvocation, IndexOutcome, Message, TooLarge, TransientError
from ..search_query import SearchRequest, page_response, to_dsl

log = logging.getLogger("imaging")
TRANSIENT_CODES = {"ThrottlingException", "Throttling", "ServiceUnavailable", "InternalFailure",
                   "InternalServerError", "ProvisionedThroughputExceededException", "RequestLimitExceeded",
                   "SlowDown", "ServiceUnavailableException", "ModelNotReadyException"}
RETRY_CONFIG = Config(retries={"mode": "standard", "max_attempts": 3}, connect_timeout=5, read_timeout=20)


def _transient(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in TRANSIENT_CODES


class SqsQueue:
    def __init__(self, url: str, client=None):
        self.url = url
        self.c = client or boto3.client("sqs", config=RETRY_CONFIG)

    def receive(self, max_messages: int, wait_seconds: int) -> list[Message]:
        resp = self.c.receive_message(QueueUrl=self.url, MaxNumberOfMessages=min(10, max_messages),
                                      WaitTimeSeconds=wait_seconds,
                                      MessageSystemAttributeNames=["ApproximateReceiveCount", "SentTimestamp"])
        out = []
        for m in resp.get("Messages", []):
            a = m.get("Attributes", {})
            out.append(Message(m["Body"], m["ReceiptHandle"], int(a.get("ApproximateReceiveCount", 1)),
                               int(a.get("SentTimestamp", 0)), m.get("MessageId", "")))
        return out

    def delete(self, msg: Message) -> None:
        self.c.delete_message(QueueUrl=self.url, ReceiptHandle=msg.receipt)

    def change_visibility(self, msg: Message, seconds: int) -> None:
        self.c.change_message_visibility(QueueUrl=self.url, ReceiptHandle=msg.receipt,
                                         VisibilityTimeout=max(0, min(43200, seconds)))

    def send(self, body: str, delay_seconds: int = 0) -> None:
        self.c.send_message(QueueUrl=self.url, MessageBody=body, DelaySeconds=max(0, min(900, delay_seconds)))


class S3Store:
    def __init__(self, client=None):
        self.c = client or boto3.client("s3", config=RETRY_CONFIG)

    def head_size(self, bucket: str, key: str) -> int | None:
        try:
            return int(self.c.head_object(Bucket=bucket, Key=key)["ContentLength"])
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def get_bounded(self, bucket: str, key: str, max_bytes: int) -> bytes:
        # Range request caps what is read even if the object grew after head_object.
        body = self.c.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{max_bytes}")["Body"].read(max_bytes + 1)
        if len(body) > max_bytes:
            raise TooLarge(key)
        return body

    def put(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
        self.c.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)

    def get_uri_bounded(self, uri: str, max_bytes: int) -> bytes:
        if not uri.startswith("s3://"):
            raise ValueError("location")
        bucket, _, key = uri[5:].partition("/")
        return self.get_bounded(bucket, key, max_bytes)


class SageMakerAsync:
    def __init__(self, endpoint_name: str, client=None, allowed_bucket: str = ""):
        self.endpoint = endpoint_name
        self.allowed_bucket = allowed_bucket
        self.c = client or boto3.client("sagemaker-runtime", config=RETRY_CONFIG)

    def invoke_async(self, *, inference_id: str, input_uri: str, content_type: str,
                     invocation_timeout_seconds: int) -> AsyncInvocation:
        if self.allowed_bucket and not input_uri.startswith(f"s3://{self.allowed_bucket}/staging/"):
            raise ValueError("input outside the staging prefix")
        try:
            r = self.c.invoke_endpoint_async(EndpointName=self.endpoint, InputLocation=input_uri,
                                             ContentType=content_type, Accept="application/json",
                                             InferenceId=inference_id,
                                             InvocationTimeoutSeconds=invocation_timeout_seconds)
        except ClientError as exc:
            if _transient(exc):
                raise TransientError(exc.response["Error"]["Code"]) from None
            raise
        return AsyncInvocation(r.get("InferenceId", inference_id), r["OutputLocation"])


class OpenSearchIndex:
    """Minimal OpenSearch client: SigV4-signed HTTPS (service `es`), TLS verified, no extra dependency."""

    def __init__(self, endpoint: str, region: str, *, session=None, index: str = INDEX_NAME, timeout: int = 10,
                 transport=None):
        self.base = "https://" + endpoint.removeprefix("https://").rstrip("/")
        self.region, self.index, self.timeout = region, index, timeout
        self.session = session or boto3.session.Session()
        self.transport = transport or self._urllib
        self.ctx = ssl.create_default_context()

    def request(self, method: str, path: str, body: Any = None, params: str = "") -> tuple[int, dict]:
        url = self.base + path + (f"?{params}" if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = AWSRequest(method=method, url=url, data=data, headers={"Content-Type": "application/json"})
        SigV4Auth(self.session.get_credentials().get_frozen_credentials(), "es", self.region).add_auth(req)
        status, raw = self.transport(method, url, dict(req.headers.items()), data)
        if status >= 500 or status == 429:
            raise TransientError(f"search {status}")
        try:
            return status, json.loads(raw or b"{}")
        except ValueError:
            return status, {}

    def _urllib(self, method, url, headers, data):
        r = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310 - https only
        try:
            with urllib.request.urlopen(r, timeout=self.timeout, context=self.ctx) as resp:  # noqa: S310
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, TimeoutError) as e:
            raise TransientError("search unreachable") from e

    @staticmethod
    def _denied_action(resp: dict) -> str:
        """For security_exception: the permission name from "no permissions for [<ACTION>]" (a fixed keyword)."""
        import re

        err = resp.get("error")
        reason = err.get("reason", "") if isinstance(err, dict) else ""
        m = re.search(r"no permissions for \[([a-z_:/*\[\]]{1,80})\]", reason if isinstance(reason, str) else "")
        return m.group(1) if m else ""

    @staticmethod
    def _error_type(resp: dict) -> str:
        err = resp.get("error")
        etype = err.get("type") if isinstance(err, dict) else None
        # Only the service's error type keyword is logged (e.g. security_exception), never a message or body.
        return etype if isinstance(etype, str) and etype.replace("_", "").isalnum() and len(etype) < 64 else "unknown"

    def put(self, doc_id: str, doc: dict[str, Any], version: int) -> IndexOutcome:
        status, resp = self.request("PUT", f"/{self.index}/_doc/{doc_id}", doc,
                                    params=f"version={int(version)}&version_type=external")
        if status == 409:
            return IndexOutcome("duplicate")
        if status not in (200, 201):
            log.warning(json.dumps({"event": "search_write_failed", "status": status,
                                    "error_type": self._error_type(resp), "denied": self._denied_action(resp)}))
            raise RuntimeError(f"index write failed with {status}")
        return IndexOutcome(resp.get("result", "created"))

    def get(self, doc_id: str) -> dict[str, Any] | None:
        status, resp = self.request("GET", f"/{self.index}/_doc/{doc_id}")
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(f"get failed with {status}")
        return resp.get("_source")

    def search(self, req: SearchRequest) -> dict[str, Any]:
        status, resp = self.request("POST", f"/{self.index}/_search", to_dsl(req))
        if status != 200:
            log.warning(json.dumps({"event": "search_query_failed", "status": status,
                                    "error_type": self._error_type(resp)}))
            raise RuntimeError(f"search failed with {status}")
        hits = resp.get("hits", {})
        total = hits.get("total", {}).get("value", 0) if isinstance(hits.get("total"), dict) else hits.get("total", 0)
        return page_response(hits.get("hits", []), total, req.size)
