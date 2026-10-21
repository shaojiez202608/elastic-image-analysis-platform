#!/usr/bin/env bash
# Read-only pre-deploy checks. Changes nothing in the account.
#   1. credentials: account (last 4 digits only) and region; you confirm they are the intended ones
#   2. toolchain: python3, uv, node/npx, AWS CLI v2, git (Docker is not needed: images build in CodeBuild)
#   3. region availability and quotas: SageMaker AI endpoint instance quota for the configured type,
#      OpenSearch instance type for the engine version, prebuilt inference image tag, VPC quota,
#      service-linked roles already present, API Gateway CloudWatch role already set, CDK bootstrap
#   4. destructive-change guard on `cdk diff` (see scripts/check_destructive.py)
# Usage: [IMAGING_CONFIG=config/<PROFILE>.json] [CONFIRM=<LAST_4_DIGITS>] scripts/preflight.sh
set -uo pipefail
source "$(dirname "$0")/lib/env.sh"
fail=0
ok() { printf '  ok    %s\n' "$*"; }
bad() { printf '  FAIL  %s\n' "$*"; fail=1; }
note() { printf '  note  %s\n' "$*"; }

echo "[preflight] profile $IMAGING_CONFIG, prefix $PROJECT, region $REGION"
(cd "$ROOT" && uv run python -c 'import sys; sys.path.insert(0,"infra"); from imaging_infra.config import load; load(sys.argv[1])' "$IMAGING_CONFIG") \
  >/dev/null 2>/tmp/imaging-preflight.err && ok "configuration valid" || bad "configuration invalid: $(tail -1 /tmp/imaging-preflight.err)"

echo "[preflight] 1. credentials"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || { bad "no valid AWS credentials"; exit 1; }
LAST4="${ACCOUNT: -4}"
echo "  target account ********$LAST4, region $REGION"
if [ "${CONFIRM:-}" != "$LAST4" ]; then
  if [ -t 0 ]; then
    read -r -p "  type the last 4 digits of the account to confirm: " answer
    [ "$answer" = "$LAST4" ] || { bad "account not confirmed; stop if this is not your intended account"; exit 1; }
  else
    bad "set CONFIRM=<LAST_4_DIGITS_OF_THE_TARGET_ACCOUNT> (non-interactive run)"; exit 1
  fi
fi
ok "account and region confirmed"

echo "[preflight] 2. toolchain"
for t in python3 uv node npx aws git; do command -v "$t" >/dev/null && ok "$t" || bad "$t missing"; done
aws --version 2>&1 | grep -q 'aws-cli/2' && ok "AWS CLI v2" || bad "AWS CLI v2 required"
note "Docker not required (worker image and model artifact are built in CodeBuild)"

echo "[preflight] 3. region availability and quotas"
SM_TYPE=$(cfg inference.instance_type); SM_MAX=$(cfg inference.max_instances)
q=$(aws service-quotas list-service-quotas --service-code sagemaker --query "Quotas[?QuotaName=='$SM_TYPE for endpoint usage'].Value" --output text 2>/dev/null | tr '\t' '\n' | grep -v -x -e None -e '' | head -1)
if [ -z "$q" ] || [ "$q" = "None" ]; then bad "$SM_TYPE: no endpoint-usage quota listed in $REGION (type not offered or not readable)"
else
  used=$(aws sagemaker list-endpoints --query 'length(Endpoints)' --output text 2>/dev/null)
  [ "${q%.*}" -ge "$SM_MAX" ] && ok "$SM_TYPE endpoint quota >= max_instances ($SM_MAX); endpoints in region: $used" \
    || bad "$SM_TYPE endpoint quota below max_instances ($SM_MAX): lower max_instances or pick another type (do not request increases from this script)"
fi
OS_TYPE=$(cfg search.instance_type); OS_VER=$(cfg search.engine_version)
aws opensearch list-instance-type-details --engine-version "$OS_VER" --query 'InstanceTypeDetails[].InstanceType' --output text 2>/dev/null \
  | tr '\t' '\n' | grep -qx "$OS_TYPE" && ok "OpenSearch $OS_VER on $OS_TYPE" || bad "$OS_TYPE not offered for $OS_VER in $REGION"
IMG=$(cd "$ROOT" && uv run python scripts/resolve_inference_image.py --config "$IMAGING_CONFIG" --verify 2>/dev/null) \
  && ok "prebuilt inference image tag exists: $(echo "$IMG" | redact)" || bad "prebuilt inference image not resolvable for the profile"
vpcs=$(aws ec2 describe-vpcs --query 'length(Vpcs)' --output text)
have=$(aws ec2 describe-vpcs --filters "Name=tag:Name,Values=$PROJECT-vpc" --query 'length(Vpcs)' --output text)
vq=$(aws service-quotas get-service-quota --service-code vpc --quota-code L-F678F1CE --query 'Quota.Value' --output text 2>/dev/null)
if [ "$have" != "0" ]; then ok "VPC $PROJECT-vpc already exists (update)"
elif [ -n "$vq" ] && [ "$vq" != "None" ] && [ "$vpcs" -lt "${vq%.*}" ]; then ok "VPC quota ($vpcs used < ${vq%.*})"
else bad "VPC quota exhausted in $REGION ($vpcs used, quota ${vq:-unknown}); choose another region in the profile"; fi
for slr in AWSServiceRoleForApplicationAutoScaling_SageMakerEndpoint AWSServiceRoleForApplicationAutoScaling_ECSService \
           AWSServiceRoleForAmazonOpenSearchService AWSServiceRoleForECS; do
  aws iam get-role --role-name "$slr" >/dev/null 2>&1 && ok "service-linked role $slr present" \
    || note "service-linked role $slr missing: the service creates it on first use (account-level change; ask the account owner)"
done
aws apigateway get-account --query cloudwatchRoleArn --output text 2>/dev/null | grep -q '^arn:' \
  && ok "API Gateway CloudWatch Logs role is set (not modified by this project)" \
  || bad "API Gateway account has no CloudWatch Logs role: access logging would fail; ask the account owner (account-level setting)"
aws cloudformation describe-stacks --stack-name CDKToolkit --query 'Stacks[0].StackStatus' --output text >/dev/null 2>&1 \
  && ok "CDK bootstrap present (not modified)" || bad "CDKToolkit stack missing in $REGION: bootstrap first"

echo "[preflight] 4. destructive-change guard (cdk diff)"
(cd "$ROOT" && $CDK diff --all -c config="$IMAGING_CONFIG" $(ctx_args) -o cdk.out/preflight --no-color) > /tmp/imaging-preflight-diff.txt 2>&1 \
  || note "cdk diff returned non-zero (differences found or first deployment)"
grep -qE '^Stack ' /tmp/imaging-preflight-diff.txt || bad "cdk diff produced no stack output (see /tmp/imaging-preflight-diff.txt)"
PROTECTED=$([ "$(cfg removal_policy)" = retain ] && echo 1 || echo 0) python3 "$ROOT/scripts/check_destructive.py" /tmp/imaging-preflight-diff.txt || fail=1

[ "$fail" = "0" ] && echo "[preflight] PASS" || echo "[preflight] FAIL"
exit "$fail"
