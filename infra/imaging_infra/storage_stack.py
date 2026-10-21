"""Storage and queues: KMS key, buckets, SQS queues with DLQs, SNS notification topics, the job table,
the worker image repository and the CodeBuild project that builds the image and the model artifact.

Images bucket prefixes:
  uploads/<IMAGE_ID>/original.<ext>   presigned POST target (S3 event -> ingest queue)
  staging/<IMAGE_ID>.jpg              normalised image written by the dispatcher, read by the endpoint
  async-output/ , async-failure/      SageMaker AI asynchronous inference output / failure payloads
"""

from __future__ import annotations

from aws_cdk import Aws, Duration, Stack
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_notifications as s3n
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from .common import allow, bucket_name, log_retention, removal, s3_objects
from .config import AppConfig


class StorageStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: AppConfig, **kw) -> None:
        super().__init__(scope, cid, **kw)
        rp = removal(cfg)
        destroy = cfg.destroy_on_delete
        self.key = kms.Key(self, "DataKey", alias=f"alias/{cfg.project}-data", enable_key_rotation=True,
                           removal_policy=rp, pending_window=Duration.days(7),
                           description="Encrypts images, the job table, the image repository and build artifacts")
        # Queues and topics use their own key: CDK adds the S3 service principal to the queue key's policy with
        # the images bucket ARN as aws:SourceArn, which would be circular if the bucket used the same key.
        self.msg_key = kms.Key(self, "MessagingKey", alias=f"alias/{cfg.project}-messaging", enable_key_rotation=True,
                               removal_policy=rp, pending_window=Duration.days(7),
                               description="Encrypts the SQS queues and SNS topics")
        # CloudWatch alarms publish to the encrypted alarm topic (observability stack).
        self.msg_key.add_to_resource_policy(iam.PolicyStatement(
            sid="CloudWatchAlarms", principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
            actions=["kms:Decrypt", "kms:GenerateDataKey"], resources=["*"],
            conditions={"StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID}}))

        self.access_logs = s3.Bucket(
            self, "AccessLogs", bucket_name=f"{cfg.project}-access-logs-{Aws.ACCOUNT_ID}-{Aws.REGION}",
            encryption=s3.BucketEncryption.S3_MANAGED, block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True, object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED, removal_policy=rp,
            auto_delete_objects=destroy, lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(30))])

        def bucket(cid_: str, name: str, rules: list[s3.LifecycleRule]) -> s3.Bucket:
            return s3.Bucket(
                self, cid_, bucket_name=bucket_name(cfg, name),
                encryption=s3.BucketEncryption.KMS, encryption_key=self.key, bucket_key_enabled=True,
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True, minimum_tls_version=1.2,
                object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED, versioned=False,
                server_access_logs_bucket=self.access_logs, server_access_logs_prefix=f"{name}/",
                removal_policy=rp, auto_delete_objects=destroy, lifecycle_rules=rules)

        days = Duration.days
        self.images = bucket("Images", "images", [
            s3.LifecycleRule(prefix="uploads/", expiration=days(cfg.upload_expiry_days)),
            s3.LifecycleRule(prefix="staging/", expiration=days(cfg.upload_expiry_days)),
            s3.LifecycleRule(prefix="async-output/", expiration=days(cfg.output_expiry_days)),
            s3.LifecycleRule(prefix="async-failure/", expiration=days(cfg.output_expiry_days)),
            s3.LifecycleRule(abort_incomplete_multipart_upload_after=days(1)),
        ])
        self.artifacts = bucket("Artifacts", "artifacts", [
            s3.LifecycleRule(prefix="source/", expiration=days(30)),
            s3.LifecycleRule(abort_incomplete_multipart_upload_after=days(1)),
        ])

        def queue(cid_: str, name: str, dlq: sqs.IQueue | None) -> sqs.Queue:
            return sqs.Queue(
                self, cid_, queue_name=cfg.name(name), encryption=sqs.QueueEncryption.KMS, encryption_master_key=self.msg_key,
                data_key_reuse=Duration.minutes(5), enforce_ssl=True, removal_policy=rp,
                retention_period=days(14) if dlq is None else days(cfg.message_retention_days),
                visibility_timeout=Duration.seconds(cfg.visibility_timeout_seconds),
                dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=cfg.max_receive_count) if dlq else None)

        self.ingest_dlq = queue("IngestDlq", "ingest-dlq", None)
        self.ingest = queue("Ingest", "ingest", self.ingest_dlq)
        self.results_dlq = queue("ResultsDlq", "results-dlq", None)
        self.results = queue("Results", "results", self.results_dlq)
        self.images.add_event_notification(s3.EventType.OBJECT_CREATED, s3n.SqsDestination(self.ingest),
                                           s3.NotificationKeyFilter(prefix="uploads/"))

        self.success_topic = sns.Topic(self, "InferenceSuccess", topic_name=cfg.name("inference-success"),
                                       master_key=self.msg_key, enforce_ssl=True)
        self.error_topic = sns.Topic(self, "InferenceError", topic_name=cfg.name("inference-error"),
                                     master_key=self.msg_key, enforce_ssl=True)
        for topic in (self.success_topic, self.error_topic):
            topic.add_subscription(subs.SqsSubscription(self.results))
            topic.apply_removal_policy(rp)

        self.jobs = ddb.Table(
            self, "Jobs", table_name=cfg.name("jobs"),
            partition_key=ddb.Attribute(name="image_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST, encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.key,
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True),
            removal_policy=rp, deletion_protection=not destroy)

        self.repo = ecr.Repository(
            self, "WorkerRepo", repository_name=cfg.name("worker"), image_scan_on_push=True,
            image_tag_mutability=ecr.TagMutability.IMMUTABLE, encryption=ecr.RepositoryEncryption.KMS,
            encryption_key=self.key, removal_policy=rp, empty_on_delete=destroy,
            lifecycle_rules=[ecr.LifecycleRule(max_image_count=10)])

        build_logs = logs.LogGroup(self, "BuildLogs", log_group_name=f"/{cfg.project}/codebuild",
                                   retention=log_retention(cfg), removal_policy=rp)
        self.build = codebuild.Project(
            self, "Build", project_name=cfg.name("build"),
            description="Builds the ARM64 worker image and the model artifact (no local Docker needed)",
            source=codebuild.Source.s3(bucket=self.artifacts, path="source/placeholder.zip"),
            build_spec=codebuild.BuildSpec.from_source_filename("buildspec/build.yml"),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0,
                compute_type=codebuild.ComputeType.SMALL, privileged=True),
            encryption_key=self.key, timeout=Duration.minutes(30),
            logging=codebuild.LoggingOptions(cloud_watch=codebuild.CloudWatchLoggingOptions(log_group=build_logs)))
        self.repo.grant_pull_push(self.build)
        allow(self.build, "DescribeOwnImages", ["ecr:DescribeImages"], [self.repo.repository_arn])
        allow(self.build, "ReadSource", ["s3:GetObject", "s3:GetObjectVersion"], [s3_objects(cfg, "artifacts", "source/*")])
        allow(self.build, "WriteModel", ["s3:GetObject", "s3:PutObject"], [s3_objects(cfg, "artifacts", "model/*")])
        allow(self.build, "ListModel", ["s3:ListBucket"], [self.artifacts.bucket_arn],
              {"StringLike": {"s3:prefix": ["model/*"]}})
