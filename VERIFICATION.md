# Verification

What was actually run, with results. Every record states date, Region, profile, commit, failures and what the
result does not prove. Account IDs are replaced by 111122223333.

## Local checks (2026-10-21, no AWS)

| Check | Command | Result |
|---|---|---|
| Lint | `make lint` (ruff + `bash -n` on scripts) | pass |
| Unit, integration, CDK assertion, script and hygiene tests | `uv run pytest` | 145 passed, 1 skipped (the real-model test needs `MODEL_WEIGHTS`) |
| Real model through the handler and the output validator | `MODEL_WEIGHTS=<PATH> uv run --group model pytest tests/test_scripts.py -k real_model` | 1 passed (strict state_dict load, output accepted by the indexer validator) |
| Offline demo D1-D5 | `make demo-offline` | `RESULT: PASS` (D1 20/20 done; D3 6/6 bad files rejected with the expected reason, timeout -> FAILED after 3 attempts and in the DLQ, error_once -> DONE on attempt 2, replay -> no extra write; D4 3 pages, 20 unique; D2/D5 simulation timeline and table) |
| Synth + cdk-nag, sandbox template | `npx -y aws-cdk@2 synth -c config=config/sandbox.example.json` | 0 unacknowledged findings |
| Synth + cdk-nag, handover template | `... -c config=config/handover.example.json -c synthCheck=true` | 0 unacknowledged findings |
| Synth with another prefix/Region (acknowledgements are templated) | `tests/test_infra.py::test_nag_acknowledgements_follow_the_configured_prefix` | pass |
| Secret scan, full history, plus canary | `make scan` (gitleaks 8.x) | no leaks; canary detected (rule aws-access-token) |
| Workflow lint | `uvx zizmor .github/workflows` | no findings |
| Dependency vulnerabilities | `osv-scanner scan source --lockfile uv.lock` | first run: pillow 12.3.0 and certifi 2026.7.22 High/Critical -> upgraded (commit 763c005); now "No issues found" |
| Python security lint | `uvx bandit -r src scripts infra model -l` | no issues (urlopen calls on fixed/checked https URLs marked with reasons) |
| ASH | not run locally: no Docker on this machine (ASH container mode). The CI job `ash` (copied from the reference workflow that ran all scanners on GitHub) is the place where ASH runs. **Not yet run on GitHub** - to be confirmed after push. |

Negative controls (evaluation must be able to fail): `test_scale_from_zero_negative_control` (without the step
policy a small backlog never wakes the endpoint), `test_the_guard_detects_a_planted_example` (hygiene patterns
fire on planted words), secret-scan canary, invalid model outputs in `test_model_output.py` and
`test_invalid_model_output_is_recorded_not_indexed_raw`.

## Repository self-check (final commit)

- forbidden-term scan over `git log --all -p` (the term list of the project brief): 2 hits, both the same line of
  `tests/test_repo_hygiene.py` (a local-path guard pattern) added in 429f019 and removed in 2757818; the working tree
  is clean. History was not rewritten (project rule); see the hand-over notes.
- authors/committers: one identity; all commit subjects are Conventional Commits; no 12-digit values in the working
  tree other than 111122223333 (a placeholder-shaped example ID from a test literal remains in history: 429f019,
  removed in 3fb8571).

## Cloud verification (2026-10-21, us-west-2, profile `config/sandbox.json` = sandbox template with Region us-west-2)

Region note: us-east-1 (template default) had no VPC quota left; see DEPLOYMENT-LOG.md.

Deployed artifacts: worker image and Lambda/infra code from commit `e72d332`. Later commits changed only client-side
scripts and tests (`6d4b565` e2e per-run tags, `9749b7d` comment-only nosec markers, `bcfed64` comparison window,
`3fb8571` test literal, `ba9998f` lineage/bootstrap cleanup in destroy and the residual check) and documentation;
`src/`, `infra/`, `model/`, `docker/` and `buildspec/` are unchanged after `e72d332` except comments. The ops-script
changes were exercised against the same sandbox (comparison on the recorded runs, lineage and bootstrap cleanup,
residual check); the stacks were not redeployed after `e72d332`.

### End-to-end (`scripts/e2e_cloud.py`, record `out/e2e-20261021T043606Z.json`, commit 6d4b565): 16/16 passed

| Check | Result |
|---|---|
| unsigned API request | 403 |
| upload request with a disallowed type | 400 (API Gateway model validation) |
| search with an invalid enum | 400 (handler validation) |
| presigned POST with a body larger than the declared size | 400 from S3 (content-length-range enforced) |
| presigned POST with a changed key | 403 from S3 |
| presigned POST with a changed Content-Type | 403 from S3 |
| D1: 6 synthetic images | all `DONE`; documents carry metadata and top-k results |
| D3: corrupt, text-as-JPEG, decompression bomb, PNG declared as JPEG | `REJECTED` with the expected reason codes |
| D3: `fault=error_once` | `DONE` on attempt 2 |
| D3: `fault=timeout` | retried, `FAILED` after 3 attempts (reason `timeout`), record in the ingest DLQ |
| D3: replayed S3 event + replayed completion notification | document unchanged (`processed_at` identical) |
| D4: tag filter with cursor paging (size 4) | each image exactly once, total matches |
| D4: status filter, result-field filter (`top1_class`) | correct |

Earlier runs of the same script (before the fixes listed in DEPLOYMENT-LOG.md) failed: 502 from the API
(ImportModuleError), all document writes 403 (FGAC), and a search count polluted by an earlier run's tags.

### D2 load test (elastic), run `elastic-20261021T044629Z`, commit e72d332 deployed

`scripts/loadtest.py --label elastic --burst 40 --idle-seconds 900`, starting from idle (dispatcher and indexer at
0 tasks, endpoint scaled in to 0 instances). 40/40 images analyzed. Order of scaling activities recorded in
`scaling-activities.csv` (log summary; absolute durations are deliberately not reproduced here):

1. ingest queue rises to the burst size; dispatcher scales out 0 -> 2 -> 3 tasks (step policy, scale-out cooldown);
2. the ingest queue drains into the endpoint's internal queue; dispatcher scales in 3 -> 2 -> 1 -> 0;
3. endpoint scales out from zero 0 -> 1 through the `HasBacklogWithoutCapacity` step policy - this was the largest
   single contributor to the end-to-end time of the burst;
4. results queue fills; indexer scales 0 -> 1 -> 2;
5. target tracking then raises the endpoint 1 -> 2 although the backlog was already almost processed (metric lag);
6. indexer scales back to 0; the endpoint stayed at 2 for the rest of the observed idle window (target-tracking
   scale-in is slower than the observation window).

The timeline (`timeline.svg`, `timeline.csv`) stays in `out/` (git-ignored) and is regenerated by the script.

### D5 fixed vs elastic, runs `fixed-20261021T051419Z` vs `elastic-20261021T044629Z`

Fixed reference: `set_capacity.py fixed --workers 2 --indexers 1 --instances 2 --wait` (all capacity in service
before the burst), same 40-image burst. `compare_runs.py` (commit bcfed64), elastic relative to fixed:

| Measure | Elastic relative to fixed |
|---|---|
| end-to-end p50 / p90 | 49.76x / 38.19x (elastic waits for capacity to start, mostly the endpoint from zero) |
| makespan of the burst | 12.09x |
| endpoint instance-minutes over the same window | 0.56x |
| dispatcher task-minutes / indexer task-minutes over the same window | 0.36x / 0.50x |
| items per endpoint instance-minute over the same window | 1.80x |

Reading: with a burst followed by idle time, elastic capacity held about half the inference capacity and did more
work per provisioned instance-minute, in exchange for a much longer wait for the first burst after idle. With a
floor (`min_instances: 1`) the wait would shrink and the capacity ratio would rise - measure it the same way. The
window was one burst plus idle; a longer idle period lowers the elastic capacity ratio further, a sustained load
moves both runs closer. Capacity was restored to the profile values afterwards (`set_capacity.py elastic`).

### What these results do not prove

- Performance or capacity of a real model on real images; the sandbox uses a small CPU model and synthetic images.
- Behaviour under sustained high load, multi-AZ failures, or quotas of other accounts/Regions.
- The handover profile was synthesized and checked with cdk-nag but not deployed.
- ASH completeness (GitHub CI, not yet run).
- Statistical spread: each load pattern ran once; the ratios show direction and order of magnitude, not variance.
