# Single entry point. Targets used by CI (lint, test, synth, scan, demo-offline) only check and never change files.
SHELL := /bin/bash
IMAGING_CONFIG ?= config/sandbox.json
CDK := npx -y aws-cdk@2
export IMAGING_CONFIG

.PHONY: help init preflight lint test synth deploy verify loadtest destroy verify-cleanup scan demo-offline demo

help:
	@echo "targets: init preflight lint test synth deploy verify loadtest destroy verify-cleanup scan demo-offline demo"
	@echo "IMAGING_CONFIG=$(IMAGING_CONFIG) (local copy of config/*.example.json created by 'make init')"

init:  ## create local profiles from the committed templates (never overwrites)
	uv sync
	@for f in config/*.example.json; do t="$${f%.example.json}.json"; \
	  if [ -e "$$t" ]; then echo "keep $$t"; else cp "$$f" "$$t"; echo "created $$t from $$f"; fi; done

preflight:  ## read-only: account/region confirmation, toolchain, availability/quotas, destructive-change guard
	bash scripts/preflight.sh

lint:
	uv run ruff check .
	@for f in $$(git ls-files '*.sh'); do bash -n "$$f" || exit 1; done

test:
	uv run pytest

synth:  ## both templates; cdk-nag AwsSolutions must be clean (no AWS credentials needed)
	$(CDK) synth -q -c config=config/sandbox.example.json -o cdk.out/sandbox
	$(CDK) synth -q -c config=config/handover.example.json -c synthCheck=true -o cdk.out/handover

deploy:  ## preflight + network/storage + CodeBuild (image, model) + remaining stacks
	bash scripts/deploy.sh

verify:  ## end-to-end checks against the deployed sandbox (D1, D3, D4, deny paths)
	uv run python scripts/e2e_cloud.py --config $(IMAGING_CONFIG)

loadtest:  ## minimal burst load test with the scaling timeline (D2)
	uv run python scripts/loadtest.py --config $(IMAGING_CONFIG) --label elastic

destroy:  ## two confirmations; warns and continues per step; ends with the residual check
	bash scripts/destroy.sh

verify-cleanup:  ## non-zero exit when anything of this project is left
	uv run python scripts/check_residuals.py --config $(IMAGING_CONFIG)

scan:  ## gitleaks over the full history plus a canary that must be detected
	bash scripts/secret_scan.sh

demo-offline:
	bash scripts/demo.sh offline

demo:
	bash scripts/demo.sh cloud
