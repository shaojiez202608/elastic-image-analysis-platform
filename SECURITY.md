# Security

## Reporting a vulnerability

If you discover a potential security issue in this project, notify AWS Security via the
[vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/) or by email to
aws-security@amazon.com. Please do **not** create a public GitHub issue.

## What this project deploys

A prototype image analysis pipeline in one AWS account and Region: an IAM-authorised API Gateway REST API and an
ARM64 Lambda function, S3 buckets (images, artifacts, access logs), SQS queues with DLQs, SNS topics, a DynamoDB
table, ECS Fargate ARM64 services, a SageMaker AI asynchronous endpoint, an OpenSearch domain in a VPC, VPC
endpoints, KMS keys, CloudWatch dashboard/alarms, and a CodeBuild project with an ECR repository. Details:
`docs/architecture.md`.

## How data is handled

- Images are uploaded by the caller directly to S3 with a presigned POST; only synthetic images are used in this
  repository. Originals, normalised copies and inference outputs expire by lifecycle rules (profile parameters).
- Metadata and results are stored in DynamoDB (job state) and OpenSearch (search documents).
- Logs contain identifiers, reason codes and metric values only - no image content, no model output text.

## Controls (each mapped to its implementation)

| Control | Implementation |
|---|---|
| IAM authorisation and account-scoped resource policy on the API | `infra/imaging_infra/search_stack.py` |
| Presigned POST pins key, Content-Type, size | `src/imaging/uploads.py` |
| Untrusted image validation and re-encoding | `src/imaging/validation.py` |
| Untrusted model output validation | `src/imaging/model_output.py` |
| Strict search parameters and opaque cursor | `src/imaging/search_query.py` |
| Idempotent state transitions and versioned writes | `src/imaging/jobs.py`, `src/imaging/adapters/aws.py` |
| Least-privilege roles, trust conditions, key-policy conditions | `infra/imaging_infra/*_stack.py`, `trust.py`, `common.py` |
| Encryption at rest (KMS) and in transit (TLS-only policies) | `storage_stack.py`, `search_stack.py`, `inference_stack.py` |
| No public network paths (isolated subnets, VPC endpoints, no CIDR ingress) | `network_stack.py` |
| Static checks (cdk-nag) with construct-scoped, reasoned acknowledgements | `infra/imaging_infra/nag.py`, `SECURITY-NOTES.md` |
| Secret scanning with a canary, ASH in CI | `scripts/secret_scan.sh`, `.github/workflows/ci.yml` |

## Known considerations

See `SECURITY-NOTES.md` ("Known risks") and `THREAT_MODEL.md`. This is a prototype for a disposable,
non-production account.

## Hardening before production

See `docs/handover.md`, section "Production hardening checklist".

## Clean-up

`make destroy` then `make verify-cleanup`; see `docs/cleanup.md` for what is not removed automatically.
