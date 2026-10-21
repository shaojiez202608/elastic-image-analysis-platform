# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-21

### Added
- Upload -> queue -> ARM64 workers -> SageMaker AI asynchronous inference -> OpenSearch pipeline with
  presigned uploads, validation and normalisation of untrusted images, and an IAM-authorised query API.
- Two-layer queue-driven scaling (ECS step scaling on queue backlog; endpoint target tracking on
  backlog per instance plus scale-out from zero), all parameters in one profile.
- Failure handling: retries with backoff, DLQs on both queues, bounded re-invocation, idempotent writes,
  bad files recorded as rejected.
- Local mode (in-process stand-ins), offline demo, discrete-time simulation, load test with scaling
  timeline, fixed-vs-elastic comparison method.
- Six CDK stacks (network, storage/queues, inference, search, compute, observability) with cdk-nag,
  confused-deputy trust conditions, dashboard and alarms.
- Deploy/verify/destroy scripts with preflight, destructive-change guard and residual check.
