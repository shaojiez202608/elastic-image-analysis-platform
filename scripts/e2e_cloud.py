"""End-to-end checks against the deployed sandbox: D1, D3, D4 plus deny/abuse paths.

    uv run python scripts/e2e_cloud.py [--config config/sandbox.json] [--timeout 1500]

Every check prints PASS/FAIL; a JSON record (account IDs redacted) goes to out/e2e-<UTC>.json.
Exit code 1 if any check fails.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from cloudlib import Cloud, redact  # noqa: E402

from imaging.synthetic import bad_file, good_image  # noqa: E402

RESULTS: list[dict] = []


def check(name: str, ok: bool, detail="") -> bool:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": redact(detail)})
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {redact(detail)}")
    return ok


def wait_status(cl: Cloud, ids: list[str], final=("DONE", "REJECTED", "FAILED"), timeout=1500) -> dict:
    t0, last = time.time(), {}
    while time.time() - t0 < timeout:
        last = {i: cl.call("GET", f"/images/{i}")[1] for i in ids}
        if all(v.get("status") in final for v in last.values()):
            break
        time.sleep(15)
    return last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/sandbox.json")
    ap.add_argument("--timeout", type=int, default=1500)
    a = ap.parse_args()
    cl = Cloud(a.config)
    started = datetime.now(UTC)
    run = f"{started:%m%d%H%M%S}"  # per-run tags, so earlier runs never count in the search checks
    tag, tag_bad, tag_fault = f"e2e-{run}", f"e2e-bad-{run}", f"e2e-fault-{run}"
    print(f"[e2e] region {cl.region}, prefix {cl.p}")

    print("[e2e] deny and validation paths")
    code, _ = cl.call("GET", "/search", sign=False)
    check("unsigned API request is refused", code == 403, f"status {code}")
    code, out = cl.call("POST", "/uploads", {"content_type": "image/gif", "size_bytes": 10})
    check("upload of a disallowed type is refused", code == 400, f"status {code} {out.get('message', out.get('keyword', ''))}")
    code, out = cl.call("GET", "/search", params={"status": "deleted"})
    check("search with an invalid enum is refused", code == 400, f"status {code}")

    img = good_image(1)
    code, out = cl.call("POST", "/uploads", {"content_type": "image/jpeg", "size_bytes": len(img)})
    st = cl.post_form(out["upload"], img + b"\0" * 2048, "image/jpeg")
    check("S3 refuses a body larger than the declared size (content-length-range)", st in (400, 403), f"status {st}")
    forged = copy.deepcopy(out["upload"])
    forged["fields"]["key"] = forged["fields"]["key"].replace("/original.jpg", "/../other.jpg")
    st = cl.post_form(forged, img, "image/jpeg")
    check("S3 refuses a presigned POST whose key was changed", st == 403, f"status {st}")
    forged = copy.deepcopy(out["upload"])
    forged["fields"]["Content-Type"] = "text/html"
    st = cl.post_form(forged, img, "text/html")
    check("S3 refuses a presigned POST whose Content-Type was changed", st == 403, f"status {st}")

    print("[e2e] D1 basic flow + D3 failure cases (asynchronous path)")
    good = []
    for i in range(6):
        iid, st = cl.upload(good_image(10 + i), "image/jpeg", category="batch", tags=[tag])
        good.append(iid)
    bad = {}
    for case in ("truncated_jpeg", "text_as_jpeg", "bomb_png", "png_declared_jpeg"):
        data, ctype, reason = bad_file(case)
        iid, st = cl.upload(data, ctype, category="general", tags=[tag_bad])
        bad[iid] = reason
    t_id, _ = cl.upload(good_image(50), "image/jpeg", fault="timeout", tags=[tag_fault])
    e_id, _ = cl.upload(good_image(51), "image/jpeg", fault="error_once", tags=[tag_fault])
    print(f"  uploaded {len(good)} good, {len(bad)} bad, 2 fault-injected; waiting (endpoint may start from zero)")
    states = wait_status(cl, good + list(bad) + [e_id], timeout=a.timeout)
    check("all good images analyzed", all(states[i].get("status") == "DONE" for i in good),
          str({k[:6]: v.get("status") for k, v in states.items() if k in good}))
    d0 = states[good[0]].get("document") or {}
    check("document has model results and metadata", d0.get("status") == "analyzed" and d0.get("category") == "batch"
          and isinstance(d0.get("top1_class"), int) and 0 <= d0.get("top1_score", -1) <= 1, json.dumps(d0)[:200])
    check("bad files rejected with the expected reason",
          all(states[i].get("status") == "REJECTED" and states[i].get("reason") == r for i, r in bad.items()),
          str({r: states[i].get("reason") for i, r in bad.items()}))
    es = states[e_id]
    check("transient inference error recovers on retry", es.get("status") == "DONE" and es.get("attempt") == 2,
          f"{es.get('status')} attempt {es.get('attempt')}")
    ts = wait_status(cl, [t_id], timeout=a.timeout)[t_id]
    check("inference timeout retried then FAILED", ts.get("status") == "FAILED" and ts.get("reason") == "timeout",
          f"{ts.get('status')} attempt {ts.get('attempt')} reason {ts.get('reason')}")
    sqs = cl.c("sqs")
    dead = []
    for _ in range(3):
        for m in sqs.receive_message(QueueUrl=cl.queues["ingest-dlq"], MaxNumberOfMessages=10, WaitTimeSeconds=2,
                                     VisibilityTimeout=5).get("Messages", []):
            dead.append(json.loads(m["Body"]))
    check("failed job is in the ingest DLQ for redrive", any(d.get("image_id") == t_id for d in dead),
          json.dumps([d for d in dead if d.get("image_id") == t_id])[:200])

    print("[e2e] D3 idempotency: replay the S3 event and the completion notification")
    before = cl.call("GET", f"/images/{good[0]}")[1]["document"]
    bucket = f"{cl.p}-images-" + cl.c("sts").get_caller_identity()["Account"] + f"-{cl.region}"
    sqs.send_message(QueueUrl=cl.queues["ingest"], MessageBody=json.dumps({"Records": [{
        "eventName": "ObjectCreated:Post", "eventTime": before["uploaded_at"],
        "s3": {"bucket": {"name": bucket}, "object": {"key": f"uploads/{good[0]}/original.jpg", "size": 1}}}]}))
    sqs.send_message(QueueUrl=cl.queues["results"], MessageBody=json.dumps({
        "Type": "Notification", "Message": json.dumps({
            "eventSource": "aws:sagemaker", "eventName": "InferenceResult", "invocationStatus": "Completed",
            "inferenceId": f"{good[0]}-a1",
            "responseParameters": {"outputLocation": f"s3://{bucket}/async-output/{good[0]}-a1.out"}})}))
    time.sleep(90)
    after = cl.call("GET", f"/images/{good[0]}")[1]["document"]
    check("replayed deliveries do not rewrite the document", after.get("processed_at") == before.get("processed_at"),
          f"processed_at {before.get('processed_at')} -> {after.get('processed_at')}")

    print("[e2e] D4 search and paging")
    seen, cursor, total = [], None, None
    while True:
        params = {"tag": tag, "size": "4", **({"cursor": cursor} if cursor else {})}
        code, page = cl.call("GET", "/search", params=params)
        if code != 200:
            break
        total = page["total"]
        seen += [d["image_id"] for d in page["items"]]
        cursor = page.get("next_cursor")
        if not cursor:
            break
    check("tag filter with cursor paging returns each image once",
          set(good) <= set(seen) and len(seen) == len(set(seen)) and total == len(seen), f"total {total}, seen {len(seen)}")
    code, page = cl.call("GET", "/search", params={"status": "rejected", "tag": tag_bad})
    check("status filter finds rejected files", code == 200 and page["total"] == len(bad), f"total {page.get('total')}")
    code, page = cl.call("GET", "/search", params={"top1_class": str(d0.get("top1_class", 0)), "tag": tag})
    check("result-field filter (top1_class) finds the image", good[0] in [d["image_id"] for d in page.get("items", [])])

    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    rec = {"started": started.isoformat(), "region": cl.region, "prefix": cl.p, "results": RESULTS}
    path = out / f"e2e-{started:%Y%m%dT%H%M%SZ}.json"
    path.write_text(redact(json.dumps(rec, indent=2)))
    failed = [r for r in RESULTS if not r["ok"]]
    print(f"[e2e] {len(RESULTS) - len(failed)}/{len(RESULTS)} passed; record {path.relative_to(ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
