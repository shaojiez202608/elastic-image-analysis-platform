#!/usr/bin/env python3
"""CDK entry point. Deployment commands: README "Deployment" (make deploy).

Context:
  config          path to the single parameter file (default config/sandbox.json)
  imageTag        worker image tag in the ECR repository (written by scripts/build.sh)
  modelKey        S3 key of model.tar.gz in the artifacts bucket (scripts/package_model.py --print-key)
  inferenceImage  prebuilt SageMaker AI PyTorch inference image URI (scripts/resolve_inference_image.py)
  synthCheck      "true": fill blank sizing fields with placeholders so the handover profile can be
                  synthesized and checked with cdk-nag. Not for deployment.
Without imageTag/modelKey/inferenceImage the app synthesizes with placeholders (CI, no credentials).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra"))
sys.path.insert(0, str(ROOT / "src"))

import aws_cdk as cdk  # noqa: E402

from imaging_infra.app_builder import build_app  # noqa: E402

app = cdk.App()
ctx = app.node.try_get_context
build_app(app, config_path=ctx("config") or "config/sandbox.json", image_tag=ctx("imageTag"),
          model_key=ctx("modelKey"), inference_image=ctx("inferenceImage"),
          synth_check=str(ctx("synthCheck")).lower() == "true")
app.synth()
