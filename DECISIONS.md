# Decisions

Each entry: what was chosen, alternatives, why. When two options were otherwise equal, the more conservative
and safer one was taken.

## Technology choices

| Layer | Chosen | Alternatives | Why |
|---|---|---|---|
| IaC | AWS CDK v2, Python (aws-cdk-lib and cdk-nag versions pinned in `uv.lock`) with cdk-nag AwsSolutions | CDK TypeScript | one language with the worker/API code; cdk-nag v3 `Validations.acknowledge` gives construct-scoped acknowledgements |
| Upload | S3 presigned POST (key, Content-Type, content-length range in the signed policy) | presigned PUT; upload through the API | POST policies let S3 enforce size and type; large files never pass through Lambda |
| Queue | SQS standard queues + DLQs, KMS | SNS+SQS fan-out; EventBridge | simplest, gives queue depth for scaling, redrive semantics are well defined; at-least-once handled by idempotency |
| Business service | ECS Fargate, Linux/ARM64, two services (dispatcher, indexer) from one image | Lambda (arm64); EKS | long-polling consumers with queue-driven scaling to zero and back; Graviton; no cluster to run |
| Image build | CodeBuild (ARM64 Linux, privileged) from `git archive HEAD` | local Docker; CDK Docker assets | no local Docker on the maintainer machine; build input is exactly the committed tree |
| Inference | SageMaker AI asynchronous endpoint, prebuilt PyTorch inference image (arm64), Graviton CPU instance in the sandbox | real-time endpoint; serverless inference; batch transform; GPU instance | built-in queue, scale to zero, SNS notifications; CPU because the model is small and GPU quota is limited in the shared account (see below) |
| Example model | ResNet-18 weights `timm/resnet18.a1_in1k` (Apache-2.0), pinned revision + SHA-256, loaded into torchvision ResNet-18 | torchvision weights (no explicit license on the weights); detection models | explicit license on the weights; small; strict state_dict compatibility verified |
| Metadata search | OpenSearch Service domain in the VPC, FGAC with IAM master role, KMS, node-to-node TLS | OpenSearch Serverless; DynamoDB + simple queries | multi-field filters and paging; FGAC roles as required; external versioning for idempotent writes |
| Query API | API Gateway REST, IAM (SigV4) auth, resource policy, Lambda ARM64 in the VPC | ALB + ECS | no identity system to build; request validation; throttling |
| Observability | CloudWatch dashboard, alarms, EMF metrics from the workers | Grafana | native, nothing extra to deploy |
| Load test | own Python script (uploads through the real API, samples queue/ECS/endpoint state, reads scaling activities) | Distributed Load Testing on AWS; Locust | the load is "objects arriving", measured end to end through two queues; DLT targets HTTP endpoints |
| Local mode | in-process stand-ins (queue with visibility/redrive, object store, fake async endpoint emitting the real notification format, versioned in-memory index) | LocalStack; Docker containers | no Docker or system services; same Dispatcher/Indexer code path |

## Design decisions

1. **GPU vs CPU.** Service Quotas (read-only) showed non-zero GPU endpoint quotas, so the GPU rule did not force
   CPU. CPU (Graviton) was still chosen for the sandbox: the model is small, the shared account's GPU quotas are
   limited and used by others, and the scaling behaviour under test is independent of the accelerator. GPU is a
   profile switch (`architecture: x86`, `accelerator: gpu`). No quota increase was requested.
2. **Region.** The sandbox template defaults to us-east-1. At deployment time us-east-1 had no VPC quota left
   (all VPCs belonged to other projects); the run used us-west-2 (local profile copy), recorded in DEPLOYMENT-LOG.
   No quota change was requested.
3. **No NAT, isolated subnets, VPC endpoints.** Smaller attack surface; every dependency has an endpoint
   (S3, DynamoDB gateway; SQS, SageMaker Runtime, ECR API/Docker, CloudWatch Logs interface). Interface endpoints
   accept HTTPS only from the project's security groups (`open=False`).
4. **Two KMS keys.** Queues/topics use a messaging key, data (buckets, table, ECR, domain, build) a data key: with one
   key, the S3-notification key-policy statement (SourceArn = bucket) and the bucket's encryption key form a
   CloudFormation cycle. Service-principal statements without conditions get `aws:SourceAccount` added.
5. **Idempotency design.** Job state in DynamoDB with conditional transitions (status, attempt, DISPATCHING lease);
   InferenceId = `<IMAGE_ID>-a<ATTEMPT>`; document ID = image ID, written with `version_type=external` (version =
   attempt), so a replay is a version conflict, not a second write. Side effect first, transition second, so a crash
   between them causes a harmless replay instead of a lost update.
6. **Bad files are not retried and not sent to the DLQ.** They are permanent: recorded `REJECTED` with a reason and
   searchable; the DLQ is kept for things an operator must look at (exhausted inference attempts, poison messages).
7. **Inference retries are bounded by the pipeline, not by SQS redelivery.** The endpoint does not retry container
   errors; the indexer schedules the next attempt on the ingest queue with exponential delay, then records `FAILED`
   and writes a record to the ingest DLQ.
8. **Scaling metric = visible + in-flight messages** (one-minute maximum), scale-in only after two empty minutes,
   separate cooldowns for scale-out and scale-in (two step policies; CDK's `scale_on_metric` has one cooldown).
9. **Endpoint scale-from-zero step policy** as documented, in addition to target tracking; the profile refuses
   `min_instances: 0` without it (a small backlog would never wake the endpoint - shown by the negative control).
10. **Fault injection** (demo of timeouts/errors) is a profile switch, on only in the sandbox; the API refuses the
    `fault` field when it is off, and the model ignores the content-type parameter unless its environment enables it.
11. **API degraded answers.** When DynamoDB or OpenSearch fail, the API answers 200 with `degraded: true` and a
    metric instead of a 5xx (project rule: downstream errors are never surfaced as 5xx or swallowed).
12. **Domain access policy** follows the documented FGAC recommendation (allow the account's principals, deny
    `_dashboards*`); authorisation is in FGAC roles mapped to exact role ARNs (writer on one index for the indexer,
    reader for the API, master only for the bootstrap Lambda).
13. **No money wording anywhere.** The evaluation compares capacity-minutes, waits and utilisation; a test fails the
    build on money words (this overrides the generic README section title from the standards; the section is
    "Prerequisites and billing dimensions" and points to each service's official billing page by name, without links).
14. **Removal policy is a parameter.** Sandbox: destroy (auto-delete objects, ECR images, no deletion protection on
    the table). Handover: retain (buckets, table with deletion protection, domain, keys, repository).
15. **Model names hashed from their inputs** (image, artifact key, environment) so a changed model gets a new
    SageMaker model and endpoint configuration and the endpoint updates in place; the destructive-change guard
    reports these replacements and requires `ALLOW_DESTRUCTIVE=1`.
16. **Standards overrides** (from the project brief): MIT-0 with the Amazon copyright line; account ID placeholder
    `111122223333`; CODEOWNERS `@shaojiez202608`; ASH runs in GitHub CI (copied job), not locally (no Docker).
