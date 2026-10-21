"""Offline demo: D1-D5 in local mode (no AWS, no Docker). Prints a readable summary.

    uv run python scripts/local_demo.py [--out out/local-demo]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from imaging import jobs as J  # noqa: E402
from imaging.local_platform import LocalPlatform  # noqa: E402
from imaging.synthetic import BAD_CASES, bad_file, good_image  # noqa: E402


def section(title: str) -> None:
    print(f"\n== {title}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out/local-demo")
    a = ap.parse_args()
    ok = True
    p = LocalPlatform()

    section("D1 basic flow: 20 synthetic images")
    ids = [p.upload(good_image(i), "image/jpeg", category="batch", tags=["demo"]) for i in range(20)]
    print(f"  ingest queue depth after upload: {p.ingest.depth()}")
    steps = p.run_until_idle()
    done = sum(p.jobs.get(i).status == J.DONE for i in ids)
    print(f"  processed in {steps} steps; DONE {done}/20; searchable: {p.search({'status': 'analyzed'})['total']}")
    ok &= done == 20

    section("D3 failure handling: bad files, timeout, transient error, duplicates")
    bad = {p.upload(*bad_file(c)[:2]): bad_file(c)[2] for c in sorted(BAD_CASES)}
    t_id = p.upload(good_image(100), "image/jpeg", fault="timeout")
    e_id = p.upload(good_image(101), "image/jpeg", fault="error_once")
    good = [p.upload(good_image(200 + i), "image/jpeg") for i in range(5)]
    replay = p.ingest.bodies()[-1]
    p.run_until_idle()
    writes_before = p.counters.get("IndexWrites", 0)
    p.ingest.send(replay)
    p.run_until_idle()
    rej = sum(p.jobs.get(i).status == J.REJECTED and p.jobs.get(i).reason == r for i, r in bad.items())
    print(f"  bad files rejected with the expected reason: {rej}/{len(bad)} (never sent to the model, not in DLQ)")
    tj, ej = p.jobs.get(t_id), p.jobs.get(e_id)
    print(f"  timeout image: {tj.status} after {tj.attempt} attempts, reason {tj.reason}; DLQ depth {p.ingest_dlq.depth()}")
    print(f"  transient-error image: {ej.status} on attempt {ej.attempt}")
    print(f"  good images unaffected: {sum(p.jobs.get(i).status == J.DONE for i in good)}/5")
    print(f"  duplicate S3 event replayed: index writes {writes_before} -> {p.counters.get('IndexWrites', 0)}")
    ok &= rej == len(bad) and tj.status == J.FAILED and ej.status == J.DONE and p.ingest_dlq.depth() == 1
    ok &= p.counters.get("IndexWrites", 0) == writes_before

    section("D4 search: filters and cursor pagination")
    seen, cursor, pages = [], None, 0
    while True:
        params = {"category": "batch", "tag": "demo", "size": "7", **({"cursor": cursor} if cursor else {})}
        page = p.search(params)
        pages += 1
        seen += [d["image_id"] for d in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    print(f"  category=batch tag=demo: total {page['total']}, {pages} pages of 7, unique ids {len(set(seen))}")
    print(f"  status=rejected: {p.search({'status': 'rejected'})['total']}; status=failed: {p.search({'status': 'failed'})['total']}")
    ok &= len(set(seen)) == 20

    section("D2/D5 scaling timeline and fixed-vs-elastic (simulation)")
    import simulate
    r = simulate.run(Path(a.out) / "sim", ROOT / "scripts" / "sim_profile.json")
    e, f = r["elastic"], r["fixed"]
    print(f"  scaling events (elastic): {len(r['events'])}; timeline: {a.out}/sim/timeline-elastic.svg")
    for k in ("queue_wait_p95_s", "instance_seconds", "inference_utilisation"):
        print(f"  {k}: elastic/fixed = {e[k] / f[k]:.2f}x" if f[k] else f"  {k}: n/a")
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "counters.json").write_text(json.dumps(p.counters, indent=2, sort_keys=True))
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
