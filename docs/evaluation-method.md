# Fixed capacity vs elastic capacity: evaluation method

The question: if a fixed-size GPU/CPU cluster is replaced by queue-driven elastic capacity, what changes for
the workload? This document gives the method and the scripts. It deliberately contains no money figures:
translate capacity-minutes into your own billing terms with the official pages of each service.

## What to measure

| Measure | Why | How (script / metric) |
|---|---|---|
| Queue wait (upload -> inference start) | elasticity trades waiting time while capacity starts | `IngestQueueWaitMs` (EMF, dispatcher), endpoint `TimeInBacklog` |
| End-to-end time p50 / p90 / max | what the business sees | `items.csv`, `end_to_end_ms` in each search document |
| Makespan of a burst | how long a batch takes to clear | `summary.json: makespan_s` |
| Throughput per minute (peak) | does capacity keep up | `summary.json: throughput_peak_per_minute` |
| Provisioned capacity over the window | what is held, busy or not | `endpoint_instance_minutes`, `dispatcher_task_minutes`, `indexer_task_minutes` |
| Utilisation proxy | work per unit of provisioned capacity | `items_per_endpoint_instance_minute`; instance `CPUUtilization` / GPU utilisation |
| Cold start | time from first request to first ready capacity, per layer | `time_to_first_endpoint_instance_s`, `scaling-activities.csv` |
| Scaling activity timeline | is the policy reacting as designed | `scaling-activities.csv`, `timeline.svg` |
| Failures, retries, DLQ | elasticity must not trade correctness | dashboard "Outcomes", DLQ alarms |

## How to run it

1. Choose a load shape that represents the business pattern: a burst, a sustained rate, idle gaps. Keep it the
   smallest that still shows scaling (in a sandbox a few dozen images are enough to move both layers).
2. **Elastic run**: profile values (`python scripts/set_capacity.py elastic`), start from idle (both layers at
   their minimum), then `python scripts/loadtest.py --label elastic --burst <N> [--sustained-per-minute R --sustained-seconds S] --idle-seconds <T>`.
3. **Fixed run**: `python scripts/set_capacity.py fixed --workers W --indexers X --instances K --wait` with the
   capacity a fixed cluster would hold (the peak you would provision for), then the same `loadtest.py` command
   with `--label fixed`.
4. `python scripts/compare_runs.py out/loadtest-fixed-<UTC> out/loadtest-elastic-<UTC>` prints elastic
   relative to fixed for each measure.
5. Without an account, `python scripts/simulate.py` runs the same comparison on a discrete-time model of the two
   policies (inputs in `scripts/sim_profile.json` are illustrative choices, not measurements).

Keep everything else equal: same profile, same Region, same model artifact and image, same synthetic images
(the generator is deterministic), the same observation window rule (`--idle-seconds`). Repeat each run at least
twice; a single run shows the shape, not the variance.

## How to read the results

- Elastic should hold **less provisioned capacity** (capacity-minutes ratio below 1) when the load has idle
  periods, and **higher utilisation** (items per instance-minute above the fixed run).
- Elastic pays with **more waiting at the start of a burst** (queue wait and end-to-end p90 above fixed),
  roughly the sum of: alarm evaluation periods, cooldowns, task/instance start and model load. The
  `scaling-activities.csv` timestamps show which layer dominates.
- A sustained load near the fixed capacity makes the two converge; a load above it makes fixed fall behind
  (makespan and wait grow) while elastic adds capacity up to `max_*`.
- If the business requirement on end-to-end time is tighter than the elastic cold start, keep a minimum capacity
  (`min_instances > 0`, `min_tasks > 0`) - the method then compares "fixed peak" with "elastic above a floor".
- The evaluation includes a negative control (`tests/test_scripts.py::test_scale_from_zero_negative_control`): with
  the scale-from-zero policy removed, a small backlog never wakes the endpoint. A method that cannot show this
  failure would not be trustworthy.

## What it does not prove

Sandbox runs with synthetic images and a small CPU model show the policy behaviour and the measurement method.
They do not predict production latency, throughput or capacity for a real model, real images or real traffic.
