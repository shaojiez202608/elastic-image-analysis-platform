"""Search and query API: VPC-only OpenSearch domain with fine-grained access control (IAM master role),
a bootstrap custom resource (index mapping + least-privilege FGAC roles), and the IAM-authorised
REST API (upload / status / search) backed by an ARM64 Lambda function in the VPC.

Access model (one of the configurations recommended in the OpenSearch Service developer guide):
domain access policy allows es:ESHttp* for principals of this account and denies the Dashboards
path; FGAC then authorises each request by the caller's IAM role (backend-role mapping).
https://docs.aws.amazon.com/opensearch-service/latest/developerguide/fgac.html#fgac-recommendations
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aws_cdk import Aws, CustomResource, Duration, Stack
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_opensearchservice as opensearch
from aws_cdk import custom_resources as cr
from constructs import Construct

from imaging.documents import INDEX_NAME

from .common import allow, log_retention, removal, s3_objects
from .config import AppConfig
from .network_stack import NetworkStack
from .storage_stack import StorageStack

ROOT = Path(__file__).resolve().parents[2]
LAMBDA_EXCLUDES = ["**/__pycache__", "imaging/adapters/local.py", "imaging/local_platform.py", "imaging/synthetic.py",
                   "imaging/validation.py", "imaging/pipeline.py", "imaging/runner.py", "imaging/worker_main.py", "imaging/sim.py"]


def role_arn(name: str) -> str:
    return f"arn:{Aws.PARTITION}:iam::{Aws.ACCOUNT_ID}:role/{name}"


def lambda_code() -> lambda_.Code:
    return lambda_.Code.from_asset(str(ROOT / "src"), exclude=LAMBDA_EXCLUDES)


class SearchStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: AppConfig, network: NetworkStack, storage: StorageStack,
                 **kw) -> None:
        super().__init__(scope, cid, **kw)
        rp = removal(cfg)
        se = cfg.search
        self.indexer_role_name = cfg.name("indexer-task")
        self.api_role_name = cfg.name("api")
        domain_name = cfg.name("search")
        domain_arn = f"arn:{Aws.PARTITION}:es:{Aws.REGION}:{Aws.ACCOUNT_ID}:domain/{domain_name}"

        master = iam.Role(self, "SearchMasterRole", role_name=cfg.name("search-master"),
                          assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                          description="FGAC master user; used only by the search bootstrap custom resource")
        master.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name(
            "service-role/AWSLambdaVPCAccessExecutionRole"))

        app_logs = logs.LogGroup(self, "SearchAppLogs", log_group_name=f"/{cfg.project}/opensearch-app",
                                 retention=log_retention(cfg), removal_policy=rp)
        subnets = network.vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED).subnets
        zone_aware = se.data_nodes > 1
        self.domain = opensearch.Domain(
            self, "Domain", domain_name=domain_name,
            version=opensearch.EngineVersion.open_search(se.engine_version.removeprefix("OpenSearch_")),
            vpc=network.vpc, vpc_subnets=[ec2.SubnetSelection(subnets=subnets[: 2 if zone_aware else 1])],
            security_groups=[network.search_sg],
            capacity=opensearch.CapacityConfig(data_nodes=se.data_nodes, data_node_instance_type=se.instance_type,
                                               multi_az_with_standby_enabled=False),
            zone_awareness=opensearch.ZoneAwarenessConfig(enabled=True, availability_zone_count=2)
            if zone_aware else opensearch.ZoneAwarenessConfig(enabled=False),
            ebs=opensearch.EbsOptions(volume_size=se.volume_gib, volume_type=ec2.EbsDeviceVolumeType.GP3),
            encryption_at_rest=opensearch.EncryptionAtRestOptions(enabled=True, kms_key=storage.key),
            node_to_node_encryption=True, enforce_https=True, tls_security_policy=opensearch.TLSSecurityPolicy.TLS_1_2,
            fine_grained_access_control=opensearch.AdvancedSecurityOptions(master_user_arn=master.role_arn),
            logging=opensearch.LoggingOptions(app_log_enabled=True, app_log_group=app_logs),
            removal_policy=rp,
            access_policies=[
                iam.PolicyStatement(effect=iam.Effect.ALLOW, principals=[iam.AccountRootPrincipal()],
                                    actions=["es:ESHttp*"], resources=[f"{domain_arn}/*"]),
                iam.PolicyStatement(effect=iam.Effect.DENY, principals=[iam.AccountRootPrincipal()],
                                    actions=["es:ESHttp*"], resources=[f"{domain_arn}/_dashboards*"]),
            ],
        )
        self.domain_arn = domain_arn
        master.add_to_policy(iam.PolicyStatement(actions=["es:ESHttpPut", "es:ESHttpGet"],
                                                 resources=[f"{domain_arn}/*"]))

        boot_fn = lambda_.Function(
            self, "SearchBootstrapFn", function_name=cfg.name("search-bootstrap"), runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64, handler="search_bootstrap.handler.on_event", code=lambda_code(),
            role=master, timeout=Duration.minutes(2), memory_size=256, vpc=network.vpc, vpc_subnets=network.subnets,
            security_groups=[network.bootstrap_sg],
            log_group=logs.LogGroup(self, "SearchBootstrapLogs", log_group_name=f"/aws/lambda/{cfg.name('search-bootstrap')}",
                                    retention=log_retention(cfg), removal_policy=rp))
        provider = cr.Provider(self, "SearchBootstrapProvider", on_event_handler=boot_fn,
                               log_group=logs.LogGroup(self, "SearchBootstrapProviderLogs", retention=log_retention(cfg),
                                                       removal_policy=rp))
        from imaging.documents import MAPPING  # noqa: PLC0415 - shared with the runtime code
        from search_bootstrap.handler import READER, WRITER  # noqa: PLC0415
        version = hashlib.sha256(json.dumps([MAPPING, READER, WRITER], sort_keys=True).encode()).hexdigest()[:12]
        boot = CustomResource(self, "SearchBootstrap", service_token=provider.service_token, properties={
            "Endpoint": self.domain.domain_endpoint, "WriterRoleArn": role_arn(self.indexer_role_name),
            "ReaderRoleArn": role_arn(self.api_role_name), "Version": version})
        boot.node.add_dependency(self.domain)

        # --- query/upload API -------------------------------------------------------------------
        api_role = iam.Role(self, "ApiRole", role_name=self.api_role_name,
                            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                            description="Query/upload API Lambda")
        api_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name(
            "service-role/AWSLambdaVPCAccessExecutionRole"))
        storage.jobs.grant(api_role, "dynamodb:GetItem", "dynamodb:PutItem")
        # The presigned POST is signed with this role's credentials: it needs PutObject on uploads/ and
        # GenerateDataKey for the bucket's default SSE-KMS key.
        allow(api_role, "PresignUploads", ["s3:PutObject"], [s3_objects(cfg, "images", "uploads/*")])
        storage.key.grant(api_role, "kms:GenerateDataKey", "kms:Decrypt")
        api_role.add_to_policy(iam.PolicyStatement(actions=["es:ESHttpGet", "es:ESHttpPost"],
                                                   resources=[f"{domain_arn}/{INDEX_NAME}/*"]))
        fn_name = cfg.name("api")
        env = {
            "IMAGES_BUCKET": storage.images.bucket_name, "JOBS_TABLE": storage.jobs.table_name,
            "SEARCH_ENDPOINT": self.domain.domain_endpoint, "MAX_UPLOAD_BYTES": str(cfg.max_upload_bytes),
            "PRESIGN_EXPIRES_SECONDS": str(cfg.presign_expires_seconds), "CATEGORIES": ",".join(cfg.categories),
            "FAULT_INJECTION": "true" if cfg.inference.fault_injection else "false",
            "METRICS_NAMESPACE": cfg.metrics_namespace, "TOP_K": str(cfg.inference.top_k),
        }
        self.api_fn = lambda_.Function(
            self, "ApiFn", function_name=fn_name, runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64, handler="api.handler.handler", code=lambda_code(), role=api_role,
            timeout=Duration.seconds(15), memory_size=256, vpc=network.vpc, vpc_subnets=network.subnets,
            security_groups=[network.api_sg], environment=env, reserved_concurrent_executions=None,
            log_group=logs.LogGroup(self, "ApiLogs", log_group_name=f"/aws/lambda/{fn_name}",
                                    retention=log_retention(cfg), removal_policy=rp))

        access_logs = logs.LogGroup(self, "ApiAccessLogs", log_group_name=f"/{cfg.project}/api-access",
                                    retention=log_retention(cfg), removal_policy=rp)
        private = cfg.api_endpoint_type == "PRIVATE"
        conditions: dict = {"StringEquals": {"aws:PrincipalAccount": Aws.ACCOUNT_ID}}
        if private:
            conditions["StringEquals"]["aws:SourceVpce"] = network.endpoints["ExecuteApi"].vpc_endpoint_id
        policy = iam.PolicyDocument(statements=[
            iam.PolicyStatement(effect=iam.Effect.ALLOW, principals=[iam.AnyPrincipal()], actions=["execute-api:Invoke"],
                                resources=["execute-api:/*"], conditions=conditions)])
        self.api = apigw.RestApi(
            self, "Api", rest_api_name=cfg.name("api"), cloud_watch_role=False, policy=policy,
            endpoint_configuration=apigw.EndpointConfiguration(
                types=[apigw.EndpointType.PRIVATE if private else apigw.EndpointType.REGIONAL],
                vpc_endpoints=[network.endpoints["ExecuteApi"]] if private else None),
            deploy_options=apigw.StageOptions(
                stage_name="v1", throttling_rate_limit=cfg.api_throttle_rate,
                throttling_burst_limit=cfg.api_throttle_burst, logging_level=apigw.MethodLoggingLevel.ERROR,
                data_trace_enabled=False, metrics_enabled=True,
                access_log_destination=apigw.LogGroupLogDestination(access_logs),
                access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                    caller=True, http_method=True, ip=True, protocol=True, request_time=True, resource_path=True,
                    response_length=True, status=True, user=True)),
            default_method_options=apigw.MethodOptions(authorization_type=apigw.AuthorizationType.IAM),
            disable_execute_api_endpoint=False)
        integ = apigw.LambdaIntegration(self.api_fn, proxy=True)
        validator = self.api.add_request_validator("Validator", validate_request_body=True,
                                                   validate_request_parameters=True)
        upload_model = self.api.add_model("UploadRequest", content_type="application/json", schema=apigw.JsonSchema(
            type=apigw.JsonSchemaType.OBJECT, required=["content_type", "size_bytes"], additional_properties=False,
            properties={
                "content_type": apigw.JsonSchema(type=apigw.JsonSchemaType.STRING, enum=["image/jpeg", "image/png"]),
                "size_bytes": apigw.JsonSchema(type=apigw.JsonSchemaType.INTEGER, minimum=1,
                                               maximum=cfg.max_upload_bytes),
                "category": apigw.JsonSchema(type=apigw.JsonSchemaType.STRING, enum=list(cfg.categories)),
                "tags": apigw.JsonSchema(type=apigw.JsonSchemaType.ARRAY, max_items=5,
                                         items=apigw.JsonSchema(type=apigw.JsonSchemaType.STRING, max_length=32)),
                "filename": apigw.JsonSchema(type=apigw.JsonSchemaType.STRING, max_length=100),
                "fault": apigw.JsonSchema(type=apigw.JsonSchemaType.STRING, enum=["timeout", "error", "error_once"]),
            }))
        self.api.root.add_resource("uploads").add_method(
            "POST", integ, request_validator=validator, request_models={"application/json": upload_model})
        self.api.root.add_resource("images").add_resource("{image_id}").add_method(
            "GET", integ, request_validator=validator, request_parameters={"method.request.path.image_id": True})
        search_params = {f"method.request.querystring.{p}": False for p in (
            "status", "category", "tag", "top1_class", "class", "min_score", "uploaded_after", "uploaded_before",
            "size", "cursor")}
        self.api.root.add_resource("search").add_method("GET", integ, request_validator=validator,
                                                        request_parameters=search_params)
