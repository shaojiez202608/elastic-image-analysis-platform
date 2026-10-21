"""Local D2/D5: run the same load through the elastic policy and a fixed-capacity reference.

    uv run python scripts/simulate.py [--out out/sim] [--profile scripts/sim_profile.json]

Outputs: timeline-elastic.svg / .csv, timeline-fixed.csv, events-elastic.csv, comparison.md.
Inputs (service times, start-up delays, load) are illustrative choices in the profile file; the
results are only read as relative differences between the two runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from imaging.report import comparison_markdown, timeline_svg, write_csv  # noqa: E402
from imaging.sim import Layer, Phase, SimConfig, fixed, simulate  # noqa: E402

KEYS = [
    ("queue_wait_p50_s", "lower is better; elastic pays the start-up delay of new capacity"),
    ("queue_wait_p95_s", "tail wait during the burst"),
    ("makespan_s", "time until the whole burst is processed"),
    ("instance_seconds", "provisioned inference capacity over the same window"),
    ("task_seconds", "provisioned worker capacity over the same window"),
    ("inference_utilisation", "busy share of provisioned inference capacity; higher is better"),
]


def load_profile(path: Path) -> tuple[list[Phase], SimConfig, dict]:
    p = json.loads(path.read_text())
    phases = [Phase(**ph) for ph in p["phases"]]
    c = p["config"]
    cfg = SimConfig(workers=Layer(**c["workers"]), endpoint=Layer(**c["endpoint"]),
                    dispatch_seconds=c["dispatch_seconds"], inference_seconds=c["inference_seconds"],
                    concurrency_per_instance=c["concurrency_per_instance"],
                    worker_steps=tuple(tuple(s) for s in c["worker_steps"]),
                    target_backlog_per_instance=c["target_backlog_per_instance"])
    return phases, cfg, p["fixed"]


def run(out: Path, profile: Path) -> dict:
    phases, cfg, fx = load_profile(profile)
    out.mkdir(parents=True, exist_ok=True)
    e = simulate(phases, cfg)
    f = simulate(phases, fixed(cfg, fx["workers"], fx["instances"]), tail_seconds=0)
    # same observation window for both
    window = max(e.samples[-1]["t"], f.samples[-1]["t"] if f.samples else 0)
    f2 = simulate(phases, fixed(cfg, fx["workers"], fx["instances"]),
                  tail_seconds=int(window - sum(p.seconds for p in phases)))
    es, fs = e.summary(), f2.summary()
    write_csv(out / "timeline-elastic.csv", e.samples)
    write_csv(out / "timeline-fixed.csv", f2.samples)
    write_csv(out / "events-elastic.csv", e.events)
    evs = [{"t": ev["t"], "label": f"{ev['layer']} {ev['from']}->{ev['to']}"} for ev in e.events]
    (out / "timeline-elastic.svg").write_text(timeline_svg(
        e.samples, [("ingest_backlog", "Ingest queue backlog"), ("tasks", "Worker tasks (desired)"),
                    ("endpoint_backlog", "Endpoint backlog"), ("instances", "Endpoint instances (desired)")],
        evs, title="Simulated burst then idle - elastic policy"))
    md = ["# Fixed vs elastic (local simulation)", "",
          f"Profile: `{profile.relative_to(ROOT) if profile.is_relative_to(ROOT) else profile.name}`. "
          "Illustrative inputs; read only the relative column.", "",
          comparison_markdown(fs, es, KEYS), "",
          "Raw summaries (simulation units, not measurements):", "", "```json",
          json.dumps({"elastic": es, "fixed": fs}, indent=2), "```"]
    (out / "comparison.md").write_text("\n".join(md) + "\n")
    return {"elastic": es, "fixed": fs, "events": e.events}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out/sim")
    ap.add_argument("--profile", default=str(ROOT / "scripts" / "sim_profile.json"))
    a = ap.parse_args()
    r = run(Path(a.out), Path(a.profile))
    print(json.dumps({"elastic": r["elastic"], "fixed": r["fixed"]}, indent=2))
    print(f"wrote {a.out}/comparison.md and timeline-elastic.svg")
    return 0


if __name__ == "__main__":
    sys.exit(main())
