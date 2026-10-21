# Security notes

Least-privilege summary, known risks, and every cdk-nag acknowledgement with its reason. The authoritative list of
acknowledgements is `infra/imaging_infra/nag.py`; each entry is attached to one construct path, templated from the
profile (prefix, Region, index), and an entry that no longer matches a construct fails the synth.

## Network

- VPC with isolated subnets only: no internet gateway, no NAT gateway, no public IPs (`tests/test_infra.py`).
- Interface endpoints (SQS, SageMaker Runtime, ECR API, ECR Docker, CloudWatch Logs; execute-api in the handover
  profile) accept HTTPS only from the project's security groups; gateway endpoints for S3 and DynamoDB.
- Security groups have no ingress from CIDR ranges. The search domain accepts 443 from the indexer, API and
  bootstrap security groups only. Worker/API egress is HTTPS only: to the endpoints, the domain, and 443 to any
  address for the S3/DynamoDB gateway endpoints (the subnets have no other route out).
- VPC flow logs to CloudWatch Logs with retention.

## Identity and access

| Principal | Can | Cannot |
|---|---|---|
| API Lambda role | DynamoDB Get/Put on the jobs table; PutObject on `uploads/*` (signs the presigned POST); KMS GenerateDataKey/Decrypt on the data key; HTTP GET/POST on the images index | write to the index (FGAC `imaging_reader`), read images |
| Dispatcher task role | consume the ingest queue, send to the results queue; GetObject `uploads/*`, PutObject `staging/*`; DynamoDB Get/Put/Update; InvokeEndpointAsync on the one endpoint | read outputs, touch the index |
| Indexer task role | consume the results queue, send to the ingest queue and ingest DLQ; GetObject `async-output/*`; DynamoDB Get/Update; HTTP PUT/GET on the images index | invoke the endpoint, read uploads; FGAC `imaging_writer` only allows index/get on one index |
| SageMaker AI execution role | GetObject on the exact model artifact key and on `staging/*`; PutObject `async-output/*`, `async-failure/*`; publish to the two topics; pull the prebuilt image repository; endpoint logs/metrics | read uploads or anything else in the buckets |
| Search master role (bootstrap Lambda) | FGAC master: creates the index and the two FGAC roles/mappings | nothing outside the domain |
| CodeBuild role | read `source/*`, write `model/*` in the artifacts bucket, push to the worker repository | everything else |

Trust policies (`infra/imaging_infra/trust.py`): SageMaker AI (`arn:aws:sagemaker:<REGION>:<ACCOUNT>:*`, the exact
form the SageMaker guide requires), ECS task roles (`arn:aws:ecs:<REGION>:<ACCOUNT>:*`), CodeBuild (project ARN
prefix) and VPC Flow Logs carry both `aws:SourceAccount` and `aws:SourceArn`. Not changed, because the service
documents no such trust-policy guidance: Lambda execution roles
(https://docs.aws.amazon.com/lambda/latest/dg/lambda-intro-execution-role.html), ECS task execution roles
(https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_execution_IAM_role.html) and the Lambda roles of
CDK-provided custom resources.

Key policies: service-principal statements (S3 notifications, SNS deliveries, CloudWatch alarm actions) carry
`aws:SourceAccount`; CDK's S3 statement additionally carries `aws:SourceArn` = the images bucket.

Account-level settings are not changed: the API stage does not create the API Gateway CloudWatch role
(`cloud_watch_role=False`, test asserts no `AWS::ApiGateway::Account`); service-linked roles are expected to
exist (preflight checks, does not create).

## Data

- S3: Block Public Access, TLS-only bucket policies (min TLS 1.2), SSE-KMS with bucket keys (data key), server
  access logs to a separate log bucket (SSE-S3, as log delivery requires), lifecycle expiry per prefix.
- SQS and SNS: SSE-KMS (messaging key), TLS-only policies.
- DynamoDB: KMS (data key), point-in-time recovery, deletion protection in the handover profile.
- OpenSearch: KMS at rest, node-to-node TLS, HTTPS only (TLS 1.2 policy), FGAC, VPC only.
- ECR: KMS, scan on push, immutable tags. SageMaker AI endpoint: storage volume and async output encrypted with the
  data key; model network isolation on.
- Logs: every log group has retention; logs contain IDs, reason codes, exception class names and field paths -
  never image content, model output text or exception messages.

## cdk-nag acknowledgements

| Stack | Construct | Finding | Reason |
|---|---|---|---|
| storage | BucketNotificationsHandler*/Role | IAM4 AWSLambdaBasicExecutionRole | CDK-provided handler; managed policy only grants Logs write |
| storage | Build/Role/DefaultPolicy | IAM5 kms:GenerateDataKey*, kms:ReEncrypt* | CDK grant for the project key; single key ARN |
| storage | Build/Role/DefaultPolicy | IAM5 s3:GetBucket*, GetObject*, List* | CDK grant for the CodeBuild S3 source (read-only, artifacts bucket) |
| storage | Build/Role/DefaultPolicy | IAM5 Resource::* | ecr:GetAuthorizationToken has no resource-level permissions |
| storage | Build/Role/DefaultPolicy | IAM5 report-group/<PROJECT>-* , log-group /aws/codebuild/<PROJECT>:* | CDK defaults scoped to this project |
| storage | Build/Role/DefaultPolicy | IAM5 artifacts `model/*`, `source/*` | object keys under one prefix |
| inference | ExecutionRole/DefaultPolicy | IAM5 kms:GenerateDataKey* | CDK topic-publish grant on the messaging key |
| inference | ExecutionRole/DefaultPolicy | IAM5 Resource::* | ecr:GetAuthorizationToken; PutMetricData limited by namespace condition |
| inference | ExecutionRole/DefaultPolicy | IAM5 /aws/sagemaker/Endpoints/<PREFIX>-endpoint:* | per-instance log streams in the endpoint's log group |
| inference | ExecutionRole/DefaultPolicy | IAM5 images `staging/*`, `async-output/*`, `async-failure/*` | one prefix each |
| search | AwsCustomResource singleton | IAM4 AWSLambdaBasicExecutionRole; IAM5 kms:Describe*/List* | CDK custom resource (domain log policy, key lookup) |
| search | Domain/ESLogGroupPolicy* | IAM5 Resource::* | logs:PutResourcePolicy has no resource-level permissions |
| search | Api methods (3) | COG4 | IAM (SigV4) authorisation instead of Cognito |
| search | Api stage | APIG3 | no web clients; IAM-authorised; WAF in the hardening checklist |
| search | ApiRole, SearchMasterRole | IAM4 AWSLambdaVPCAccessExecutionRole | ENI management for Lambda in a VPC has no resource scoping |
| search | ApiRole/DefaultPolicy | IAM5 domain/<PREFIX>-search/images-v1/* ; images `uploads/*` | one index; one prefix (presigned POST) |
| search | SearchMasterRole/DefaultPolicy | IAM5 domain/<PREFIX>-search/* | FGAC master needs `_plugins/_security` and index creation |
| search | SearchBootstrapProvider/framework-onEvent | IAM4 basic execution; IAM5 `<fn>:*` | CDK Provider framework |
| search | Domain | OS3 | VPC domain; IP allowlists do not apply |
| search | Domain | OS4 | dedicated manager nodes are a production sizing decision |
| search | Domain | OS7 (only with one data node) | zone awareness is on automatically with more than one node |
| search | Domain | OS9 slow logs | application logs published; slow logs when tuning |
| compute | Dispatcher/Indexer ExecutionRole | IAM5 Resource::* | ecr:GetAuthorizationToken |
| compute | Dispatcher/Indexer Task | ECS2 | environment holds names/URLs/limits only, no secrets |
| compute | Dispatcher/Indexer TaskRole | IAM5 kms:GenerateDataKey*, ReEncrypt* | CDK SQS grants on the messaging key |
| compute | DispatcherTaskRole | IAM5 images `uploads/*`, `staging/*` | one prefix each |
| compute | IndexerTaskRole | IAM5 images `async-output/*`; domain index `images-v1/*` | one prefix; one index |

## Known risks (prototype)

- The S3/DynamoDB egress rule allows 443 to any IPv4 address; harmless while the subnets have no internet route,
  but it would matter if a NAT were added. Hardening: prefix-list based rules.
- Gateway endpoint policies are the default (full access to the service) because ECR layer downloads use an
  AWS-owned bucket; hardening: restrict to the project buckets plus the documented ECR layer bucket.
- Fault injection exists in code (off in the handover profile; the API refuses the field when off).
- No WAF, no per-caller quotas beyond stage throttling, no end-user identity.
- The example model's weights were trained on ImageNet-1k (OPEN-QUESTIONS 7).
