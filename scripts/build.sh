#!/usr/bin/env bash
# Builds the ARM64 worker image and the model artifact in CodeBuild (no local Docker).
#   source = `git archive HEAD` (committed tree only) -> s3://<ARTIFACTS>/source/<SHA>.zip
#   image  = <ECR_REPO>:<SHA>; model = s3://<ARTIFACTS>/model/<CODE_HASH>/model.tar.gz
# Writes build/deploy-context.json (imageTag, modelKey, inferenceImage) for the CDK deploy.
# Usage: [IMAGING_CONFIG=...] scripts/build.sh
set -euo pipefail
source "$(dirname "$0")/lib/env.sh"
cd "$ROOT"
SHA=$(git rev-parse --short=12 HEAD)
[ -z "$(git status --porcelain -- src docker buildspec model scripts/package_model.py)" ] \
  || echo "  WARNING: uncommitted changes in build inputs are NOT part of the build (git archive HEAD)" >&2
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ART="$PROJECT-artifacts-$ACCOUNT-$REGION"
REPO_URI="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$PROJECT-worker"
MODEL_KEY=$(python3 scripts/package_model.py --print-key)
IMG=$(uv run python scripts/resolve_inference_image.py --config "$IMAGING_CONFIG")
mkdir -p build
git archive --format=zip -o "build/source-$SHA.zip" HEAD
aws s3 cp "build/source-$SHA.zip" "s3://$ART/source/$SHA.zip" --only-show-errors
echo "[build] starting CodeBuild $PROJECT-build for $SHA"
BID=$(aws codebuild start-build --project-name "$PROJECT-build" --source-location-override "$ART/source/$SHA.zip" \
  --environment-variables-override name=REPO_URI,value="$REPO_URI" name=IMAGE_TAG,value="$SHA" \
    name=ARTIFACT_BUCKET,value="$ART" name=MODEL_KEY,value="$MODEL_KEY" --query build.id --output text)
while :; do
  st=$(aws codebuild batch-get-builds --ids "$BID" --query 'builds[0].buildStatus' --output text)
  [ "$st" = "IN_PROGRESS" ] || break
  sleep 15
done
echo "[build] status $st"
[ "$st" = "SUCCEEDED" ] || { echo "build failed; logs in /$PROJECT/codebuild" >&2; exit 1; }
python3 - "$STATE" "$SHA" "$MODEL_KEY" "$IMG" <<'PY'
import json, sys
json.dump({"imageTag": sys.argv[2], "modelKey": sys.argv[3], "inferenceImage": sys.argv[4]}, open(sys.argv[1], "w"), indent=2)
PY
echo "[build] image tag $SHA, model key $MODEL_KEY (context in build/deploy-context.json)"
