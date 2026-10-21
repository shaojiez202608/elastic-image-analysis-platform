# Deployment guide

The commands themselves are listed once, in README "Deployment". This guide explains prerequisites, parameters and
what each phase does.

## Prerequisites

- A disposable, non-production AWS account; credentials in the shell environment; the target Region bootstrapped
  for CDK (the scripts never modify the CDKToolkit stack).
- Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js (for `npx aws-cdk@2`), AWS CLI v2, git. Docker is **not**
  needed: the worker image and the model artifact are built in CodeBuild.
- Account-level items that must already exist (the preflight checks them, never creates them): the API Gateway
  account CloudWatch Logs role, and the service-linked roles for Application Auto Scaling (ECS, SageMaker endpoint),
  OpenSearch and ECS.
- Quotas: a SageMaker AI endpoint quota for the configured instance type of at least `inference.max_instances`;
  one free VPC. The preflight reads them; it does not request increases.

## Phases of `make deploy`

1. `scripts/preflight.sh` (read-only): confirms account (last four digits) and Region, toolchain, configuration
   validity, quotas and availability (SageMaker instance type, OpenSearch type/version, prebuilt inference image tag,
   Fargate ARM64 zone support, VPC quota), and runs the destructive-change guard on `cdk diff`.
2. `cdk deploy proto-imaging-network proto-imaging-storage` - VPC, endpoints, buckets, queues, topics, table, ECR,
   CodeBuild.
3. `scripts/build.sh` - `git archive HEAD` to the artifacts bucket, one CodeBuild run (ARM64) that downloads and
   verifies the model weights, uploads `model.tar.gz`, builds and pushes the worker image tagged with the commit;
   writes `build/deploy-context.json`.
4. `cdk deploy --all` with that context - endpoint and scaling, search domain and API, ECS services, dashboard
   and alarms. The OpenSearch domain and the endpoint take the longest.

## Parameters (`config/<PROFILE>.json`)

| Section | Key | Meaning |
|---|---|---|
| top | `project` | resource prefix (`proto-...`), also the stack prefix |
| top | `region`, `network.availability_zones` | Region and two AZs (fixed so synth needs no AWS lookups) |
| top | `removal_policy` | `destroy` (sandbox) or `retain` (handover) |
| top | `tags` | must include Project, Purpose, Teardown |
| `upload` | `max_upload_bytes`, `presign_expires_seconds`, `categories` | upload limits and allowed metadata categories |
| `image_limits` | `max_pixels`, `min_side`, `max_side`, `normalize_max_side` | validation and normalisation |
| `queues` | `visibility_timeout_seconds`, `max_receive_count`, `message_retention_days` | SQS behaviour and redrive |
| `workers` | `cpu`, `memory_mib`, `receive_batch`; per service `min_tasks`, `max_tasks`, `steps`, cooldowns | Fargate size and queue-driven scaling |
| `inference` | `instance_type`, `architecture`, `accelerator`, framework/py version | endpoint hardware and prebuilt image |
| `inference` | `min_instances`, `max_instances`, `initial_instances`, `target_backlog_per_instance`, cooldowns, `scale_from_zero` | endpoint scaling |
| `inference` | `max_concurrent_invocations_per_instance`, `invocation_timeout_seconds`, `max_inference_attempts`, `retry_base_delay_seconds` | asynchronous invocation and retries |
| `inference` | `network_isolation`, `fault_injection`, `fault_sleep_seconds`, `top_k` | model container settings |
| `search` | `engine_version`, `instance_type`, `data_nodes`, `volume_gib` | domain |
| `api` | `endpoint_type`, `throttle_rate`, `throttle_burst` | REGIONAL or PRIVATE; stage throttling |
| `observability` | `alarm_email`, `oldest_message_alarm_seconds`, `failure_alarm_count` | alarms |

Validation runs at synth time (`infra/imaging_infra/config.py`), e.g. arm64 + gpu is refused, `min_instances: 0`
requires `scale_from_zero`, the fault sleep must exceed the invocation timeout, AZs must belong to the Region.

Instance types: choose an endpoint type the Region offers (Service Quotas shows "<TYPE> for endpoint usage") and an
OpenSearch type from https://docs.aws.amazon.com/opensearch-service/latest/developerguide/supported-instance-types.html.
The sandbox values are sandbox-only deployable values.

## Billing dimensions

No figures are given here. The deployment is billed by the services' own dimensions: SageMaker AI endpoint instance
time and storage; OpenSearch instance time and storage; Fargate vCPU and memory time; Lambda requests and duration;
API Gateway requests; SQS/SNS requests; DynamoDB on-demand requests and storage; S3 storage and requests; KMS keys and
requests; interface VPC endpoint hours and data processed; CloudWatch logs, metrics, alarms and dashboards; CodeBuild
build minutes; ECR storage. Check each service's official billing page and consider an AWS Budgets alert on the
account before deploying.
