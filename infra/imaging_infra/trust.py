"""Confused-deputy protection on service-role trust policies.

Adds aws:SourceAccount (StringEquals) and aws:SourceArn (ArnLike) to the sts:AssumeRole statement of
roles whose service documents these keys for role trust (checked 2026-10-21):
- sagemaker.amazonaws.com (real-time/serverless/asynchronous inference): SourceArn must be exactly
  arn:aws:sagemaker:<REGION>:<ACCOUNT>:* ("do not replace with the ARN of a model or endpoint")
  https://docs.aws.amazon.com/sagemaker/latest/dg/security-confused-deputy-prevention.html
- ecs-tasks.amazonaws.com (task roles): SourceArn arn:aws:ecs:<REGION>:<ACCOUNT>:*
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html
- codebuild.amazonaws.com: SourceArn = this project's CodeBuild project ARN pattern
  https://docs.aws.amazon.com/codebuild/latest/userguide/cross-service-confused-deputy-prevention.html
- vpc-flow-logs.amazonaws.com: SourceArn = flow-log ARN pattern
  https://docs.aws.amazon.com/vpc/latest/userguide/flow-logs-iam-role.html
Roles left unchanged (no documented trust-policy guidance, listed in SECURITY-NOTES.md): Lambda
execution roles, ECS task execution roles, the Lambda roles of CDK-provided custom resources.
"""

from __future__ import annotations

from aws_cdk import Stack
from aws_cdk import aws_iam as iam

from .config import AppConfig


def _services(st: dict) -> set[str]:
    svc = (st.get("Principal") or {}).get("Service")
    return set([svc] if isinstance(svc, str) else (svc or []))


def source_arn_for(service: str, role: iam.CfnRole, stack: Stack, cfg: AppConfig) -> str | None:
    region, account, partition = stack.region, stack.account, stack.partition
    path = role.node.path
    if service == "sagemaker.amazonaws.com":
        return f"arn:{partition}:sagemaker:{region}:{account}:*"
    if service == "ecs-tasks.amazonaws.com" and "TaskRole" in path:
        return f"arn:{partition}:ecs:{region}:{account}:*"
    if service == "codebuild.amazonaws.com":
        return f"arn:{partition}:codebuild:{region}:{account}:project/{cfg.project}-*"
    if service == "vpc-flow-logs.amazonaws.com":
        return f"arn:{partition}:ec2:{region}:{account}:vpc-flow-log/*"
    return None


def apply_trust_conditions(stack: Stack, cfg: AppConfig) -> list[str]:
    """Returns the construct paths that received the conditions (used by tests and docs)."""
    changed = []
    for node in stack.node.find_all():
        if not isinstance(node, iam.CfnRole):
            continue
        doc = stack.resolve(node.assume_role_policy_document)
        if not isinstance(doc, dict):
            continue
        for idx, st in enumerate(doc.get("Statement", [])):
            services = _services(st)
            if len(services) != 1:
                continue
            arn = source_arn_for(next(iter(services)), node, stack, cfg)
            if arn is None:
                continue
            condition = dict(st.get("Condition") or {})
            condition.setdefault("StringEquals", {})["aws:SourceAccount"] = stack.account
            condition.setdefault("ArnLike", {})["aws:SourceArn"] = arn
            node.add_property_override(f"AssumeRolePolicyDocument.Statement.{idx}.Condition", condition)
            changed.append(node.node.path)
    return changed
