"""D5 on the cloud: compare a fixed-capacity load-test run with an elastic run of the same load.

    uv run python scripts/compare_runs.py out/loadtest-fixed-<UTC> out/loadtest-elastic-<UTC> [--out out/compare.md]

Prints relative values only (elastic divided by fixed); no absolute figures are interpreted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from imaging.report import comparison_markdown  # noqa: E402

KEYS = [
    ("end_to_end_p50_s", "median upload-to-indexed time; elastic includes start-up of new capacity"),
    ("end_to_end_p90_s", "tail during the burst"),
    ("makespan_s", "time until the whole load was indexed"),
    ("endpoint_instance_minutes", "provisioned inference capacity over the observed window"),
    ("dispatcher_task_minutes", "provisioned worker capacity over the observed window"),
    ("items_per_endpoint_instance_minute", "work done per unit of provisioned inference capacity (utilisation proxy)"),
    ("time_to_first_endpoint_instance_s", "cold start when the endpoint starts from zero"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixed")
    ap.add_argument("elastic")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    f = json.loads((Path(a.fixed) / "summary.json").read_text())
    e = json.loads((Path(a.elastic) / "summary.json").read_text())
    md = "\n".join([f"# Fixed vs elastic: {f['run']} vs {e['run']}", "",
                    "Same load pattern, same window rules; values are elastic relative to fixed.", "",
                    comparison_markdown(f, e, KEYS), ""])
    if f.get("observed_window_s") != e.get("observed_window_s"):
        md += ("\nNote: the observed windows differ; capacity-minutes are compared over each run's own window.\n")
    print(md)
    if a.out:
        Path(a.out).write_text(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
