"""Switch the deployed sandbox between the elastic policy (profile values) and a fixed-capacity reference (D5).

    uv run python scripts/set_capacity.py fixed --workers 2 --indexers 1 --instances 2 [--wait]
    uv run python scripts/set_capacity.py elastic

`fixed` sets min = max on the three scalable targets (the policies stay attached but cannot move
capacity); `elastic` restores min/max from the profile. Only this project's scalable targets are touched.
Re-running `make deploy` also restores the profile values.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cloudlib import Cloud  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["fixed", "elastic"])
    ap.add_argument("--config", default="config/sandbox.json")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--indexers", type=int, default=1)
    ap.add_argument("--instances", type=int, default=2)
    ap.add_argument("--wait", action="store_true", help="wait until the fixed capacity is in service")
    a = ap.parse_args()
    cl = Cloud(a.config)
    c, aas = cl.cfg, cl.c("application-autoscaling")
    targets = [
        ("ecs", f"service/{cl.cluster}/{cl.p}-dispatcher", "ecs:service:DesiredCount", a.workers,
         c["workers"]["dispatcher"]["min_tasks"], c["workers"]["dispatcher"]["max_tasks"]),
        ("ecs", f"service/{cl.cluster}/{cl.p}-indexer", "ecs:service:DesiredCount", a.indexers,
         c["workers"]["indexer"]["min_tasks"], c["workers"]["indexer"]["max_tasks"]),
        ("sagemaker", f"endpoint/{cl.endpoint}/variant/AllTraffic", "sagemaker:variant:DesiredInstanceCount",
         a.instances, c["inference"]["min_instances"], c["inference"]["max_instances"]),
    ]
    for ns, rid, dim, fixed, lo, hi in targets:
        mn, mx = (fixed, fixed) if a.mode == "fixed" else (lo, hi)
        aas.register_scalable_target(ServiceNamespace=ns, ResourceId=rid, ScalableDimension=dim,
                                     MinCapacity=mn, MaxCapacity=mx)
        print(f"  {rid.split('/')[-1] if ns == 'ecs' else 'endpoint'}: min {mn} max {mx}")
    if a.mode == "fixed" and a.wait:
        for _ in range(80):
            ep, sv = cl.endpoint_state(), cl.services()
            if ep["current"] == a.instances and ep["status"] == "InService" and \
                    sv["dispatcher"]["running"] == a.workers and sv["indexer"]["running"] == a.indexers:
                print("  fixed capacity in service")
                return 0
            time.sleep(15)
        print("  timed out waiting for fixed capacity", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
