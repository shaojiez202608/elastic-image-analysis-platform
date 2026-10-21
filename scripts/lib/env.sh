# Sourced by the scripts: resolves the profile and exports PROJECT, REGION, AWS_REGION.
# IMAGING_CONFIG=config/<PROFILE>.json (default config/sandbox.json, a local copy of the template).
ROOT="${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
IMAGING_CONFIG="${IMAGING_CONFIG:-config/sandbox.json}"
CFG_FILE="$ROOT/$IMAGING_CONFIG"
[ -f "$CFG_FILE" ] || { echo "profile $IMAGING_CONFIG not found: run 'make init'" >&2; exit 1; }
cfg() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); [d:=d[k] for k in sys.argv[2].split(".")]; print(d)' "$CFG_FILE" "$1"; }
PROJECT="$(cfg project)"
REGION="$(cfg region)"
export IMAGING_CONFIG PROJECT REGION AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"
STATE="$ROOT/build/deploy-context.json"
CDK="npx -y aws-cdk@2"
redact() { sed -E 's/[0-9]{12}/111122223333/g'; }
ctx_args() {  # cdk context from the last build (image tag, model key, inference image)
  [ -f "$STATE" ] || return 0
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(" ".join(f"-c {k}={v}" for k,v in d.items()))' "$STATE"
}
