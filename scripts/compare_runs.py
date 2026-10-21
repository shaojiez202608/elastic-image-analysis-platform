"""D5 on the cloud: compare a fixed-capacity load-test run with an elastic run of the same load.

    uv run python scripts/compare_runs.py out/loadtest-fixed-<UTC> out/loadtest-elastic-<UTC> [--out out/compare.md]

Prints relative values only (elastic divided by fixed); no absolute figures are interpreted.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from imaging.report import comparison_markdown  # noqa: E402

KEYS = [
    ("end_to_end_p50_s", "median upload-to-indexed time; elastic includes start-up of new capacity"),
    ("end_to_end_p90_s", "tail during the burst"),
    ("makespan_s", "time until the whole load was indexed"),
    ("endpoint_instance_minutes_common_window", "provisioned inference capacity over the same window"),
    ("dispatcher_task_minutes_common_window", "provisioned worker capacity over the same window"),
    ("indexer_task_minutes_common_window", "provisioned indexer capacity over the same window"),
    ("items_per_endpoint_instance_minute_common_window",
     "work done per unit of provisioned inference capacity over the same window (utilisation proxy)"),
    ("time_to_first_endpoint_instance_s", "cold start from zero (the fixed run already has capacity running)"),
]


CAPACITY = (("endpoint_current", "endpoint_instance_minutes_common_window"),
            ("dispatcher_running", "dispatcher_task_minutes_common_window"),
            ("indexer_running", "indexer_task_minutes_common_window"))


def extend_to_window(run_dir: Path, summary: dict, window_s: float) -> None:
    """Capacity-minutes over a common window: a run observed for a shorter time is extended with its last
    sampled capacity (exact for a fixed-capacity run, whose capacity does not change)."""
    rows = list(csv.DictReader((run_dir / "timeline.csv").open()))
    own = float(summary.get("observed_window_s") or 0)
    for col, key in CAPACITY:
        minutes = sum(float(r[col]) for r in rows) * 15 / 60
        last = float(rows[-1][col]) if rows else 0.0
        summary[key] = round(minutes + last * max(0.0, window_s - own) / 60, 2)
    cap = summary["endpoint_instance_minutes_common_window"]
    summary["items_per_endpoint_instance_minute_common_window"] = (
        round(summary.get("items_analyzed", 0) / cap, 3) if cap else None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixed")
    ap.add_argument("elastic")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    f = json.loads((Path(a.fixed) / "summary.json").read_text())
    e = json.loads((Path(a.elastic) / "summary.json").read_text())
    window = max(float(f.get("observed_window_s") or 0), float(e.get("observed_window_s") or 0))
    extend_to_window(Path(a.fixed), f, window)
    extend_to_window(Path(a.elastic), e, window)
    md = "\n".join([f"# Fixed vs elastic: {f['run']} vs {e['run']}", "",
                    "Same load pattern, same window rules; values are elastic relative to fixed.", "",
                    comparison_markdown(f, e, KEYS), ""])
    if f.get("observed_window_s") != e.get("observed_window_s"):
        md += ("\nThe observed windows differ; capacity is compared over the longer window, extending the shorter run "
               "with its last sampled capacity (exact for the fixed run).\n")
    print(md)
    if a.out:
        Path(a.out).write_text(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
