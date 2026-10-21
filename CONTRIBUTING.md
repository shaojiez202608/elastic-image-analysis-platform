# Contributing

This repository is a prototype. Contributions that keep it small, testable and safe to deploy in a
disposable sandbox account are welcome.

## Workflow

1. Open an issue with the issue forms. Security issues go to
   https://aws.amazon.com/security/vulnerability-reporting/ or aws-security@amazon.com, never a public issue.
2. Fork, create a branch, one topic per pull request (squash on merge).
3. Run the checks locally (no AWS account needed): `make init lint test synth scan demo-offline`.
4. Open a pull request with the template: list the checks you ran and what you did not test.

## Tests are required

Every change comes with a test: unit tests for the pipeline (`tests/test_pipeline_local.py`,
`tests/test_validation.py`, ...), CDK assertions for infrastructure (`tests/test_infra.py`), and
script tests (`tests/test_scripts.py`). Evaluation code must keep a negative control that proves it can
report a failure (see `test_scale_from_zero_negative_control`). A change to deployment behaviour also needs
a sandbox run recorded in `VERIFICATION.md` (date, region, profile, commit, failures, what it does not prove).

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/): `feat(scope): ...`, `fix`, `docs`, `test`,
`chore`, `ci`, `refactor`. The body says what changed, why, and how it was verified.

## Rules

- Never commit credentials, account IDs, real images, real customer data or domain names. Use
  `111122223333` for account IDs, `example.com` for host names, `<UPPER_SNAKE>` for other placeholders,
  and the synthetic image generator for data. Only `config/*.example.json` is committed.
- Every cdk-nag acknowledgement is attached to a specific construct in `infra/imaging_infra/nag.py`, with a
  written reason, templated from the configuration, and listed in `SECURITY-NOTES.md`.
- Facts about AWS services link to the official documentation (`docs/architecture.md`, "Fact check").
- No money figures or wording in the repository (`tests/test_repo_hygiene.py` enforces it); no quota,
  sizing, latency or throughput figures in the documentation: describe the measurement method instead.

## AI-assisted contributions

AI tools may be used. You must be able to explain every line you submit, you are responsible for its
correctness and licensing, and the same review and test rules apply. Instructions for coding assistants
are in `AGENTS.md`.

## Licensing

By contributing you confirm that you have the right to submit the work and agree that it is licensed under
the MIT-0 license in `LICENSE`. Third-party components are listed in `THIRD-PARTY-LICENSES`.
