"""Builds all stacks from one config file; shared by app.py and the unit tests."""

from __future__ import annotations

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from .common import harden_key_policies
from .compute_stack import ComputeStack
from .config import AppConfig, load
from .inference_stack import InferenceStack, placeholder_image
from .nag import apply_suppressions
from .network_stack import NetworkStack
from .observability_stack import ObservabilityStack
from .search_stack import SearchStack
from .storage_stack import StorageStack
from .trust import apply_trust_conditions

PLACEHOLDER_TAG = "not-built"
PLACEHOLDER_MODEL_KEY = "model/not-built/model.tar.gz"


def build_app(app: cdk.App, *, config_path: str, image_tag: str | None = None, model_key: str | None = None,
              inference_image: str | None = None, account: str | None = None, nag: bool = True,
              synth_check: bool = False) -> dict[str, cdk.Stack]:
    cfg: AppConfig = load(config_path, synth_check=synth_check)
    # The account stays a CloudFormation token, so no account ID ends up in templates or nag output.
    env = cdk.Environment(account=account, region=cfg.region)
    d = cfg.project

    network = NetworkStack(app, f"{d}-network", cfg=cfg, env=env, description="Elastic imaging: VPC and endpoints (prototype)")
    storage = StorageStack(app, f"{d}-storage", cfg=cfg, env=env,
                           description="Elastic imaging: buckets, queues, topics, job table, image build (prototype)")
    inference = InferenceStack(app, f"{d}-inference", cfg=cfg, storage=storage, env=env,
                               image_uri=inference_image or placeholder_image(cfg),
                               model_key=model_key or PLACEHOLDER_MODEL_KEY,
                               description="Elastic imaging: SageMaker AI asynchronous endpoint and scaling (prototype)")
    search = SearchStack(app, f"{d}-search", cfg=cfg, network=network, storage=storage, env=env,
                         description="Elastic imaging: OpenSearch domain and query API (prototype)")
    compute = ComputeStack(app, f"{d}-compute", cfg=cfg, network=network, storage=storage, inference=inference,
                           search=search, image_tag=image_tag or PLACEHOLDER_TAG, env=env,
                           description="Elastic imaging: ECS Fargate ARM64 workers and queue-driven scaling (prototype)")
    obs = ObservabilityStack(app, f"{d}-observability", cfg=cfg, storage=storage, inference=inference,
                             compute=compute, env=env, description="Elastic imaging: dashboard and alarms (prototype)")
    stacks = {s.stack_name: s for s in (network, storage, inference, search, compute, obs)}
    for key, value in cfg.tags.items():
        cdk.Tags.of(app).add(key, value)
    for stack in stacks.values():
        apply_trust_conditions(stack, cfg)
        harden_key_policies(stack)
        apply_suppressions(stack, cfg)
    if nag:
        cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app, verbose=True))
    return stacks
