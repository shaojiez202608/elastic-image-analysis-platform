"""Client helpers for the deployed sandbox (used by e2e_cloud.py, loadtest.py, set_capacity.py).

Resources are found by their deterministic names (prefix from the profile). API calls are SigV4
signed with the caller's credentials (service `execute-api`); uploads use the presigned POST
returned by the API. Output never contains the account ID (redact()).
"""

from __future__ import annotations

import json
import re
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

ROOT = Path(__file__).resolve().parents[1]
ACCT = re.compile(r"\b\d{12}\b")


def redact(v) -> str:
    return ACCT.sub("111122223333", str(v))


def profile(path: str) -> dict:
    p = ROOT / path
    if not p.exists():
        p = p.with_name(p.stem + ".example.json")
    return json.loads(p.read_text())


class Cloud:
    def __init__(self, config: str = "config/sandbox.json"):
        self.cfg = profile(config)
        self.p, self.region = self.cfg["project"], self.cfg["region"]
        self.s = boto3.session.Session(region_name=self.region)
        self.ctx = ssl.create_default_context()
        apis = [a for a in self.s.client("apigateway").get_rest_apis(limit=500)["items"] if a["name"] == f"{self.p}-api"]
        if not apis:
            raise SystemExit("API not found: is the sandbox deployed?")
        self.api = f"https://{apis[0]['id']}.execute-api.{self.region}.amazonaws.com/v1"
        sqs = self.s.client("sqs")
        self.queues = {n: sqs.get_queue_url(QueueName=f"{self.p}-{n}")["QueueUrl"]
                       for n in ("ingest", "ingest-dlq", "results", "results-dlq")}
        self.endpoint = f"{self.p}-endpoint"
        self.cluster = f"{self.p}-cluster"

    def c(self, svc: str):
        return self.s.client(svc)

    # --- API --------------------------------------------------------------------------------
    def call(self, method: str, path: str, body: dict | None = None, params: dict | None = None,
             sign: bool = True) -> tuple[int, dict]:
        url = self.api + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = AWSRequest(method=method, url=url, data=data, headers={"Content-Type": "application/json"})
        if sign:
            SigV4Auth(self.s.get_credentials().get_frozen_credentials(), "execute-api", self.region).add_auth(req)
        r = urllib.request.Request(url, data=data, method=method, headers=dict(req.headers.items()))  # noqa: S310
        try:
            with urllib.request.urlopen(r, timeout=30, context=self.ctx) as resp:  # noqa: S310
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except ValueError:
                return e.code, {"raw": raw[:200].decode("utf-8", "replace")}

    def post_form(self, upload: dict, data: bytes, content_type: str) -> int:
        """Multipart POST of a presigned upload. Returns the HTTP status (204 on success)."""
        boundary = "----imaging" + secrets.token_hex(8)
        parts = []
        for k, v in upload["fields"].items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="upload"\r\n'
                     f"Content-Type: {content_type}\r\n\r\n".encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        if not upload["url"].startswith("https://"):
            raise ValueError("https only")
        r = urllib.request.Request(upload["url"], data=body, method="POST",  # noqa: S310
                                   headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(r, timeout=60, context=self.ctx) as resp:  # noqa: S310
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    def upload(self, data: bytes, content_type: str, *, declared: int | None = None, **meta) -> tuple[str, int]:
        code, out = self.call("POST", "/uploads", {"content_type": content_type,
                                                   "size_bytes": declared or len(data), **meta})
        if code != 201:
            raise RuntimeError(f"upload request failed: {code} {out}")
        return out["image_id"], self.post_form(out["upload"], data, content_type)

    # --- state ------------------------------------------------------------------------------
    def queue_depths(self) -> dict:
        sqs = self.c("sqs")
        out = {}
        for n, url in self.queues.items():
            a = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=[
                "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
            out[n] = int(a["ApproximateNumberOfMessages"]) + int(a["ApproximateNumberOfMessagesNotVisible"])
        return out

    def services(self) -> dict:
        r = self.c("ecs").describe_services(cluster=self.cluster, services=[f"{self.p}-dispatcher", f"{self.p}-indexer"])
        return {s["serviceName"].removeprefix(self.p + "-"): {"desired": s["desiredCount"], "running": s["runningCount"]}
                for s in r["services"]}

    def endpoint_state(self) -> dict:
        d = self.c("sagemaker").describe_endpoint(EndpointName=self.endpoint)
        v = (d.get("ProductionVariants") or [{}])[0]
        return {"status": d["EndpointStatus"], "current": v.get("CurrentInstanceCount", 0),
                "desired": v.get("DesiredInstanceCount", 0)}

    def scaling_activities(self, since=None) -> list[dict]:
        aas = self.c("application-autoscaling")
        out = []
        for ns, rid in (("ecs", f"service/{self.cluster}/{self.p}-dispatcher"),
                        ("ecs", f"service/{self.cluster}/{self.p}-indexer"),
                        ("sagemaker", f"endpoint/{self.endpoint}/variant/AllTraffic")):
            for page in aas.get_paginator("describe_scaling_activities").paginate(ServiceNamespace=ns, ResourceId=rid):
                for a in page["ScalingActivities"]:
                    if since is None or a["StartTime"] >= since:
                        out.append({"resource": rid.split("/")[-1] if ns == "ecs" else "endpoint",
                                    "start": a["StartTime"].isoformat(), "end": a.get("EndTime").isoformat()
                                    if a.get("EndTime") else "", "status": a["StatusCode"],
                                    "description": redact(a["Description"])})
        return sorted(out, key=lambda x: x["start"])
