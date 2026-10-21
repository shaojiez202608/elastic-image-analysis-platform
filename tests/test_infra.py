"""CDK assertions for both profiles, cdk-nag, and configuration-driven names."""

import json
import re
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from imaging_infra.app_builder import build_app
from imaging_infra.config import ConfigError, parse

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = json.loads((ROOT / "cdk.json").read_text())["context"]


def synth(profile="sandbox", nag=True, raw=None, tmp_path=None, **kw):
    app = cdk.App(context={k: v for k, v in CONTEXT.items() if k != "config"})
    path = ROOT / "config" / f"{profile}.example.json"
    if raw is not None:
        path = tmp_path / "cfg.json"
        path.write_text(json.dumps(raw))
    stacks = build_app(app, config_path=str(path), nag=nag, synth_check=(profile == "handover"), **kw)
    return app, {name: Template.from_stack(s) for name, s in stacks.items()}


@pytest.fixture(scope="module")
def sandbox():
    return synth()[1]


@pytest.fixture(scope="module")
def handover():
    return synth("handover")[1]


def res(templates, rtype):
    return [r for t in templates.values() for r in t.find_resources(rtype).values()]


def test_at_least_five_stacks(sandbox):
    assert {"proto-imaging-storage", "proto-imaging-compute", "proto-imaging-inference", "proto-imaging-search",
            "proto-imaging-observability", "proto-imaging-network"} == set(sandbox)


@pytest.mark.parametrize("profile", ["sandbox", "handover"])
def test_cdk_nag_clean(profile):
    app, _ = synth(profile)
    app.synth()  # raises on any unacknowledged AwsSolutions finding


def test_nag_acknowledgements_follow_the_configured_prefix(tmp_path):
    raw = json.loads((ROOT / "config/sandbox.example.json").read_text())
    raw["project"] = "proto-imgalt"
    raw["region"] = "us-west-2"
    raw["network"]["availability_zones"] = ["us-west-2a", "us-west-2b"]
    raw["search"]["data_nodes"] = 2
    app, templates = synth(raw=raw, tmp_path=tmp_path)
    app.synth()
    assert all(n.startswith("proto-imgalt-") for n in templates)


def test_no_open_ingress_and_no_public_subnets(sandbox):
    for sg in res(sandbox, "AWS::EC2::SecurityGroup"):
        for rule in sg["Properties"].get("SecurityGroupIngress", []):
            assert rule.get("CidrIp") not in ("0.0.0.0/0",) and rule.get("CidrIpv6") != "::/0"
    for rule in res(sandbox, "AWS::EC2::SecurityGroupIngress"):
        assert "CidrIp" not in rule["Properties"]
    assert not res(sandbox, "AWS::EC2::InternetGateway") and not res(sandbox, "AWS::EC2::NatGateway")
    for sn in res(sandbox, "AWS::EC2::Subnet"):
        assert sn["Properties"].get("MapPublicIpOnLaunch") in (None, False)


def test_buckets_private_encrypted_tls_only(sandbox):
    t = sandbox["proto-imaging-storage"]
    buckets = t.find_resources("AWS::S3::Bucket")
    assert len(buckets) == 3
    for b in buckets.values():
        p = b["Properties"]["PublicAccessBlockConfiguration"]
        assert all(p[k] for k in ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets"))
        assert b["Properties"]["BucketEncryption"]
    policies = t.find_resources("AWS::S3::BucketPolicy")
    assert len(policies) == 3
    for pol in policies.values():
        sts = pol["Properties"]["PolicyDocument"]["Statement"]
        assert any(s["Effect"] == "Deny" and s.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport") == "false"
                   for s in sts)


def test_queues_have_dlqs_and_kms(sandbox):
    queues = sandbox["proto-imaging-storage"].find_resources("AWS::SQS::Queue")
    main = {v["Properties"]["QueueName"]: v["Properties"] for v in queues.values()}
    assert main["proto-imaging-ingest"]["RedrivePolicy"]["maxReceiveCount"] == 5
    assert "RedrivePolicy" in main["proto-imaging-results"]
    assert all("KmsMasterKeyId" in p for p in main.values())


def test_async_endpoint_scaling(sandbox):
    t = sandbox["proto-imaging-inference"]
    t.has_resource_properties("AWS::ApplicationAutoScaling::ScalableTarget",
                              {"MinCapacity": 0, "MaxCapacity": 2, "ScalableDimension": "sagemaker:variant:DesiredInstanceCount"})
    t.has_resource_properties("AWS::ApplicationAutoScaling::ScalingPolicy", {
        "PolicyType": "TargetTrackingScaling",
        "TargetTrackingScalingPolicyConfiguration": Match.object_like({
            "TargetValue": 4, "CustomizedMetricSpecification": Match.object_like(
                {"MetricName": "ApproximateBacklogSizePerInstance", "Namespace": "AWS/SageMaker"})})})
    t.has_resource_properties("AWS::ApplicationAutoScaling::ScalingPolicy", {"PolicyType": "StepScaling"})
    t.has_resource_properties("AWS::CloudWatch::Alarm", {"MetricName": "HasBacklogWithoutCapacity", "Threshold": 1,
                                                         "EvaluationPeriods": 2, "DatapointsToAlarm": 2})
    epc = list(t.find_resources("AWS::SageMaker::EndpointConfig").values())[0]["Properties"]
    out = epc["AsyncInferenceConfig"]["OutputConfig"]
    assert "SuccessTopic" in out["NotificationConfig"] and "ErrorTopic" in out["NotificationConfig"]
    assert epc["AsyncInferenceConfig"]["ClientConfig"]["MaxConcurrentInvocationsPerInstance"] == 2
    model = list(t.find_resources("AWS::SageMaker::Model").values())[0]["Properties"]
    assert model["EnableNetworkIsolation"] is True
    assert model["PrimaryContainer"]["Environment"]["FAULT_INJECTION"] == "true"


def test_handover_disables_fault_injection_and_retains_data(handover):
    model = list(handover["proto-imaging-inference"].find_resources("AWS::SageMaker::Model").values())[0]
    assert model["Properties"]["PrimaryContainer"]["Environment"]["FAULT_INJECTION"] == "false"
    for b in handover["proto-imaging-storage"].find_resources("AWS::S3::Bucket").values():
        assert b["DeletionPolicy"] == "Retain"
    table = list(handover["proto-imaging-storage"].find_resources("AWS::DynamoDB::Table").values())[0]
    assert table["Properties"]["DeletionProtectionEnabled"] is True
    api = list(handover["proto-imaging-search"].find_resources("AWS::ApiGateway::RestApi").values())[0]
    assert api["Properties"]["EndpointConfiguration"]["Types"] == ["PRIVATE"]


def test_ecs_services_are_arm64_and_scale_on_queue(sandbox):
    t = sandbox["proto-imaging-compute"]
    for td in t.find_resources("AWS::ECS::TaskDefinition").values():
        assert td["Properties"]["RuntimePlatform"]["CpuArchitecture"] == "ARM64"
        c = td["Properties"]["ContainerDefinitions"][0]
        assert c["ReadonlyRootFilesystem"] is True
    targets = t.find_resources("AWS::ApplicationAutoScaling::ScalableTarget")
    assert {v["Properties"]["MinCapacity"] for v in targets.values()} == {0}
    alarms = t.find_resources("AWS::CloudWatch::Alarm")
    names = sorted(a["Properties"]["AlarmName"] for a in alarms.values())
    assert names == ["proto-imaging-dispatcher-backlog-empty", "proto-imaging-dispatcher-backlog-high",
                     "proto-imaging-indexer-backlog-empty", "proto-imaging-indexer-backlog-high"]
    for a in alarms.values():
        exprs = [m.get("Expression") for m in a["Properties"]["Metrics"]]
        assert "visible + inflight" in exprs
    for svc in t.find_resources("AWS::ECS::Service").values():
        assert svc["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]["AssignPublicIp"] == "DISABLED"


def test_step_bounds_follow_config(sandbox):
    pols = [p["Properties"] for p in sandbox["proto-imaging-compute"].find_resources(
        "AWS::ApplicationAutoScaling::ScalingPolicy").values()]
    outs = [p for p in pols if p["StepScalingPolicyConfiguration"]["StepAdjustments"][0]["ScalingAdjustment"] > 0]
    steps = sorted((s["MetricIntervalLowerBound"], s["ScalingAdjustment"])
                   for p in outs if "Dispatcher" in json.dumps(p["ScalingTargetId"])
                   for s in p["StepScalingPolicyConfiguration"]["StepAdjustments"])
    assert (0, 1) in steps and (39, 2) in steps


def test_opensearch_vpc_fgac_encrypted(sandbox):
    d = list(sandbox["proto-imaging-search"].find_resources("AWS::OpenSearchService::Domain").values())[0]["Properties"]
    assert d["VPCOptions"]["SubnetIds"] and d["NodeToNodeEncryptionOptions"]["Enabled"]
    assert d["EncryptionAtRestOptions"]["Enabled"] and d["DomainEndpointOptions"]["EnforceHTTPS"]
    adv = d["AdvancedSecurityOptions"]
    assert adv["Enabled"] and "MasterUserARN" in adv["MasterUserOptions"] and not adv.get("InternalUserDatabaseEnabled")
    # CDK applies the access policy through a Custom::OpenSearchAccessPolicy (UpdateDomainConfig) resource.
    cr = list(sandbox["proto-imaging-search"].find_resources("Custom::OpenSearchAccessPolicy").values())[0]
    create = json.dumps(cr["Properties"]["Create"])
    assert "Deny" in create and "es:ESHttp*" in create
    assert "_dashboards" in create


def test_api_methods_use_iam_and_resource_policy(sandbox):
    t = sandbox["proto-imaging-search"]
    methods = t.find_resources("AWS::ApiGateway::Method")
    assert methods and all(m["Properties"]["AuthorizationType"] == "AWS_IAM" for m in methods.values())
    api = list(t.find_resources("AWS::ApiGateway::RestApi").values())[0]["Properties"]
    st = api["Policy"]["Statement"][0]
    assert "aws:PrincipalAccount" in st["Condition"]["StringEquals"]
    assert not t.find_resources("AWS::ApiGateway::Account")  # account-level setting untouched


def test_lambdas_are_arm64_latest_python(sandbox):
    for fn in res(sandbox, "AWS::Lambda::Function"):
        if fn["Properties"].get("Handler", "").startswith(("api.", "search_bootstrap.")):
            assert fn["Properties"]["Architectures"] == ["arm64"] and fn["Properties"]["Runtime"] == "python3.14"


def test_trust_policies_have_confused_deputy_conditions(sandbox):
    checked = 0
    for t in sandbox.values():
        for role in t.find_resources("AWS::IAM::Role").values():
            for st in role["Properties"]["AssumeRolePolicyDocument"]["Statement"]:
                svc = st["Principal"].get("Service")
                if svc in ("sagemaker.amazonaws.com", "codebuild.amazonaws.com", "vpc-flow-logs.amazonaws.com") or (
                        svc == "ecs-tasks.amazonaws.com" and "task" in json.dumps(role["Properties"].get("RoleName", ""))
                        and "exec" not in json.dumps(role["Properties"].get("RoleName", ""))):
                    assert "aws:SourceAccount" in st["Condition"]["StringEquals"]
                    assert "aws:SourceArn" in st["Condition"]["ArnLike"]
                    checked += 1
    assert checked >= 5


def test_log_groups_have_retention(sandbox):
    groups = res(sandbox, "AWS::Logs::LogGroup")
    assert groups and all("RetentionInDays" in g["Properties"] for g in groups)


def test_key_policies_service_principals_conditioned(sandbox):
    for key in res(sandbox, "AWS::KMS::Key"):
        for st in key["Properties"]["KeyPolicy"]["Statement"]:
            if "Service" in st["Principal"]:
                assert st.get("Condition")


def test_tags_and_no_account_ids(sandbox):
    for name, t in sandbox.items():
        body = json.dumps(t.to_json())
        assert not re.search(r"\b\d{12}\b", body.replace("111122223333", "")), name
    vpc = list(sandbox["proto-imaging-network"].find_resources("AWS::EC2::VPC").values())[0]
    tags = {t["Key"]: t["Value"] for t in vpc["Properties"]["Tags"]}
    assert tags["Project"] == "elastic-imaging" and tags["Teardown"] == "after-test" and tags["Purpose"] == "prototype"


def test_endpoint_has_dashboard_and_alarms(sandbox):
    t = sandbox["proto-imaging-observability"]
    assert len(t.find_resources("AWS::CloudWatch::Dashboard")) == 1
    names = sorted(a["Properties"]["AlarmName"] for a in t.find_resources("AWS::CloudWatch::Alarm").values())
    assert "proto-imaging-ingest-dlq-not-empty" in names and "proto-imaging-endpoint-backlog-without-capacity" in names


def base():
    return json.loads((ROOT / "config/sandbox.example.json").read_text())


@pytest.mark.parametrize("mutate,msg", [
    (lambda r: r.update(project="Bad_Name"), "project"),
    (lambda r: r.update(project="imaging-x"), "proto-"),
    (lambda r: r["network"].update(availability_zones=["us-west-2a", "us-west-2b"]), "availability_zones"),
    (lambda r: r["network"].update(vpc_cidr="8.8.0.0/16"), "vpc_cidr"),
    (lambda r: r.update(removal_policy="maybe"), "removal_policy"),
    (lambda r: r["tags"].pop("Teardown"), "tags"),
    (lambda r: r["inference"].update(instance_type=""), "instance_type is empty"),
    (lambda r: r["inference"].update(accelerator="gpu"), "CPU-only"),
    (lambda r: r["inference"].update(scale_from_zero=False), "scale_from_zero"),
    (lambda r: r["inference"].update(fault_sleep_seconds=5), "fault_sleep_seconds"),
    (lambda r: r["inference"].update(invocation_timeout_seconds=4000), "invocation_timeout_seconds"),
    (lambda r: r["workers"].update(memory_mib=8192), "memory_mib"),
    (lambda r: r["workers"]["dispatcher"].update(steps=[{"lower": 10, "change": 1}, {"lower": 5, "change": 1}]), "ascend"),
    (lambda r: r["upload"].update(categories=["batch"]), "general"),
    (lambda r: r["image_limits"].update(normalize_max_side=100), "normalize_max_side"),
    (lambda r: r["api"].update(endpoint_type="EDGE"), "endpoint_type"),
    (lambda r: r["observability"].update(alarm_email="not-an-email"), "alarm_email"),
])
def test_config_validation(mutate, msg):
    raw = base()
    mutate(raw)
    with pytest.raises(ConfigError) as e:
        parse(raw)
    assert msg in str(e.value)
