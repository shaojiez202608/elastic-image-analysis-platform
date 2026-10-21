#!/usr/bin/env bash
# One-command deployment: preflight -> network+storage stacks -> CodeBuild (image + model) -> remaining stacks.
# Usage: [IMAGING_CONFIG=...] [CONFIRM=<LAST_4>] [ALLOW_DESTRUCTIVE=1] scripts/deploy.sh
set -euo pipefail
source "$(dirname "$0")/lib/env.sh"
cd "$ROOT"
bash scripts/preflight.sh
echo "[deploy] 1/3 network and storage"
$CDK deploy "$PROJECT-network" "$PROJECT-storage" -c config="$IMAGING_CONFIG" $(ctx_args) --require-approval never \
  --exclusively -o cdk.out/deploy
echo "[deploy] 2/3 build worker image and model artifact"
bash scripts/build.sh
echo "[deploy] 3/3 inference, search, compute, observability"
$CDK deploy --all -c config="$IMAGING_CONFIG" $(ctx_args) --require-approval never --concurrency 2 -o cdk.out/deploy
# keep the asset manifests of what was deployed (destroy re-synthesizes and overwrites cdk.out)
mkdir -p build/assets && cp cdk.out/deploy/*.assets.json build/assets/ 2>/dev/null || true
echo "[deploy] done"
