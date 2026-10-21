"""Observability: one CloudWatch dashboard and the alarms (FR-10).

Dashboard rows: queues (depth, in-flight, oldest age, DLQs) | ECS tasks per service | endpoint backlog,
instances and utilisation | pipeline metrics from the workers (EMF: latency, failures, retries).
"""

from __future__ import annotations

from aws_cdk import Duration, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from constructs import Construct

from .compute_stack import ComputeStack
from .config import AppConfig
from .inference_stack import VARIANT, InferenceStack
from .storage_stack import StorageStack

MIN = Duration.minutes(1)


class ObservabilityStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: AppConfig, storage: StorageStack, inference: InferenceStack,
                 compute: ComputeStack, **kw) -> None:
        super().__init__(scope, cid, **kw)
        ns = cfg.metrics_namespace
        ep = {"EndpointName": inference.endpoint_name}
        epv = {**ep, "VariantName": VARIANT}

        def app_metric(name: str, service: str, stat: str = "Sum") -> cw.Metric:
            return cw.Metric(namespace=ns, metric_name=name, dimensions_map={"Service": service}, statistic=stat,
                             period=MIN)

        def sm(name: str, stat: str = "Average", dims=ep, namespace: str = "AWS/SageMaker") -> cw.Metric:
            return cw.Metric(namespace=namespace, metric_name=name, dimensions_map=dims, statistic=stat, period=MIN)

        def tasks(service: str) -> cw.Metric:
            return cw.Metric(namespace="ECS/ContainerInsights", metric_name="RunningTaskCount", statistic="Maximum",
                             period=MIN, dimensions_map={"ClusterName": cfg.name("cluster"),
                                                         "ServiceName": cfg.name(service)})

        q = {"ingest": storage.ingest, "results": storage.results}
        dash = cw.Dashboard(self, "Dashboard", dashboard_name=cfg.name("dashboard"))
        dash.add_widgets(
            cw.GraphWidget(title="Queue depth (visible / in flight)", width=12, left=[
                m for qq in q.values() for m in (qq.metric_approximate_number_of_messages_visible(period=MIN, statistic="Maximum"),
                                                 qq.metric_approximate_number_of_messages_not_visible(period=MIN, statistic="Maximum"))]),
            cw.GraphWidget(title="Oldest message age (s) and DLQ depth", width=12,
                           left=[qq.metric_approximate_age_of_oldest_message(period=MIN, statistic="Maximum") for qq in q.values()],
                           right=[storage.ingest_dlq.metric_approximate_number_of_messages_visible(period=MIN, statistic="Maximum"),
                                  storage.results_dlq.metric_approximate_number_of_messages_visible(period=MIN, statistic="Maximum")]))
        dash.add_widgets(
            cw.GraphWidget(title="ECS running tasks (container layer)", width=12,
                           left=[tasks("dispatcher"), tasks("indexer")]),
            cw.GraphWidget(title="Endpoint backlog and instances (inference layer)", width=12,
                           left=[sm("ApproximateBacklogSize"), sm("ApproximateBacklogSizePerInstance")],
                           right=[sm("HasBacklogWithoutCapacity", "Maximum"),
                                  cw.MathExpression(expression="cpu", label="instances (CPUUtilization sample count)",
                                                    period=MIN, using_metrics={"cpu": sm(
                                                        "CPUUtilization", "SampleCount", epv, "/aws/sagemaker/Endpoints")})]))
        dash.add_widgets(
            cw.GraphWidget(title="Inference utilisation (CPU %, memory %)", width=8,
                           left=[sm("CPUUtilization", "Average", epv, "/aws/sagemaker/Endpoints"),
                                 sm("MemoryUtilization", "Average", epv, "/aws/sagemaker/Endpoints")]),
            cw.GraphWidget(title="Latency (ms): queue wait, end to end, endpoint backlog time", width=8,
                           left=[app_metric("IngestQueueWaitMs", "dispatcher", "p50"),
                                 app_metric("IngestQueueWaitMs", "dispatcher", "p90"),
                                 app_metric("EndToEndMs", "indexer", "p50"), app_metric("EndToEndMs", "indexer", "p90"),
                                 sm("TimeInBacklog", "Average", epv)]),
            cw.GraphWidget(title="Outcomes per minute", width=8, left=[
                app_metric("Analyzed", "indexer"), app_metric("RejectedFiles", "dispatcher"),
                app_metric("FailedJobs", "indexer"), app_metric("InferenceRetries", "dispatcher"),
                app_metric("DuplicateDeliveries", "dispatcher"), app_metric("DuplicateDeliveries", "indexer"),
                app_metric("MessageErrors", "dispatcher"), app_metric("MessageErrors", "indexer")]))

        topic = sns.Topic(self, "AlarmTopic", topic_name=cfg.name("alarms"), master_key=storage.msg_key, enforce_ssl=True)
        if cfg.alarm_email:
            topic.add_subscription(subs.EmailSubscription(cfg.alarm_email))
        action = cw_actions.SnsAction(topic)

        def alarm(cid_: str, name: str, metric: cw.IMetric, threshold: float, desc: str, periods: int = 1) -> None:
            a = cw.Alarm(self, cid_, alarm_name=cfg.name(name), metric=metric, threshold=threshold,
                         evaluation_periods=periods, datapoints_to_alarm=periods,
                         comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                         treat_missing_data=cw.TreatMissingData.NOT_BREACHING, alarm_description=desc)
            a.add_alarm_action(action)

        alarm("IngestDlqAlarm", "ingest-dlq-not-empty",
              storage.ingest_dlq.metric_approximate_number_of_messages_visible(period=MIN, statistic="Maximum"), 1,
              "Messages in the ingest DLQ (failed jobs or poison messages): inspect and redrive")
        alarm("ResultsDlqAlarm", "results-dlq-not-empty",
              storage.results_dlq.metric_approximate_number_of_messages_visible(period=MIN, statistic="Maximum"), 1,
              "Messages in the results DLQ (indexing kept failing): inspect and redrive")
        alarm("IngestAgeAlarm", "ingest-oldest-message-age",
              storage.ingest.metric_approximate_age_of_oldest_message(period=MIN, statistic="Maximum"),
              cfg.oldest_message_alarm_seconds, "Ingest queue is not being drained fast enough", periods=3)
        alarm("FailedJobsAlarm", "failed-jobs",
              cw.Metric(namespace=ns, metric_name="FailedJobs", dimensions_map={"Service": "indexer"}, statistic="Sum",
                        period=Duration.minutes(5)), cfg.failure_alarm_count, "Jobs failed after all inference attempts")
        alarm("EndpointNotWakingAlarm", "endpoint-backlog-without-capacity",
              sm("HasBacklogWithoutCapacity", "Minimum"), 1,
              "Endpoint has queued requests but no instances for 15 minutes", periods=15)
        self.dashboard = dash
        self.add_dependency(compute)
