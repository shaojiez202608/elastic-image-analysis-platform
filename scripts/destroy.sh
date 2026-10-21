#!/usr/bin/env bash
# Tear down everything this project created in the profile's account/region.
#   - confirmation 1: type the prefix (or CONFIRM_DESTROY=<PROJECT>)
#   - confirmation 2: deleting data (buckets, table, search domain contents) is confirmed separately
#     (or CONFIRM_DATA=yes); without it a "retain" profile keeps its data stores and they are listed
#   - every step warns and continues on failure; the residual check runs at the end
# Out-of-stack leftovers handled here: log groups created by services (SageMaker AI endpoint logs,
# Container Insights, CDK handler Lambdas), INACTIVE ECS task-definition revisions, SageMaker AI lineage
# entities created for the endpoint, and this project's objects in the shared CDK bootstrap bucket
# (identified by asset hash or content; provider packages are kept).
# Usage: [IMAGING_CONFIG=...] scripts/destroy.sh
set -uo pipefail
source "$(dirname "$0")/lib/env.sh"
cd "$ROOT"
P="$PROJECT"
REMOVAL=$(cfg removal_policy)
warn() { echo "  WARNING: $*" >&2; }
ask() {  # ask <PROMPT> <EXPECTED> <ENV_VALUE>
  [ "${3:-}" = "$2" ] && return 0
  [ -t 0 ] || { echo "non-interactive: set the confirmation variable" >&2; return 1; }
  read -r -p "$1 " a; [ "$a" = "$2" ]
}
echo "[destroy] profile $IMAGING_CONFIG, prefix $P, region $REGION, removal_policy $REMOVAL"
ask "type the prefix '$P' to destroy all its stacks:" "$P" "${CONFIRM_DESTROY:-}" || { echo "aborted"; exit 1; }
DATA=no
ask "type 'yes' to also delete data (buckets, job table, search domain):" yes "${CONFIRM_DATA:-}" && DATA=yes
[ "$REMOVAL" = "retain" ] && [ "$DATA" = "no" ] && echo "  retain profile: buckets, table, domain and keys stay after stack deletion"

echo "[destroy] 0. keep the deployed asset manifests (cdk destroy re-synthesizes cdk.out)"
mkdir -p build/assets && cp cdk.out/deploy/*.assets.json build/assets/ 2>/dev/null || true

echo "[destroy] 1. cdk destroy"
$CDK destroy --all -c config="$IMAGING_CONFIG" $(ctx_args) --force -o cdk.out/destroy || warn "cdk destroy reported errors; continuing"

echo "[destroy] 2. ECS task definition revisions (INACTIVE after the stack deletion)"
for fam in $(aws ecs list-task-definition-families --family-prefix "$P" --status ALL --query families --output text); do
  for st in ACTIVE INACTIVE; do
    for arn in $(aws ecs list-task-definitions --family-prefix "$fam" --status "$st" --query taskDefinitionArns --output text); do
      [ "$st" = ACTIVE ] && { aws ecs deregister-task-definition --task-definition "$arn" >/dev/null || warn "deregister"; }
      aws ecs delete-task-definitions --task-definitions "$arn" >/dev/null || warn "delete ${arn##*/}"
    done
  done
done

echo "[destroy] 3. log groups created by services outside the stacks"
for lg in $(aws logs describe-log-groups --query "logGroups[?contains(logGroupName,'$P')].logGroupName" --output text); do
  aws logs delete-log-group --log-group-name "$lg" && echo "  deleted $lg" || warn "log group $lg"
done

echo "[destroy] 4. SageMaker AI lineage entities created automatically for the endpoint"
CONFIRM_LINEAGE=yes uv run python scripts/cleanup_lineage.py --config "$IMAGING_CONFIG" --delete || warn "lineage cleanup incomplete"

echo "[destroy] 5. this project's objects in the CDK bootstrap bucket (asset hashes + content identification)"
# --content-scan also finds assets of earlier deployments whose manifests were overwritten (templates must carry
# both the Project tag value and the stack prefix; zips must contain this project's handler).
CONFIRM_ASSETS=yes uv run python scripts/cleanup_bootstrap_assets.py --assembly build/assets --assembly cdk.out --config "$IMAGING_CONFIG" \
  --region "$REGION" --content-scan --delete || warn "bootstrap asset cleanup incomplete"

echo "[destroy] 6. residual check"
uv run python scripts/check_residuals.py --config "$IMAGING_CONFIG"
