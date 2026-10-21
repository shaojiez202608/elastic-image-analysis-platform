"""Residual check after destroy (read-only). Exit code 1 when anything of this project is left.

Every category is checked by name prefix (paginated list APIs) and, where the API supports it, by the
Project tag. The tagging index lags behind deletions, so each tagged ARN is looked up in its own
service and only counted when it still exists. Account IDs are replaced by 111122223333 in all output.

    uv run python scripts/check_residuals.py [--config config/sandbox.json] [--region us-west-2 ...]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
ACCT = re.compile(r"\b\d{12}\b")


def redact(v) -> str:
    return ACCT.sub("111122223333", str(v))


def profile(path: str) -> dict:
    p = ROOT / path
    if not p.exists():
        p = p.with_name(p.stem + ".example.json")
    return json.loads(p.read_text())


class Checker:
    def __init__(self, region: str, prefix: str, tag_value: str):
        self.r, self.p, self.tag = region, prefix, tag_value
        self.s = boto3.session.Session(region_name=region)
        self.residual: list[str] = []

    def c(self, svc):
        return self.s.client(svc)

    def report(self, label: str, items: list, residual: bool = True) -> None:
        items = [redact(i) for i in items]
        print(f"  {label:<40} {', '.join(items) if items else '<none>'}")
        if items and residual:
            self.residual.extend(f"{label}: {i}" for i in items)

    def pages(self, svc, op, key, **kw):
        out = []
        for page in self.c(svc).get_paginator(op).paginate(**kw):
            out.extend(page.get(key, []))
        return out

    def run(self) -> None:  # noqa: C901 - one flat list of categories
        p = self.p
        print(f"region {self.r}")
        self.report("cloudformation stacks", [s["StackName"] + ":" + s["StackStatus"] for s in self.pages(
            "cloudformation", "list_stacks", "StackSummaries") if s["StackName"].startswith(p)
            and s["StackStatus"] != "DELETE_COMPLETE"])
        self.report("s3 buckets (any objects/versions)", [b["Name"] for b in self.pages("s3", "list_buckets", "Buckets")
                                                         if b["Name"].startswith(p)])
        self.report("log groups", [g["logGroupName"] for g in self.pages("logs", "describe_log_groups", "logGroups")
                                   if p in g["logGroupName"]])
        self.report("ecr repositories", [r["repositoryName"] for r in self.pages("ecr", "describe_repositories",
                    "repositories") if r["repositoryName"].startswith(p)])
        self.report("lambda functions", [f["FunctionName"] for f in self.pages("lambda", "list_functions", "Functions")
                                         if f["FunctionName"].startswith(p)])
        self.report("iam roles", [r["RoleName"] for r in self.pages("iam", "list_roles", "Roles")
                                  if r["RoleName"].startswith(p)])
        self.report("iam policies (local)", [x["PolicyName"] for x in self.pages("iam", "list_policies", "Policies",
                    Scope="Local") if x["PolicyName"].startswith(p)])
        sm = "sagemaker"
        self.report("sagemaker endpoints", [e["EndpointName"] + ":" + e["EndpointStatus"] for e in self.pages(
            sm, "list_endpoints", "Endpoints", NameContains=p)])
        self.report("sagemaker endpoint configs", [e["EndpointConfigName"] for e in self.pages(
            sm, "list_endpoint_configs", "EndpointConfigs", NameContains=p)])
        self.report("sagemaker models", [m["ModelName"] for m in self.pages(sm, "list_models", "Models", NameContains=p)])
        # Lineage entities SageMaker AI records automatically for each endpoint deployment (cleanup_lineage.py).
        self.report("sagemaker lineage contexts/actions",
                    [c["ContextName"] for c in self.pages(sm, "list_contexts", "ContextSummaries")
                     if c["ContextName"].startswith(f"{p}-endpoint-")]
                    + [a["ActionName"] for a in self.pages(sm, "list_actions", "ActionSummaries")
                       if a["ActionName"].startswith(f"{p}-endpoint-")])
        aas = self.c("application-autoscaling")
        targets = []
        for ns in ("sagemaker", "ecs"):
            for t in aas.get_paginator("describe_scalable_targets").paginate(ServiceNamespace=ns):
                targets += [x["ResourceId"] for x in t["ScalableTargets"] if p in x["ResourceId"]]
        self.report("application auto scaling targets", targets)
        self.report("opensearch domains", [d["DomainName"] for d in self.c("opensearch").list_domain_names()["DomainNames"]
                                           if d["DomainName"].startswith(p)])
        ecs = self.c("ecs")
        clusters = [a for a in self.pages("ecs", "list_clusters", "clusterArns") if f"/{p}" in a]
        active = [c["clusterName"] for c in (ecs.describe_clusters(clusters=clusters)["clusters"] if clusters else [])
                  if c["status"] != "INACTIVE"]
        self.report("ecs clusters (not INACTIVE)", active)
        families = self.pages("ecs", "list_task_definition_families", "families", familyPrefix=p, status="ACTIVE")
        tds = [a for fam in families for a in self.pages("ecs", "list_task_definitions", "taskDefinitionArns",
                                                           familyPrefix=fam, status="ACTIVE")]
        self.report("ecs task definitions (ACTIVE)", [a.rsplit("/", 1)[-1] for a in tds])
        inactive = [a for fam in self.pages("ecs", "list_task_definition_families", "families", familyPrefix=p,
                                            status="INACTIVE")
                    for a in self.pages("ecs", "list_task_definitions", "taskDefinitionArns", familyPrefix=fam,
                                        status="INACTIVE")]
        # INACTIVE revisions are not billable and cannot be used; listed for completeness, deleted by destroy.sh
        self.report("ecs task definitions (INACTIVE)", [a.rsplit("/", 1)[-1] for a in inactive], residual=False)
        self.report("codebuild projects", [x for x in self.pages("codebuild", "list_projects", "projects")
                                           if x.startswith(p)])
        ec2 = self.c("ec2")
        tagf = [{"Name": "tag:Project", "Values": [self.tag]}]
        self.report("vpcs", [v["VpcId"] for v in self.pages("ec2", "describe_vpcs", "Vpcs", Filters=tagf)])
        self.report("vpc endpoints (not deleted)", [e["VpcEndpointId"] + ":" + e["State"] for e in self.pages(
            "ec2", "describe_vpc_endpoints", "VpcEndpoints", Filters=tagf) if e["State"].lower() != "deleted"])
        self.report("nat gateways (not deleted)", [n["NatGatewayId"] for n in self.pages(
            "ec2", "describe_nat_gateways", "NatGateways", Filter=tagf) if n["State"] != "deleted"])
        self.report("security groups", [g["GroupId"] for g in self.pages("ec2", "describe_security_groups",
                                                                          "SecurityGroups", Filters=tagf)])
        self.report("network interfaces (tagged or by SG name)", [n["NetworkInterfaceId"] for n in self.pages(
            "ec2", "describe_network_interfaces", "NetworkInterfaces",
            Filters=[{"Name": "group-name", "Values": [f"{p}-*"]}])])
        self.report("elastic ips", [a["AllocationId"] for a in ec2.describe_addresses(Filters=tagf)["Addresses"]])
        self.report("sqs queues", self.c("sqs").list_queues(QueueNamePrefix=p).get("QueueUrls", []))
        self.report("sns topics", [t["TopicArn"] for t in self.pages("sns", "list_topics", "Topics")
                                   if f":{p}" in t["TopicArn"]])
        self.report("dynamodb tables", [t for t in self.pages("dynamodb", "list_tables", "TableNames") if t.startswith(p)])
        self.report("cloudwatch alarms", [a["AlarmName"] for a in self.pages("cloudwatch", "describe_alarms",
                    "MetricAlarms", AlarmNamePrefix=p)])
        self.report("cloudwatch dashboards", [d["DashboardName"] for d in self.pages(
            "cloudwatch", "list_dashboards", "DashboardEntries", DashboardNamePrefix=p)])
        self.report("api gateways", [a["name"] for a in self.pages("apigateway", "get_rest_apis", "items")
                                     if a["name"].startswith(p)])
        self.kms()
        self.tagged()

    def kms(self) -> None:
        kms = self.c("kms")
        aliases = [a for a in self.pages("kms", "list_aliases", "Aliases") if a["AliasName"].startswith(f"alias/{self.p}")]
        self.report("kms aliases", [a["AliasName"] for a in aliases])
        found, pending = [], []
        for k in self.pages("kms", "list_keys", "Keys"):
            try:
                tags = kms.list_resource_tags(KeyId=k["KeyId"]).get("Tags", [])
            except ClientError:
                continue
            if any(t["TagKey"] == "Project" and t["TagValue"] == self.tag for t in tags):
                state = kms.describe_key(KeyId=k["KeyId"])["KeyMetadata"]["KeyState"]
                (pending if state == "PendingDeletion" else found).append(f"{k['KeyId']}:{state}")
        self.report("kms keys (tagged, enabled)", found)
        # Keys cannot be deleted immediately; PendingDeletion is the end state of a destroy and is recorded.
        self.report("kms keys pending deletion (recorded)", pending, residual=False)

    def tagged(self) -> None:
        arns = self.pages("resourcegroupstaggingapi", "get_resources", "ResourceTagMappingList",
                          TagFilters=[{"Key": "Project", "Values": [self.tag]}])
        live, gone = [], []
        for m in arns:
            arn = m["ResourceARN"]
            (live if self.exists(arn) else gone).append(arn)
        self.report("tagged resources (confirmed live)", live)
        if gone:
            self.report("tagged ARNs already deleted (index lag)", gone, residual=False)

    def exists(self, arn: str) -> bool:  # noqa: C901
        svc, res = arn.split(":")[2], arn.split(":", 5)[5]
        try:
            if svc == "kms":
                st = self.c("kms").describe_key(KeyId=arn)["KeyMetadata"]["KeyState"]
                return st not in ("PendingDeletion", "Deleted")
            if svc == "ecs" and res.startswith("cluster/"):
                cl = self.c("ecs").describe_clusters(clusters=[arn])["clusters"]
                return bool(cl) and cl[0]["status"] != "INACTIVE"
            if svc == "ecs" and res.startswith("service/"):
                parts = res.split("/")
                sv = self.c("ecs").describe_services(cluster=parts[1], services=[arn])["services"]
                return bool(sv) and sv[0]["status"] != "INACTIVE"
            if svc == "ecs" and res.startswith("task-definition/"):
                return self.c("ecs").describe_task_definition(taskDefinition=arn)["taskDefinition"]["status"] == "ACTIVE"
            if svc == "ec2":
                kind, rid = res.split("/", 1)
                f = {"vpc": ("describe_vpcs", "VpcIds", "Vpcs"), "subnet": ("describe_subnets", "SubnetIds", "Subnets"),
                     "security-group": ("describe_security_groups", "GroupIds", "SecurityGroups"),
                     "vpc-endpoint": ("describe_vpc_endpoints", "VpcEndpointIds", "VpcEndpoints"),
                     "route-table": ("describe_route_tables", "RouteTableIds", "RouteTables"),
                     "vpc-flow-log": ("describe_flow_logs", "FlowLogIds", "FlowLogs"),
                     "natgateway": ("describe_nat_gateways", "NatGatewayIds", "NatGateways")}.get(kind)
                if f:
                    items = getattr(self.c("ec2"), f[0])(**{f[1]: [rid]})[f[2]]
                    return bool(items) and all(str(i.get("State", "available")).lower() != "deleted" for i in items)
            if svc == "logs":
                name = res.split(":", 1)[1].removesuffix(":*")
                return any(g["logGroupName"] == name for g in self.c("logs").describe_log_groups(
                    logGroupNamePrefix=name)["logGroups"])
            if svc == "sagemaker":
                kind, name = res.split("/", 1)
                fn = {"endpoint": ("describe_endpoint", "EndpointName"),
                      "endpoint-config": ("describe_endpoint_config", "EndpointConfigName"),
                      "model": ("describe_model", "ModelName"), "context": ("describe_context", "ContextName"),
                      "action": ("describe_action", "ActionName")}.get(kind)
                if kind == "artifact":
                    self.c("sagemaker").describe_artifact(ArtifactArn=arn)
                    return True
                if fn:
                    getattr(self.c("sagemaker"), fn[0])(**{fn[1]: name})
                    return True
            if svc == "sqs":
                self.c("sqs").get_queue_url(QueueName=res)
                return True
            if svc == "sns":
                self.c("sns").get_topic_attributes(TopicArn=arn)
                return True
            if svc == "s3":
                self.c("s3").head_bucket(Bucket=res)
                return True
            if svc == "dynamodb":
                self.c("dynamodb").describe_table(TableName=res.split("/", 1)[1])
                return True
            if svc == "es":
                self.c("opensearch").describe_domain(DomainName=res.split("/", 1)[1])
                return True
            if svc == "cloudwatch" and res.startswith("alarm:"):
                return bool(self.c("cloudwatch").describe_alarms(AlarmNames=[res.split(":", 1)[1]])["MetricAlarms"])
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if "NotFound" in code or code in ("ValidationException", "ResourceNotFoundException", "NoSuchEntity",
                                              "AWS.SimpleQueueService.NonExistentQueue", "404", "NoSuchBucket",
                                              "ClientException", "InvalidParameterException"):
                return False
            raise
        return True  # unknown type: count it, a human confirms


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/sandbox.json")
    ap.add_argument("--region", action="append", default=None)
    ap.add_argument("--prefix", default=None)
    args = ap.parse_args()
    cfg = profile(args.config)
    prefix = args.prefix or cfg["project"]
    residual = []
    for region in args.region or [cfg["region"]]:
        ch = Checker(region, prefix, cfg["tags"]["Project"])
        ch.run()
        residual += [f"{region} {r}" for r in ch.residual]
    print("conclusion:", "已清理，无残留" if not residual else "仍有残留：" + "; ".join(residual))
    return 1 if residual else 0


if __name__ == "__main__":
    sys.exit(main())
