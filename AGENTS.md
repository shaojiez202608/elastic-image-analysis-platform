# AGENTS.md

Instructions for AI coding assistants working in this repository.

## Role

You maintain a **prototype** (not production): an elastic image analysis pipeline on AWS (S3 presigned
upload -> SQS -> ECS Fargate ARM64 workers -> SageMaker AI asynchronous inference -> OpenSearch), built
with AWS CDK (Python), plus its load-test and fixed-vs-elastic evaluation method. Keep changes small,
tested and safe for a disposable sandbox account.

## Common commands

| Task | Command |
|---|---|
| Create local profiles from the templates | `make init` |
| Lint / tests | `make lint` / `make test` |
| Offline demo (no AWS) | `make demo-offline` |
| Synthesize both templates with cdk-nag | `make synth` |
| Secret scan (full history + canary) | `make scan` |
| Read-only pre-deploy checks | `make preflight` |
| Deploy / verify / load test / destroy / residual check | `make deploy` / `make verify` / `make loadtest` / `make destroy` / `make verify-cleanup` |

## Git workflow

- Conventional Commits with a body (what, why, how verified); end with the co-author line your tool requires.
- Never push to a remote, never rewrite published history, never commit `config/*.json` (only `*.example.json`).

## Boundaries

**Always**
- Run `make lint test synth` before proposing a commit; add tests with every change.
- Use placeholders: `111122223333`, `example.com`, `<UPPER_SNAKE>`; synthetic images only.
- Keep cdk-nag acknowledgements construct-scoped, templated from config, with a reason; update `SECURITY-NOTES.md`.
- Redact 12-digit numbers before writing command output into any file.

**Ask first**
- Any `cdk deploy`, `cdk destroy`, load test, or command that creates, changes or deletes AWS resources.
- Changing IAM policies, trust policies, key policies, security groups, the OpenSearch access model or `nag.py`.
- Changing CI workflows, `.gitleaks.toml`, `CODEOWNERS` or this file.
- Adding dependencies, base images or models.

**Never**
- Commit credentials, account IDs, images, model weights or customer data.
- Request quota increases, modify account-level settings, the CDKToolkit stack, or resources this project did not create.
- Add money figures or wording; disable a security control or a test to make a change pass.
