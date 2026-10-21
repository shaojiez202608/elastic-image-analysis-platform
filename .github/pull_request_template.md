## What and why

<!-- What changed, why, and which issue it addresses. -->

## Checks run

- [ ] `make lint`
- [ ] `make test`
- [ ] `make demo-offline`
- [ ] `make synth` (both profiles, cdk-nag)
- [ ] `make scan` (gitleaks + canary)
- [ ] Deployed to a sandbox and ran `make verify` / `make loadtest` / `make verify-cleanup` (say which commit)

## Not tested

<!-- List what you did not run or could not verify, and why. -->

## Hygiene

- [ ] No account IDs (use 111122223333), credentials, real images, customer data or domains (use example.com)
- [ ] No money figures or wording; no quota/latency/throughput figures in docs (methods only)
- [ ] New cdk-nag acknowledgements have a reason in `infra/imaging_infra/nag.py` and `SECURITY-NOTES.md`
- [ ] Commit messages follow Conventional Commits
