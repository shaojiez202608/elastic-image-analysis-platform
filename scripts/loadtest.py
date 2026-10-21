"""Load test (D2, D5): drive a burst / sustained / idle pattern through the deployed pipeline and record
queue wait, throughput and the scaling timeline of both layers.

    uv run python scripts/loadtest.py --label elastic [--burst 60] [--sustained-per-minute 0 --sustained-seconds 0]
                                      [--idle-seconds 900] [--max-wait 2400] [--config config/sandbox.json]

What is recorded (out/loadtest-<LABEL>-<UTC>/):
  timeline.csv            every 15 s: queue depth (visible + in flight) per queue, ECS desired/running per
                          service, endpoint current/desired instances
  scaling-activities.csv  Application Auto Scaling activities of the three scalable targets in the window
  items.csv               per image: upload time, end-to-end time (upload -> indexed), attempts, status
  summary.json / .md      queue-wait and end-to-end percentiles (from CloudWatch: IngestQueueWaitMs, the
                          endpoint's TimeInBacklog), throughput per minute, instance-minutes, task-minutes,
                          time to first ready capacity, scaling activity count
  timeline.svg            small multiples of the timeline with scaling activities marked
Use the smallest load that shows scaling; it is a method demonstration, not a benchmark.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import math
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from cloudlib import Cloud, redact  # noqa: E402

from imaging.report import timeline_svg, write_csv  # noqa: E402
from imaging.synthetic import good_image  # noqa: E402


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, math.ceil(p / 100 * len(s)) - 1))]


def sample(cl: Cloud, t0: float) -> dict:
    q, sv, ep = cl.queue_depths(), cl.services(), cl.endpoint_state()
    return {"t": round(time.time() - t0), "ingest": q["ingest"], "results": q["results"], "ingest_dlq": q["ingest-dlq"],
            "dispatcher_desired": sv["dispatcher"]["desired"], "dispatcher_running": sv["dispatcher"]["running"],
            "indexer_desired": sv["indexer"]["desired"], "indexer_running": sv["indexer"]["running"],
            "endpoint_current": ep["current"], "endpoint_desired": ep["desired"]}


def cw_stats(cl: Cloud, ns: str, name: str, dims: list[dict], start, end, stats=None, ext=None) -> list[dict]:
    kw = {"ExtendedStatistics": ext} if ext else {"Statistics": stats or ["Average", "Maximum", "Sum"]}
    r = cl.c("cloudwatch").get_metric_statistics(Namespace=ns, MetricName=name, Dimensions=dims, StartTime=start,
                                                 EndTime=end, Period=60, **kw)
    return sorted(r["Datapoints"], key=lambda d: d["Timestamp"])


def main() -> int:  # noqa: C901 - linear script
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/sandbox.json")
    ap.add_argument("--label", default="elastic")
    ap.add_argument("--burst", type=int, default=60)
    ap.add_argument("--sustained-per-minute", type=float, default=0)
    ap.add_argument("--sustained-seconds", type=int, default=0)
    ap.add_argument("--idle-seconds", type=int, default=900, help="observe scale-in after the load")
    ap.add_argument("--max-wait", type=int, default=2400, help="max seconds to wait for all items")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    cl = Cloud(a.config)
    started = datetime.now(UTC)
    run = f"{a.label}-{started:%Y%m%dT%H%M%SZ}"
    out = ROOT / "out" / f"loadtest-{run}"
    out.mkdir(parents=True, exist_ok=True)
    tag = ("lt-" + a.label.lower())[:32]
    t0 = time.time()
    timeline, items = [], {}

    def up(i: int) -> None:
        iid, st = cl.upload(good_image(10_000 + i, size=(640, 480)), "image/jpeg", category="batch", tags=[tag])
        items[iid] = {"image_id": iid, "uploaded_s": round(time.time() - t0, 1), "post_status": st}

    print(f"[loadtest] {run}: burst {a.burst}, sustained {a.sustained_per_minute}/min for {a.sustained_seconds}s, "
          f"then observe {a.idle_seconds}s idle")
    timeline.append(sample(cl, t0))
    with cf.ThreadPoolExecutor(a.workers) as pool:
        list(pool.map(up, range(a.burst)))
    print(f"  burst uploaded in {time.time() - t0:.0f}s")
    n = a.burst
    next_sample = time.time() + 15
    if a.sustained_seconds and a.sustained_per_minute:
        gap, end = 60.0 / a.sustained_per_minute, time.time() + a.sustained_seconds
        nxt = time.time()
        while time.time() < end:
            if time.time() >= nxt:
                up(n)
                n += 1
                nxt += gap
            if time.time() >= next_sample:
                timeline.append(sample(cl, t0))
                next_sample += 15
            time.sleep(0.2)
    total = len(items)
    load_end = time.time()
    done_at = None
    while time.time() - load_end < a.max_wait:
        if time.time() >= next_sample:
            s = sample(cl, t0)
            timeline.append(s)
            next_sample += 15
            if done_at is None and s["ingest"] == 0 and s["results"] == 0:
                code, page = cl.call("GET", "/search", params={"tag": tag, "size": "1"})
                if code == 200 and page["total"] >= total:
                    done_at = time.time()
                    print(f"  all {total} indexed at t={done_at - t0:.0f}s; observing scale-in")
            if done_at and time.time() - done_at >= a.idle_seconds:
                break
            if len(timeline) % 8 == 0:
                print(f"  t={s['t']}s ingest={s['ingest']} results={s['results']} tasks={s['dispatcher_desired']}/"
                      f"{s['indexer_desired']} endpoint={s['endpoint_current']}->{s['endpoint_desired']}")
        time.sleep(1)
    end_dt = datetime.now(UTC)

    # per-item results from the search API (paged)
    docs, cursor = [], None
    while True:
        params = {"tag": tag, "size": "100", **({"cursor": cursor} if cursor else {})}
        code, page = cl.call("GET", "/search", params=params)
        if code != 200:
            break
        docs += page["items"]
        cursor = page.get("next_cursor")
        if not cursor:
            break
    for d in docs:
        if d["image_id"] in items:
            items[d["image_id"]].update(status=d["status"], attempts=d.get("attempts"),
                                        end_to_end_s=round(d.get("end_to_end_ms", 0) / 1000, 1),
                                        processed_at=d.get("processed_at"))
    acts = cl.scaling_activities(since=started - timedelta(seconds=5))
    ep_dims = [{"Name": "EndpointName", "Value": cl.endpoint}]
    epv = ep_dims + [{"Name": "VariantName", "Value": "AllTraffic"}]
    win_start = started - timedelta(minutes=1)
    tib = cw_stats(cl, "AWS/SageMaker", "TimeInBacklog", epv, win_start, end_dt, ["Average", "Maximum", "SampleCount"])
    qwait = cw_stats(cl, cl.p, "IngestQueueWaitMs", [{"Name": "Service", "Value": "dispatcher"}], win_start, end_dt,
                     ext=["p50", "p90", "p99"])
    e2e = [i["end_to_end_s"] for i in items.values() if "end_to_end_s" in i]
    per_min: dict[str, int] = {}
    for i in items.values():
        if i.get("processed_at"):
            per_min[i["processed_at"][:16]] = per_min.get(i["processed_at"][:16], 0) + 1
    dt = 15 / 60
    first_ready = next((s["t"] for s in timeline if s["endpoint_current"] > 0), None)
    summary = {
        "run": run, "region": cl.region, "items_uploaded": total,
        "items_analyzed": sum(1 for i in items.values() if i.get("status") == "analyzed"),
        "makespan_s": round(done_at - t0) if done_at else None,
        "end_to_end_p50_s": pct(e2e, 50), "end_to_end_p90_s": pct(e2e, 90), "end_to_end_max_s": max(e2e, default=None),
        "endpoint_time_in_backlog_max_ms": max((d["Maximum"] for d in tib), default=None),
        "ingest_queue_wait_p90_ms_max_minute": max((d["ExtendedStatistics"]["p90"] for d in qwait), default=None),
        "throughput_peak_per_minute": max(per_min.values(), default=0),
        "endpoint_instance_minutes": round(sum(s["endpoint_current"] for s in timeline) * dt, 2),
        "dispatcher_task_minutes": round(sum(s["dispatcher_running"] for s in timeline) * dt, 2),
        "indexer_task_minutes": round(sum(s["indexer_running"] for s in timeline) * dt, 2),
        "time_to_first_endpoint_instance_s": first_ready,
        "max_endpoint_instances": max((s["endpoint_current"] for s in timeline), default=0),
        "max_dispatcher_tasks": max((s["dispatcher_running"] for s in timeline), default=0),
        "scaling_activities": len(acts),
        "observed_window_s": timeline[-1]["t"] if timeline else 0,
    }
    if summary["endpoint_instance_minutes"] and e2e:
        summary["items_per_endpoint_instance_minute"] = round(len(e2e) / summary["endpoint_instance_minutes"], 3)
    write_csv(out / "timeline.csv", timeline)
    write_csv(out / "scaling-activities.csv", acts)
    write_csv(out / "items.csv", [{k: i.get(k) for k in ("image_id", "uploaded_s", "post_status", "status", "attempts",
                                                         "end_to_end_s", "processed_at")} for i in items.values()])
    with (out / "cloudwatch.json").open("w") as f:
        json.dump({"TimeInBacklog": tib, "IngestQueueWaitMs": qwait}, f, default=str, indent=2)
    (out / "summary.json").write_text(redact(json.dumps(summary, indent=2)))
    evs = []
    for act in acts:
        st = datetime.fromisoformat(act["start"])
        evs.append({"t": (st - started).total_seconds(), "label": f"{act['resource']}: {act['description'][:80]}"})
    (out / "timeline.svg").write_text(timeline_svg(
        timeline, [("ingest", "Ingest queue (visible + in flight)"), ("dispatcher_running", "Dispatcher tasks running"),
                   ("results", "Results queue (visible + in flight)"),
                   ("endpoint_current", "Endpoint instances (current)")], evs, title=f"Load test {run}"))
    md = [f"# Load test {run}", "", f"Region {cl.region}; profile {a.config}; burst {a.burst}, sustained "
          f"{a.sustained_per_minute}/min for {a.sustained_seconds}s.", "", "| Measure | Value |", "|---|---|"]
    md += [f"| {k} | {v} |" for k, v in summary.items()]
    md += ["", "## Scaling activities", "", "| start (UTC) | resource | status | description |", "|---|---|---|---|"]
    md += [f"| {x['start'][11:19]} | {x['resource']} | {x['status']} | {x['description'][:120]} |" for x in acts]
    (out / "summary.md").write_text(redact("\n".join(md) + "\n"))
    print(redact(json.dumps(summary, indent=2)))
    print(f"[loadtest] wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
