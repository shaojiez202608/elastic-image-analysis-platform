# Handover

For the team that takes the prototype into its own non-production account.

## What you receive

- Six CDK stacks (`proto-imaging-network|storage|inference|search|compute|observability`) driven by one profile.
- Two profile templates: `config/sandbox.example.json` (disposable account: destroy on delete, fault injection on,
  Regional IAM API) and `config/handover.example.json` (retain on delete, fault injection off, PRIVATE API, instance
  types left blank on purpose).
- Scripts: preflight, build, deploy, end-to-end checks, load test, fixed/elastic switch, comparison, simulation,
  destroy, residual check (README "Deployment" lists the commands).
- The evaluation method (`docs/evaluation-method.md`) and the open questions you must answer (`OPEN-QUESTIONS.md`).

## Profile differences

| Parameter | sandbox | handover | Note |
|---|---|---|---|
| `removal_policy` | destroy | retain | retain keeps buckets, table (deletion protection on), domain, keys, repository after `cdk destroy` |
| `inference.instance_type`, `search.instance_type` | sandbox-only deployable values | blank | choose for your Region and model; `make preflight` checks quota/availability |
| `inference.fault_injection` | true | false | the API refuses the `fault` field when false |
| `api.endpoint_type` | REGIONAL | PRIVATE | PRIVATE adds an execute-api VPC endpoint and an `aws:SourceVpce` condition; callers must be in the VPC (or connected to it) |
| `search.data_nodes` | 1 | 2 | two nodes enable zone awareness |
| log retention, lifecycle expiry | short | longer | parameters in `storage` |

To synthesize the handover profile before choosing sizes: `make synth` (uses `-c synthCheck=true`); deployment
refuses blank sizes.

## Operating notes

- **DLQ alarm fired**: ingest DLQ messages of type `failed` are jobs that exhausted inference attempts (see the job
  status via `GET /images/{id}`); other messages are poison messages. Fix the cause, then use the SQS console's
  DLQ redrive (or send a `{"type":"retry","image_id":...,"attempt":n}` message).
- **Endpoint not waking alarm**: requests queued with zero instances for 15 minutes - check the scale-from-zero
  alarm/policy and the endpoint quota.
- **Scaling tuning**: `workers.*.steps`, cooldowns, `inference.target_backlog_per_instance`,
  `max_concurrent_invocations_per_instance`; measure with `scripts/loadtest.py` after each change.
- **Model change**: replace `model/code/inference.py` and `scripts/package_model.py` (pinned revision and checksum);
  the model and endpoint configuration names are derived from their inputs, so the endpoint updates in place. The
  preflight's destructive-change guard reports the replacement; run with `ALLOW_DESTRUCTIVE=1`.

## Production hardening checklist

1. Run in a dedicated account with your landing-zone guardrails; review `THREAT_MODEL.md` with your security team.
2. Identity: decide who calls the API; keep PRIVATE endpoint or add an identity provider; add AWS WAF.
3. Network: replace the any-IPv4 HTTPS egress with prefix-list rules; restrict gateway endpoint policies to the
   project buckets and the ECR layer bucket.
4. OpenSearch: dedicated manager nodes, at least two data nodes across AZs, slow logs, snapshots; review FGAC roles.
5. Data: retention and deletion requirements (OPEN-QUESTIONS 4); S3 versioning/replication where required;
   DynamoDB backups; consider customer managed keys per data class.
6. Reliability: a sweeper for jobs stuck in `INVOKED`; redrive runbook; alarms routed to an on-call channel
   (`observability.alarm_email` or another subscriber).
7. Supply chain: rebuild the worker image regularly (security updates), keep ECR scanning and CI scanners green,
   pin the inference image by digest.
8. Model governance: licence and training-data review (OPEN-QUESTIONS 7), evaluation on your data, versioning.
9. Capacity: set `min_*`/`max_*` from measured load tests; confirm quotas with the account owner (no quota changes
   are made by the scripts).
10. Fault injection off (`inference.fault_injection: false`) - the handover template already does this.
