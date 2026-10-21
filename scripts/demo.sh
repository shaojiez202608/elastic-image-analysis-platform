#!/usr/bin/env bash
# One-command demo.
#   scripts/demo.sh offline   D1-D5 in local mode (no AWS, no Docker): pipeline, failures, search, simulated
#                             scaling timeline and fixed-vs-elastic table -> out/local-demo/
#   scripts/demo.sh cloud     against the deployed sandbox: end-to-end checks (D1, D3, D4, deny paths) and a
#                             minimal burst load test with the scaling timeline (D2) -> out/
#   scripts/demo.sh reset     local: remove out/; cloud data is reset by destroy + deploy (see docs/demo-script.md)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
case "${1:-offline}" in
  offline)
    uv run python scripts/local_demo.py --out out/local-demo
    echo "timeline: out/local-demo/sim/timeline-elastic.svg   comparison: out/local-demo/sim/comparison.md" ;;
  cloud)
    uv run python scripts/e2e_cloud.py --config "${IMAGING_CONFIG:-config/sandbox.json}"
    uv run python scripts/loadtest.py --config "${IMAGING_CONFIG:-config/sandbox.json}" --label elastic \
      --burst "${BURST:-40}" --idle-seconds "${IDLE_SECONDS:-600}" ;;
  reset)
    rm -r out 2>/dev/null || true; echo "local outputs removed" ;;
  *) echo "usage: scripts/demo.sh offline|cloud|reset" >&2; exit 2 ;;
esac
