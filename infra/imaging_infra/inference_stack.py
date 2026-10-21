"""Inference: SageMaker AI model, asynchronous endpoint configuration, endpoint, and its scaling.

Scaling follows the SageMaker AI developer guide for asynchronous endpoints:
- target tracking on ApproximateBacklogSizePerInstance (the recommended metric), MinCapacity may be 0;
- an additional step policy driven by HasBacklogWithoutCapacity (alarm: 2 of 2 one-minute datapoints
  >= 1), so a backlog smaller than the target still wakes an endpoint that scaled in to zero.
https://docs.aws.amazon.com/sagemaker/latest/dg/async-inference-autoscale.html
"""

from __future__ import annotations

import hashlib
import json
import re

from aws_cdk import Aws, Duration, Stack
from aws_cdk import aws_applicationautoscaling as appscaling
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sagemaker as sm
from constructs import Construct

from .common import allow, s3_objects
from .config import AppConfig
from .storage_stack import StorageStack

VARIANT = "AllTraffic"
RE_IMAGE = re.compile(r"^(?P<acct>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com/(?P<repo>[a-z0-9._/-]+):(?P<tag>[A-Za-z0-9._-]+)$")
SYNTH_IMAGE_REPO = "pytorch-inference-arm64"


def placeholder_image(cfg: AppConfig) -> str:
    repo = SYNTH_IMAGE_REPO if cfg.inference.architecture == "arm64" else "pytorch-inference"
    return f"111122223333.dkr.ecr.{cfg.region}.amazonaws.com/{repo}:synth-placeholder"


class InferenceStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: AppConfig, storage: StorageStack, image_uri: str,
                 model_key: str, **kw) -> None:
        super().__init__(scope, cid, **kw)
        inf = cfg.inference
        m = RE_IMAGE.match(image_uri)
        if not m:
            raise ValueError("inferenceImage must be an ECR image URI <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/<REPO>:<TAG>")
        self.endpoint_name = cfg.name("endpoint")
        self.endpoint_arn = f"arn:{Aws.PARTITION}:sagemaker:{Aws.REGION}:{Aws.ACCOUNT_ID}:endpoint/{self.endpoint_name}"
        bucket = storage.images

        role = iam.Role(self, "ExecutionRole", role_name=cfg.name("sagemaker-exec"),
                        assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
                        description="SageMaker AI endpoint execution role (model artifact, async I/O, notifications)")
        allow(role, "ReadModelArtifact", ["s3:GetObject"], [s3_objects(cfg, "artifacts", model_key)])
        allow(role, "ReadStagedImages", ["s3:GetObject"], [s3_objects(cfg, "images", "staging/*")])
        allow(role, "WriteAsyncResults", ["s3:PutObject"], [s3_objects(cfg, "images", "async-output/*"),
                                                           s3_objects(cfg, "images", "async-failure/*")])
        storage.key.grant(role, "kms:Decrypt", "kms:GenerateDataKey")
        storage.success_topic.grant_publish(role)
        storage.error_topic.grant_publish(role)
        role.add_to_policy(iam.PolicyStatement(
            sid="PullPrebuiltInferenceImage",
            actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
            resources=[f"arn:{Aws.PARTITION}:ecr:{m.group('region')}:{m.group('acct')}:repository/{m.group('repo')}"]))
        role.add_to_policy(iam.PolicyStatement(sid="EcrAuth", actions=["ecr:GetAuthorizationToken"], resources=["*"]))
        role.add_to_policy(iam.PolicyStatement(
            sid="EndpointLogs", actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                                         "logs:DescribeLogStreams"],
            resources=[f"arn:{Aws.PARTITION}:logs:{Aws.REGION}:{Aws.ACCOUNT_ID}:log-group:/aws/sagemaker/Endpoints/{self.endpoint_name}",
                       f"arn:{Aws.PARTITION}:logs:{Aws.REGION}:{Aws.ACCOUNT_ID}:log-group:/aws/sagemaker/Endpoints/{self.endpoint_name}:*"]))
        role.add_to_policy(iam.PolicyStatement(
            sid="EndpointMetrics", actions=["cloudwatch:PutMetricData"], resources=["*"],
            conditions={"StringLike": {"cloudwatch:namespace": ["/aws/sagemaker/*", "AWS/SageMaker"]}}))
        self.role = role

        env = {
            "SAGEMAKER_PROGRAM": "inference.py",
            "SAGEMAKER_SUBMIT_DIRECTORY": "/opt/ml/model/code",
            "SAGEMAKER_CONTAINER_LOG_LEVEL": "20",
            "SAGEMAKER_REGION": cfg.region,
            "TOP_K": str(inf.top_k),
            "MAX_PIXELS": str(cfg.normalize_max_side * cfg.normalize_max_side * 2),
            "FAULT_INJECTION": "true" if inf.fault_injection else "false",
            "FAULT_SLEEP_SECONDS": str(inf.fault_sleep_seconds),
        }
        model_spec = {"image": image_uri, "data": model_key, "env": env, "iso": inf.network_isolation}
        h = hashlib.sha256(json.dumps(model_spec, sort_keys=True).encode()).hexdigest()[:8]
        model = sm.CfnModel(
            self, "Model", model_name=cfg.name("model", h), execution_role_arn=role.role_arn,
            enable_network_isolation=inf.network_isolation,
            primary_container=sm.CfnModel.ContainerDefinitionProperty(
                image=image_uri, model_data_url=f"s3://{storage.artifacts.bucket_name}/{model_key}", environment=env))
        model.node.add_dependency(role)

        cfg_spec = {"m": h, "type": inf.instance_type, "init": inf.initial_instances,
                    "conc": inf.max_concurrent_invocations_per_instance}
        h2 = hashlib.sha256(json.dumps(cfg_spec, sort_keys=True).encode()).hexdigest()[:8]
        epc = sm.CfnEndpointConfig(
            self, "EndpointConfig", endpoint_config_name=cfg.name("epc", h2), kms_key_id=storage.key.key_arn,
            production_variants=[sm.CfnEndpointConfig.ProductionVariantProperty(
                variant_name=VARIANT, model_name=model.attr_model_name, instance_type=inf.instance_type,
                initial_instance_count=inf.initial_instances, initial_variant_weight=1.0)],
            async_inference_config=sm.CfnEndpointConfig.AsyncInferenceConfigProperty(
                client_config=sm.CfnEndpointConfig.AsyncInferenceClientConfigProperty(
                    max_concurrent_invocations_per_instance=inf.max_concurrent_invocations_per_instance),
                output_config=sm.CfnEndpointConfig.AsyncInferenceOutputConfigProperty(
                    s3_output_path=f"s3://{bucket.bucket_name}/async-output/",
                    s3_failure_path=f"s3://{bucket.bucket_name}/async-failure/",
                    kms_key_id=storage.key.key_arn,
                    notification_config=sm.CfnEndpointConfig.AsyncInferenceNotificationConfigProperty(
                        success_topic=storage.success_topic.topic_arn, error_topic=storage.error_topic.topic_arn))))
        self.endpoint = sm.CfnEndpoint(self, "Endpoint", endpoint_name=self.endpoint_name,
                                       endpoint_config_name=epc.attr_endpoint_config_name)

        target = appscaling.ScalableTarget(
            self, "ScalableTarget", service_namespace=appscaling.ServiceNamespace.SAGEMAKER,
            resource_id=f"endpoint/{self.endpoint_name}/variant/{VARIANT}",
            scalable_dimension="sagemaker:variant:DesiredInstanceCount",
            min_capacity=inf.min_instances, max_capacity=inf.max_instances)
        target.node.add_dependency(self.endpoint)
        dims = {"EndpointName": self.endpoint_name}
        backlog_per_instance = cw.Metric(namespace="AWS/SageMaker", metric_name="ApproximateBacklogSizePerInstance",
                                         dimensions_map=dims, statistic="Average", period=Duration.minutes(1))
        target.scale_to_track_metric(
            "BacklogPerInstance", target_value=inf.target_backlog_per_instance, custom_metric=backlog_per_instance,
            scale_in_cooldown=Duration.seconds(inf.scale_in_cooldown_seconds),
            scale_out_cooldown=Duration.seconds(inf.scale_out_cooldown_seconds), policy_name=cfg.name("backlog-tracking"))
        if inf.scale_from_zero:
            step = appscaling.StepScalingAction(
                self, "FromZero", scaling_target=target, policy_name=cfg.name("has-backlog-without-capacity"),
                adjustment_type=appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
                metric_aggregation_type=appscaling.MetricAggregationType.AVERAGE,
                cooldown=Duration.seconds(inf.scale_out_cooldown_seconds))
            step.add_adjustment(adjustment=1, lower_bound=0)
            alarm = cw.Alarm(
                self, "FromZeroAlarm", alarm_name=cfg.name("has-backlog-without-capacity"),
                metric=cw.Metric(namespace="AWS/SageMaker", metric_name="HasBacklogWithoutCapacity",
                                 dimensions_map=dims, statistic="Average", period=Duration.minutes(1)),
                threshold=1, evaluation_periods=2, datapoints_to_alarm=2,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.MISSING,
                alarm_description="Scales the asynchronous endpoint out from zero when requests are queued")
            alarm.add_alarm_action(cw_actions.ApplicationScalingAction(step))
        self.scalable_target = target
