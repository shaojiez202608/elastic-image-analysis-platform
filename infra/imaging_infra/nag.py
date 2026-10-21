"""cdk-nag (AwsSolutions pack) acknowledgements, attached to the exact constructs they apply to.

Every entry names a construct path (glob, relative to its stack), the finding and a written reason;
SECURITY-NOTES.md summarises the same list. Nothing is acknowledged stack-wide. An entry whose path
matches no construct raises, so the list cannot drift silently from the stacks. Findings that contain
names are templated from the configuration ({project}, {index}), never hard-coded, so a different
prefix still synthesizes cleanly (tests/test_infra.py checks this). Conditional entries carry `when`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase

from aws_cdk import Acknowledgment, Stack, Validations
from constructs import IConstruct

from .config import AppConfig

P = "AwsSolutions::AwsSolutions-"
LAMBDA_BASIC = "IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]"
LAMBDA_VPC = "IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole]"
S3_OBJ = "IAM5[Resource::arn:<AWS::Partition>:s3:::{project}-{bucket}-<AWS::AccountId>-<AWS::Region>/{prefix}*]"
DOMAIN = "IAM5[Resource::arn:<AWS::Partition>:es:<AWS::Region>:<AWS::AccountId>:domain/{project}-search/{sub}*]"
KMS_GRANT = ("CDK grant helpers for SQS/SNS/CodeBuild on the project's customer managed key add "
             "kms:GenerateDataKey*/ReEncrypt*; the statement is scoped to that single key ARN.")
PREFIX_REASON = ("Object-level access is limited to one key prefix of one project bucket; object keys are "
                 "generated per image, so a wildcard below the prefix is inherent.")
ECR_AUTH = "ecr:GetAuthorizationToken does not support resource-level permissions; image pulls are scoped to the repository."

Cond = Callable[[AppConfig], bool] | None


@dataclass(frozen=True)
class Ack:
    path: str
    finding: str
    reason: str
    when: Cond = None


def _s3(path: str, bucket: str, prefix: str, why: str = PREFIX_REASON) -> Ack:
    return Ack(path, S3_OBJ.replace("{bucket}", bucket).replace("{prefix}", prefix), why)


def _kms(path: str) -> list[Ack]:
    return [Ack(path, "IAM5[Action::kms:GenerateDataKey*]", KMS_GRANT), Ack(path, "IAM5[Action::kms:ReEncrypt*]", KMS_GRANT)]


NETWORK: list[Ack] = []

STORAGE: list[Ack] = [
    Ack("BucketNotificationsHandler*/Role/Resource", LAMBDA_BASIC,
        "CDK-provided S3 notification configuration handler; the AWS managed policy only grants CloudWatch Logs write."),
    *_kms("Build/Role/DefaultPolicy/Resource"),
    *[Ack("Build/Role/DefaultPolicy/Resource", f"IAM5[Action::s3:{a}]",
          "CDK grant for the CodeBuild S3 source on the private artifacts bucket (read-only object access).")
      for a in ("GetBucket*", "GetObject*", "List*")],
    Ack("Build/Role/DefaultPolicy/Resource", "IAM5[Resource::*]", ECR_AUTH),
    Ack("Build/Role/DefaultPolicy/Resource",
        "IAM5[Resource::arn:<AWS::Partition>:codebuild:{region}:<AWS::AccountId>:report-group/<Build45A36621>-*]",
        "CDK default CodeBuild report-group permission, scoped to this project's name prefix."),
    Ack("Build/Role/DefaultPolicy/Resource",
        "IAM5[Resource::arn:<AWS::Partition>:logs:{region}:<AWS::AccountId>:log-group:/aws/codebuild/<Build45A36621>:*]",
        "CDK default CodeBuild log permission, scoped to this project's default log group (logs go to the "
        "explicit log group)."),
    _s3("Build/Role/DefaultPolicy/Resource", "artifacts", "model/", "Build writes the model artifact under model/ only."),
    _s3("Build/Role/DefaultPolicy/Resource", "artifacts", "source/", "Build reads its source archive under source/ only."),
]

INFERENCE: list[Ack] = [
    Ack("ExecutionRole/DefaultPolicy/Resource", "IAM5[Action::kms:GenerateDataKey*]", KMS_GRANT),
    Ack("ExecutionRole/DefaultPolicy/Resource", "IAM5[Resource::*]",
        ECR_AUTH + " cloudwatch:PutMetricData has no resource-level permissions and is limited by a namespace condition."),
    Ack("ExecutionRole/DefaultPolicy/Resource",
        "IAM5[Resource::arn:<AWS::Partition>:logs:<AWS::Region>:<AWS::AccountId>:log-group:/aws/sagemaker/Endpoints/{project}-endpoint:*]",
        "Log streams are created by SageMaker AI per instance inside the endpoint's own log group."),
    _s3("ExecutionRole/DefaultPolicy/Resource", "images", "staging/", "Endpoint reads normalised inputs under staging/ only."),
    _s3("ExecutionRole/DefaultPolicy/Resource", "images", "async-output/", "Endpoint writes results under async-output/ only."),
    _s3("ExecutionRole/DefaultPolicy/Resource", "images", "async-failure/", "Endpoint writes failures under async-failure/ only."),
]

SEARCH: list[Ack] = [
    # CDK's fixed AwsCustomResource singleton id (OpenSearch log-group resource policy), not a secret.
    Ack("AWS679f53fac002430cb0da5b7982bd2287/ServiceRole/Resource", LAMBDA_BASIC,  # pragma: allowlist secret
        "CDK AwsCustomResource singleton; the AWS managed policy only grants CloudWatch Logs write."),
    *[Ack("AWS679f53fac002430cb0da5b7982bd2287/ServiceRole/DefaultPolicy/Resource",  # pragma: allowlist secret
          f"IAM5[Action::kms:{a}]", "CDK AwsCustomResource for the domain's KMS key lookup (read-only describe/list).")
      for a in ("Describe*", "List*")],
    Ack("Domain/ESLogGroupPolicy*/CustomResourcePolicy/Resource", "IAM5[Resource::*]",
        "logs:PutResourcePolicy/DeleteResourcePolicy (CDK-managed OpenSearch log delivery policy) do not support "
        "resource-level permissions."),
    *[Ack(f"Api/Default/{r}/Resource", "COG4", "The API uses IAM (SigV4) authorisation, not Cognito; callers are IAM "
          "principals of this account (resource policy).") for r in ("images/{image_id}/GET", "search/GET", "uploads/POST")],
    Ack("Api/DeploymentStage.v1/Resource", "APIG3", "No public web clients; IAM-authorised API with stage throttling. "
        "AWS WAF is listed in the production hardening checklist."),
    Ack("ApiRole/Resource", LAMBDA_VPC, "AWS managed policy for Lambda ENIs in a VPC (ec2 network-interface actions "
        "have no resource-level scoping) plus CloudWatch Logs write."),
    Ack("SearchMasterRole/Resource", LAMBDA_VPC, "Same as the API role: the bootstrap Lambda runs in the VPC."),
    Ack("ApiRole/DefaultPolicy/Resource", DOMAIN.replace("{sub}", "{index}/"),
        "Read/search requests on the images index only; FGAC (imaging_reader role) enforces the same at the cluster."),
    _s3("ApiRole/DefaultPolicy/Resource", "images", "uploads/",
        "The presigned POST is signed by this role; it can only target the uploads/ prefix (key pinned per image)."),
    Ack("SearchMasterRole/DefaultPolicy/Resource", DOMAIN.replace("{sub}", ""),
        "FGAC master role: needs the security plugin REST API (_plugins/_security) and index creation; used only "
        "by the bootstrap custom resource."),
    Ack("SearchBootstrapProvider/framework-onEvent/ServiceRole/Resource", LAMBDA_BASIC,
        "CDK Provider framework Lambda; the AWS managed policy only grants CloudWatch Logs write."),
    Ack("SearchBootstrapProvider/framework-onEvent/ServiceRole/DefaultPolicy/Resource",
        "IAM5[Resource::<SearchBootstrapFnDE993518.Arn>:*]",
        "CDK Provider framework invokes versions/aliases of the single bootstrap function."),
    Ack("Domain/Resource", "OS3", "VPC-only domain in isolated subnets reachable from three security groups; an IP "
        "allowlist does not apply to VPC domains."),
    Ack("Domain/Resource", "OS4", "Dedicated manager nodes are a production sizing decision (hardening checklist)."),
    Ack("Domain/Resource", "OS7", "Zone awareness is enabled automatically with more than one data node; this "
        "profile has one.", when=lambda c: c.search.data_nodes <= 1),
    *[Ack("Domain/Resource", f"OS9[LogExport::{t}]", "Application logs are published; slow logs can be enabled when "
          "tuning queries.") for t in ("INDEX_SLOW_LOGS", "SEARCH_SLOW_LOGS")],
]

COMPUTE: list[Ack] = [
    *[a for svc in ("Dispatcher", "Indexer") for a in (
        Ack(f"{svc}ExecutionRole/DefaultPolicy/Resource", "IAM5[Resource::*]", ECR_AUTH),
        Ack(f"{svc}Task/Resource", "ECS2", "The environment holds only resource names, URLs and limits; the "
            "service needs no secrets."),
        *_kms(f"{svc}TaskRole/DefaultPolicy/Resource"))],
    _s3("DispatcherTaskRole/DefaultPolicy/Resource", "images", "uploads/", "Dispatcher reads uploaded originals only."),
    _s3("DispatcherTaskRole/DefaultPolicy/Resource", "images", "staging/", "Dispatcher writes normalised images only."),
    _s3("IndexerTaskRole/DefaultPolicy/Resource", "images", "async-output/", "Indexer reads inference outputs only."),
    Ack("IndexerTaskRole/DefaultPolicy/Resource", DOMAIN.replace("{sub}", "{index}/"),
        "Index/get on the images index only; FGAC (imaging_writer role) enforces the same at the cluster."),
]

OBSERVABILITY: list[Ack] = []

BY_KIND = {"network": NETWORK, "storage": STORAGE, "inference": INFERENCE, "search": SEARCH, "compute": COMPUTE,
           "observability": OBSERVABILITY}


def _relative(stack: Stack, node: IConstruct) -> str:
    return node.node.path[len(stack.node.path) + 1:]


def acknowledgements(stack: Stack, cfg: AppConfig) -> list[tuple[IConstruct, Acknowledgment]]:
    from imaging.documents import INDEX_NAME  # noqa: PLC0415

    kind = stack.stack_name.removeprefix(cfg.project + "-")
    nodes = [n for n in stack.node.find_all() if n is not stack]
    fmt = {"region": stack.region, "project": cfg.project, "index": INDEX_NAME}
    out = []
    for ack in BY_KIND.get(kind, []):
        if ack.when is not None and not ack.when(cfg):
            continue
        matched = [n for n in nodes if fnmatchcase(_relative(stack, n), ack.path)]
        if not matched:
            raise ValueError(f"{stack.stack_name}: nag acknowledgement path {ack.path!r} matches no construct")
        finding = P + ack.finding.format(**fmt)
        out.extend((n, Acknowledgment(id=finding, reason=ack.reason)) for n in matched)
    return out


def apply_suppressions(stack: Stack, cfg: AppConfig) -> None:
    for node, ack in acknowledgements(stack, cfg):
        Validations.of(node).acknowledge(ack)
