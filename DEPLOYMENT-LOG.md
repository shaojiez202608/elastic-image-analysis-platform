# Deployment log

Account: a disposable sandbox account (ID shown as 111122223333). Region: us-west-2. Date: 2026-10-21 (UTC).
Profile: `config/sandbox.json` = `config/sandbox.example.json` with `region: us-west-2` and AZs
`us-west-2a`, `us-west-2b` (sandbox-only deployable instance values unchanged).

## Region choice

Read-only checks before the first deployment: us-east-1 had no VPC quota left (all VPCs belonged to other
projects); us-west-2 had VPC quota, CDK bootstrap, Graviton and GPU SageMaker AI endpoint quotas (non-zero) and the
OpenSearch instance type for the engine version. No quota increase was requested; no account setting was changed.
The service-linked roles and the API Gateway CloudWatch Logs role already existed.

## Stacks

| Stack | Resources (CloudFormation count at the end of the run) |
|---|---|
| proto-imaging-network | 39 (VPC, 2 isolated subnets, route tables, 2 gateway + 5 interface endpoints, 5 security groups, flow log) |
| proto-imaging-storage | 40 (2 KMS keys, 3 buckets, 4 queues, 2 topics, DynamoDB table, ECR repository, CodeBuild project, log group, helper Lambdas) |
| proto-imaging-inference | 11 (role, model, endpoint configuration, endpoint, scalable target, 2 policies, alarm) |
| proto-imaging-search | 42 (OpenSearch domain, bootstrap Lambda + provider, API Gateway + Lambda, roles, log groups) |
| proto-imaging-compute | 28 (cluster, 2 task definitions, 2 services, 2 scalable targets, 4 step policies, 4 alarms, roles, log groups) |
| proto-imaging-observability | 9 (dashboard, 5 alarms, alarm topic) |

## Timeline

| Time (UTC) | Step | Result |
|---|---|---|
| 02:30 | `make deploy` (commit 9641d4d): preflight PASS, network + storage deployed, CodeBuild built image and model | ok |
| 02:4x | inference stack | **failed**: endpoint creation rejected the execution role: SageMaker AI validates `s3:ListBucket` on the asynchronous output bucket. Stack rolled back. Fix d3a80bc (ListBucket on the images bucket). |
| 02:4x-03:0x | search stack (domain, bootstrap, API) | ok; bootstrap created the index and the two FGAC roles |
| (local) | osv-scanner on uv.lock | pillow and certifi advisories -> upgrade 763c005 |
| 03:05-03:11 | redeploy (commit 50a4749): inference, compute, observability | ok; endpoint InService, model loaded (container `/ping` 200) |
| 03:11 | `scripts/e2e_cloud.py` | **failed**: API returned 502 - `Runtime.ImportModuleError` (settings.py imported a module excluded from the Lambda package). Fix 8b77879 + packaging test. |
| 03:2x | redeploy (5178007), e2e again | deny paths passed; every index write failed (RuntimeError, cause not logged). Diagnostics 155024b: log status and error type keyword. |
| 03:48 | after redeploy | writes refused with 403 `security_exception` (FGAC). Fix 62924b3 (writer role: action group `index`) - still refused. Fix e72d332: cluster permission `indices:data/write/bulk` for the writer role. |
| 04:00-04:25 | redeploy (e72d332, `ALLOW_DESTRUCTIVE=1` after the guard refused a may-replace of the bootstrap custom resource) | writes succeed |
| 04:2x | redrive of the results DLQ (`sqs start-message-move-task`) | the messages that had failed during the FGAC problem were indexed - DLQ redrive demonstrated |
| 04:26 | e2e (6d4b565 per-run tags after a polluted count) | 16/16 passed (VERIFICATION.md) |
| 04:46 | load test, elastic, burst 40, idle 900 s | 40/40 analyzed; both layers scaled out and in (VERIFICATION.md) |
| 05:14 | `set_capacity.py fixed` 2/1/2, same burst | 40/40 analyzed |
| 05:2x | `set_capacity.py elastic` (profile values restored) | ok |
| 05:2x | `make destroy` (CONFIRM_DESTROY, CONFIRM_DATA=yes) | see below |

Observed during the run, handled by design: a dispatcher scale-in alarm stayed in ALARM until the SQS metric caught
up with a new burst (metric lag); two results-queue messages not matching the notification format were acknowledged
and counted as `UnparseableMessages`; the ingest-DLQ alarm went to ALARM with the failed timeout jobs.

Scale from zero was observed twice (endpoint at 0 after idle -> `HasBacklogWithoutCapacity` step policy -> 1 instance).

## Destroy and residual check

Command: `CONFIRM_DESTROY=proto-imaging CONFIRM_DATA=yes make destroy` then `make verify-cleanup` (also run for
us-east-1, where nothing was deployed).

First pass (05:2x-06:0x UTC): all six stacks deleted (the search and network stacks waited for the Lambda VPC
network interfaces to be released); the script deleted the ECS task-definition revisions, six service-created log
groups (Container Insights, SageMaker AI endpoint, CDK helper Lambdas) and 8 bootstrap-bucket objects identified by
the saved asset manifests. **The residual check failed**: `仍有残留` - two SageMaker AI lineage contexts and two
actions (`proto-imaging-endpoint-<N>-aws-endpoint`, created automatically by SageMaker AI for each endpoint
deployment and tagged with the endpoint's tags).

Handling (commit ba9998f):
- inspected the lineage graph: 2 contexts, 2 actions and 3 artifacts (model data in the project's artifacts bucket,
  the prebuilt image by tag and by digest), all created during this run and associated only with this project's
  actions; deleted with `scripts/cleanup_lineage.py --delete` (associations first);
- the bootstrap-bucket cleanup had only the last deployment's manifests; `cleanup_bootstrap_assets.py
  --content-scan --delete` found and deleted 16 more objects of earlier deployments in this run (templates carrying
  both the Project tag value and the stack prefix; zips containing this project's handler). A second dry run
  found 0. CDK provider packages and the CDKToolkit stack were not touched;
- destroy.sh and check_residuals.py now include both steps.

Final residual check (`check_residuals.py --region us-west-2 --region us-east-1`): every category `<none>`;
KMS keys: 2 in `PendingDeletion` (7-day window, the expected end state); tagged ARNs still returned by the tagging
index were each confirmed deleted in their own service (index lag).

**Conclusion: 已清理，无残留** (KMS keys scheduled for deletion are recorded above).
