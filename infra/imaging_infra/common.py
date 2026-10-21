"""Helpers shared by the stacks."""

from __future__ import annotations

from aws_cdk import RemovalPolicy
from aws_cdk import aws_logs as logs

from .config import AppConfig


def removal(cfg: AppConfig) -> RemovalPolicy:
    return RemovalPolicy.DESTROY if cfg.destroy_on_delete else RemovalPolicy.RETAIN


RETENTION = {1: "ONE_DAY", 3: "THREE_DAYS", 5: "FIVE_DAYS", 7: "ONE_WEEK", 14: "TWO_WEEKS", 30: "ONE_MONTH",
             60: "TWO_MONTHS", 90: "THREE_MONTHS", 120: "FOUR_MONTHS", 150: "FIVE_MONTHS", 180: "SIX_MONTHS",
             365: "ONE_YEAR"}


def log_retention(cfg: AppConfig) -> logs.RetentionDays:
    return getattr(logs.RetentionDays, RETENTION[cfg.log_retention_days])


def bucket_name(cfg: AppConfig, short: str) -> str:
    """Deterministic bucket names (also used to build IAM ARNs without cross-stack references)."""
    from aws_cdk import Aws

    return f"{cfg.project}-{short}-{Aws.ACCOUNT_ID}-{Aws.REGION}"


def s3_objects(cfg: AppConfig, short: str, pattern: str) -> str:
    from aws_cdk import Aws

    return f"arn:{Aws.PARTITION}:s3:::{bucket_name(cfg, short)}/{pattern}"


def allow(principal, sid: str, actions: list[str], resources: list[str], conditions: dict | None = None) -> None:
    from aws_cdk import aws_iam as iam

    target = principal if hasattr(principal, "add_to_principal_policy") else principal.role
    target.add_to_principal_policy(iam.PolicyStatement(sid=sid, actions=actions, resources=resources,
                                                           conditions=conditions))


def harden_key_policies(stack) -> int:
    """Adds aws:SourceAccount to key-policy statements that grant a service principal without any condition
    (CDK adds such statements for S3 notifications and SNS deliveries). Returns the number of statements changed."""
    from aws_cdk import Aws
    from aws_cdk import aws_kms as kms

    changed = 0
    for node in stack.node.find_all():
        if not isinstance(node, kms.CfnKey):
            continue
        doc = stack.resolve(node.key_policy)
        for st in doc.get("Statement", []):
            if "Service" in (st.get("Principal") or {}) and not st.get("Condition"):
                st["Condition"] = {"StringEquals": {"aws:SourceAccount": stack.resolve(Aws.ACCOUNT_ID)}}
                changed += 1
        if changed:
            node.add_property_override("KeyPolicy", doc)
    return changed
