# Demo script

Audience: architects and engineering leads evaluating "fixed GPU/CPU cluster vs elastic managed inference" for an
image analysis platform. All data is synthetic; no customer information appears at any point.

## Versions

| Version | Content |
|---|---|
| 10 minutes | architecture slide (docs/architecture.md diagram) -> D1 -> D3 (show the prepared e2e record) -> D2 timeline from a prepared load-test run -> D5 table |
| 30 minutes | all steps below live, including a fresh load test, the dashboard, and the questions in OPEN-QUESTIONS.md |

## Preparation checklist

- [ ] Deployed sandbox (README "Deployment"), `make verify` passed once (it also warms the endpoint).
- [ ] A prepared load-test run in `out/loadtest-elastic-*` (and `out/loadtest-fixed-*` for D5) - the live run takes
      tens of minutes because scale-in is slow by design.
- [ ] CloudWatch dashboard `proto-imaging-dashboard` open; time range "last 1 hour".
- [ ] Offline fallback ready: `make demo-offline` run once, `out/local-demo/` present.
- [ ] Decide whether the endpoint should start from zero (cold start visible) or warm (wait less).

## One-command start and reset

- Start: `scripts/demo.sh cloud` (end-to-end checks + a minimal burst load test) or `scripts/demo.sh offline`.
- Reset: `scripts/demo.sh reset` removes local outputs; cloud state is reset with `make destroy` + `make deploy`
  (search documents are per image ID and tagged, so repeated runs do not collide; use a new `--label`).

## Steps

| # | Action | Expected | Talking points | If it fails |
|---|---|---|---|---|
| D1 | `uv run python scripts/e2e_cloud.py` (first half) or upload a few images with `scripts/loadtest.py --burst 5 --idle-seconds 0` | ingest queue shows depth, dispatcher scales from 0, endpoint starts (from zero if idle), documents appear in `GET /search?status=analyzed` | presigned POST (S3 enforces key/type/size); workers are ARM64 Fargate; the endpoint has its own queue; results via SNS | endpoint still `Creating`: show the offline path; check `HasBacklogWithoutCapacity` alarm |
| D2 | `scripts/loadtest.py --label elastic --burst 40 --idle-seconds 600` | queue depth rises; dispatcher tasks scale out first; endpoint instances follow; after the load, tasks scale in within minutes, the endpoint later; `timeline.svg` and `scaling-activities.csv` written | two layers, two metrics (queue backlog vs backlog per instance); the order of reactions; cold-start contributors | run too slow for the slot: open a prepared `out/loadtest-*/timeline.svg` |
| D3 | e2e checks for bad files, `fault=timeout`, `fault=error_once`, replayed events | bad files `REJECTED` with reason (never reach the model, never in the DLQ); timeout retried then `FAILED` and in the DLQ; error_once `DONE` on attempt 2; replays do not change `processed_at` | permanent vs transient failures; idempotency by conditional transitions + versioned documents; good files are never blocked | show `tests/test_pipeline_local.py` running locally (`uv run pytest tests/test_pipeline_local.py -q`) |
| D4 | `GET /search?tag=e2e&size=4` then follow `next_cursor`; `?status=rejected`; `?top1_class=<N>` | correct totals; each image exactly once across pages | strict parameters, opaque cursor, FGAC read-only role | use the offline demo output (D4 section) |
| D5 | `scripts/set_capacity.py fixed --workers 2 --indexers 1 --instances 2 --wait`, same load test with `--label fixed`, `scripts/compare_runs.py <fixed> <elastic>`; restore with `set_capacity.py elastic` | table of elastic relative to fixed: less capacity-minutes, higher utilisation, more wait at the burst start | method, not a verdict; what to measure with real data; translate capacity-minutes with the official billing pages yourself | show `out/local-demo/sim/comparison.md` (simulation) |

## Offline fallback (no network or account)

`scripts/demo.sh offline` runs D1, D3, D4 on the local stand-ins and D2/D5 on the simulation, printing a summary
and writing `out/local-demo/sim/timeline-elastic.svg` and `comparison.md`. Explain that the local mode reproduces
semantics (retries, DLQ, idempotency, search) and the simulation reproduces the policy shape, not real timings.

## After the demo

`make destroy` then `make verify-cleanup`; check the conclusion line (docs/cleanup.md).
