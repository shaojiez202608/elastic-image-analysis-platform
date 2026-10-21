"""Compute: ECS Fargate (Linux/ARM64) services `dispatcher` and `indexer`, each scaled on its queue's backlog.

Scaling metric per service: ApproximateNumberOfMessagesVisible + ApproximateNumberOfMessagesNotVisible
of its queue (one-minute maximum), with Application Auto Scaling step scaling:
  backlog == 0 for 2 minutes  -> -1 task (down to min_tasks, possibly 0), scale-in cooldown
  backlog >= steps[i].lower   -> +steps[i].change tasks (up to max_tasks), scale-out cooldown
Counting in-flight messages keeps tasks that are still processing from being scaled in.
"""

from __future__ import annotations

from aws_cdk import Aws, Duration, Stack
from aws_cdk import aws_applicationautoscaling as appscaling
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct

from imaging.documents import INDEX_NAME

from .common import allow, log_retention, removal, s3_objects
from .config import AppConfig, ServiceScaling
from .inference_stack import InferenceStack
from .network_stack import NetworkStack
from .search_stack import SearchStack
from .storage_stack import StorageStack


def backlog_metric(queue, label: str) -> cw.MathExpression:
    period = Duration.minutes(1)
    return cw.MathExpression(
        expression="visible + inflight", label=label, period=period,
        using_metrics={"visible": queue.metric_approximate_number_of_messages_visible(period=period, statistic="Maximum"),
                       "inflight": queue.metric_approximate_number_of_messages_not_visible(period=period,
                                                                                           statistic="Maximum")})


def queue_step_scaling(scope: Construct, cid: str, target, metric: cw.IMetric, scaling: ServiceScaling, *,
                       alarm_prefix: str) -> None:
    """Two step policies with separate cooldowns (CDK's scale_on_metric shares one cooldown)."""
    out = appscaling.StepScalingAction(
        scope, f"{cid}ScaleOut", scaling_target=target, adjustment_type=appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
        cooldown=Duration.seconds(scaling.scale_out_cooldown_seconds),
        metric_aggregation_type=appscaling.MetricAggregationType.MAXIMUM)
    first = scaling.steps[0].lower
    for i, st in enumerate(scaling.steps):
        upper = scaling.steps[i + 1].lower - first if i + 1 < len(scaling.steps) else None
        out.add_adjustment(adjustment=st.change, lower_bound=st.lower - first, upper_bound=upper)
    cw.Alarm(scope, f"{cid}ScaleOutAlarm", alarm_name=f"{alarm_prefix}-backlog-high", metric=metric,
             threshold=first, evaluation_periods=1, datapoints_to_alarm=1,
             comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
             treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
             alarm_description="Queue backlog: scale the service out").add_alarm_action(
        cw_actions.ApplicationScalingAction(out))
    scale_in = appscaling.StepScalingAction(
        scope, f"{cid}ScaleIn", scaling_target=target, adjustment_type=appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
        cooldown=Duration.seconds(scaling.scale_in_cooldown_seconds),
        metric_aggregation_type=appscaling.MetricAggregationType.MAXIMUM)
    scale_in.add_adjustment(adjustment=-1, upper_bound=0)
    cw.Alarm(scope, f"{cid}ScaleInAlarm", alarm_name=f"{alarm_prefix}-backlog-empty", metric=metric,
             threshold=0, evaluation_periods=2, datapoints_to_alarm=2,
             comparison_operator=cw.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
             treat_missing_data=cw.TreatMissingData.BREACHING,
             alarm_description="Queue empty: scale the service in").add_alarm_action(
        cw_actions.ApplicationScalingAction(scale_in))


class ComputeStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: AppConfig, network: NetworkStack, storage: StorageStack,
                 inference: InferenceStack, search: SearchStack, image_tag: str, **kw) -> None:
        super().__init__(scope, cid, **kw)
        rp = removal(cfg)
        self.cluster = ecs.Cluster(self, "Cluster", cluster_name=cfg.name("cluster"), vpc=network.vpc,
                                   container_insights_v2=ecs.ContainerInsights.ENABLED)
        inf = cfg.inference
        common_env = {
            "IMAGES_BUCKET": storage.images.bucket_name, "INGEST_QUEUE_URL": storage.ingest.queue_url,
            "RESULTS_QUEUE_URL": storage.results.queue_url, "INGEST_DLQ_URL": storage.ingest_dlq.queue_url,
            "JOBS_TABLE": storage.jobs.table_name, "ENDPOINT_NAME": inference.endpoint_name,
            "SEARCH_ENDPOINT": search.domain.domain_endpoint,
            "MAX_UPLOAD_BYTES": str(cfg.max_upload_bytes), "MAX_PIXELS": str(cfg.max_pixels),
            "MIN_SIDE": str(cfg.min_side), "MAX_SIDE": str(cfg.max_side),
            "NORMALIZE_MAX_SIDE": str(cfg.normalize_max_side),
            "INVOCATION_TIMEOUT_SECONDS": str(inf.invocation_timeout_seconds),
            "MAX_INFERENCE_ATTEMPTS": str(inf.max_inference_attempts),
            "RETRY_BASE_DELAY_SECONDS": str(inf.retry_base_delay_seconds),
            "VISIBILITY_TIMEOUT_SECONDS": str(cfg.visibility_timeout_seconds),
            "RECEIVE_BATCH": str(cfg.workers.receive_batch), "TOP_K": str(inf.top_k),
            "FAULT_INJECTION": "true" if inf.fault_injection else "false",
            "METRICS_NAMESPACE": cfg.metrics_namespace, "CATEGORIES": ",".join(cfg.categories),
        }
        self.services: dict[str, ecs.FargateService] = {}

        # dispatcher: ingest queue -> validation -> InvokeEndpointAsync
        d_role = iam.Role(self, "DispatcherTaskRole", role_name=cfg.name("dispatcher-task"),
                          assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        storage.ingest.grant_consume_messages(d_role)
        storage.results.grant_send_messages(d_role)
        allow(d_role, "ReadUploads", ["s3:GetObject"], [s3_objects(cfg, "images", "uploads/*")])
        allow(d_role, "WriteStaging", ["s3:PutObject"], [s3_objects(cfg, "images", "staging/*")])
        storage.jobs.grant(d_role, "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem")
        storage.key.grant(d_role, "kms:Decrypt", "kms:GenerateDataKey")
        d_role.add_to_policy(iam.PolicyStatement(actions=["sagemaker:InvokeEndpointAsync"],
                                                 resources=[inference.endpoint_arn]))
        self._service(cfg, "dispatcher", d_role, storage, image_tag, common_env, storage.ingest,
                      cfg.workers.dispatcher, network, rp)

        # indexer: results queue -> output validation -> OpenSearch; retries back to the ingest queue
        i_role = iam.Role(self, "IndexerTaskRole", role_name=search.indexer_role_name,
                          assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        storage.results.grant_consume_messages(i_role)
        storage.ingest.grant_send_messages(i_role)
        storage.ingest_dlq.grant_send_messages(i_role)
        allow(i_role, "ReadAsyncOutput", ["s3:GetObject"], [s3_objects(cfg, "images", "async-output/*")])
        storage.jobs.grant(i_role, "dynamodb:GetItem", "dynamodb:UpdateItem")
        storage.key.grant(i_role, "kms:Decrypt", "kms:GenerateDataKey")
        i_role.add_to_policy(iam.PolicyStatement(actions=["es:ESHttpPut", "es:ESHttpGet"],
                                                 resources=[f"{search.domain_arn}/{INDEX_NAME}/*"]))
        self._service(cfg, "indexer", i_role, storage, image_tag, common_env, storage.results, cfg.workers.indexer,
                      network, rp)

    def _service(self, cfg: AppConfig, role_name: str, task_role: iam.Role, storage: StorageStack, image_tag: str,
                 env: dict[str, str], queue, scaling: ServiceScaling, network: NetworkStack, rp) -> None:
        cap = role_name.capitalize()
        group = logs.LogGroup(self, f"{cap}Logs", log_group_name=f"/{cfg.project}/{role_name}",
                              retention=log_retention(cfg), removal_policy=rp)
        exec_role = iam.Role(self, f"{cap}ExecutionRole", role_name=cfg.name(role_name, "exec"),
                             assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        task = ecs.FargateTaskDefinition(
            self, f"{cap}Task", family=cfg.name(role_name), cpu=cfg.workers.cpu,
            memory_limit_mib=cfg.workers.memory_mib, task_role=task_role, execution_role=exec_role,
            runtime_platform=ecs.RuntimePlatform(cpu_architecture=ecs.CpuArchitecture.ARM64,
                                                 operating_system_family=ecs.OperatingSystemFamily.LINUX))
        task.add_container(
            role_name, image=ecs.ContainerImage.from_ecr_repository(storage.repo, image_tag), command=[role_name],
            environment={**env, "AWS_REGION": Aws.REGION}, readonly_root_filesystem=True,
            logging=ecs.LogDriver.aws_logs(stream_prefix=role_name, log_group=group),
            stop_timeout=Duration.seconds(60),
            health_check=ecs.HealthCheck(command=["CMD", "python", "-c", "import imaging.worker_main"],
                                         interval=Duration.seconds(30), timeout=Duration.seconds(5), retries=3))
        service = ecs.FargateService(
            self, f"{cap}Service", service_name=cfg.name(role_name), cluster=self.cluster, task_definition=task,
            desired_count=scaling.min_tasks, assign_public_ip=False, vpc_subnets=network.subnets,
            security_groups=[network.workers_sg], platform_version=ecs.FargatePlatformVersion.LATEST,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True), min_healthy_percent=100,
            enable_execute_command=False)
        scalable = appscaling.ScalableTarget(
            self, f"{cap}ScalableTarget", service_namespace=appscaling.ServiceNamespace.ECS,
            resource_id=f"service/{self.cluster.cluster_name}/{service.service_name}",
            scalable_dimension="ecs:service:DesiredCount", min_capacity=scaling.min_tasks,
            max_capacity=scaling.max_tasks)
        scalable.node.add_dependency(service)
        queue_step_scaling(self, cap, scalable, backlog_metric(queue, f"{role_name} queue backlog"), scaling,
                           alarm_prefix=cfg.name(role_name))
        self.services[role_name] = service
