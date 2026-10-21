"""Discrete-time model of the two scaling layers, for the local D2/D5 demos and for reasoning about
policies before spending a cloud run.

It reproduces the *shape* of the deployed policies (step scaling on queue backlog for the worker
tasks; target tracking on backlog-per-instance plus the scale-from-zero step for the endpoint;
cooldowns; start-up delay of new capacity). Service times, start-up delays and arrival rates are
inputs you choose; the output is only meaningful as a comparison between configurations run on
the same load. It is not a measurement and must not be quoted as one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Phase:
    kind: str  # burst | sustained | idle
    seconds: int
    count: int = 0  # burst: items released at the start of the phase
    per_minute: float = 0.0  # sustained: arrival rate


@dataclass(frozen=True)
class Layer:
    min_units: int
    max_units: int
    start_seconds: float  # time from "desired" to "serving" for a new unit
    scale_out_cooldown: float
    scale_in_cooldown: float


@dataclass(frozen=True)
class SimConfig:
    workers: Layer
    endpoint: Layer
    dispatch_seconds: float  # worker time per item (validate + normalise + invoke)
    inference_seconds: float  # endpoint time per item
    concurrency_per_instance: int
    worker_steps: tuple[tuple[int, int], ...]  # (backlog lower bound, +units)
    target_backlog_per_instance: float
    scale_from_zero: bool = True
    evaluation_seconds: float = 60.0
    dt: float = 1.0


@dataclass
class _Unit:
    ready_at: float
    busy_until: float = 0.0


@dataclass
class Result:
    samples: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    queue_wait: list[float] = field(default_factory=list)  # arrival -> inference start
    end_to_end: list[float] = field(default_factory=list)
    instance_seconds: float = 0.0
    busy_instance_seconds: float = 0.0
    task_seconds: float = 0.0
    makespan: float = 0.0

    def summary(self) -> dict:
        def pct(xs, p):
            if not xs:
                return None
            s = sorted(xs)
            return s[min(len(s) - 1, int(math.ceil(p / 100 * len(s))) - 1)]

        return {
            "items": len(self.end_to_end),
            "queue_wait_p50_s": pct(self.queue_wait, 50), "queue_wait_p95_s": pct(self.queue_wait, 95),
            "end_to_end_p50_s": pct(self.end_to_end, 50), "end_to_end_p95_s": pct(self.end_to_end, 95),
            "makespan_s": self.makespan,
            "instance_seconds": round(self.instance_seconds, 1), "task_seconds": round(self.task_seconds, 1),
            "inference_utilisation": round(self.busy_instance_seconds / self.instance_seconds, 3)
            if self.instance_seconds else None,
            "scaling_events": len(self.events),
        }


def arrivals(phases: list[Phase], dt: float = 1.0) -> list[float]:
    out, t = [], 0.0
    for ph in phases:
        if ph.kind == "burst":
            out += [t] * ph.count
        elif ph.kind == "sustained" and ph.per_minute > 0:
            gap = 60.0 / ph.per_minute
            k = 0
            while k * gap < ph.seconds:
                out.append(t + k * gap)
                k += 1
        t += ph.seconds
    return out


def simulate(phases: list[Phase], cfg: SimConfig, *, tail_seconds: int = 1800) -> Result:
    arr = arrivals(phases)
    horizon = sum(p.seconds for p in phases) + tail_seconds
    res = Result()
    ingest: list[tuple[int, float]] = []  # (item id, arrival)
    backlog: list[tuple[int, float]] = []  # endpoint queue
    started: dict[int, float] = {}
    tasks = [_Unit(0.0) for _ in range(cfg.workers.min_units)]
    insts = [_Unit(0.0) for _ in range(cfg.endpoint.min_units)]
    inflight_inst: list[list[tuple[float, int]]] = [[] for _ in insts]
    last_out_w = last_in_w = last_out_e = last_in_e = -1e9
    zero_breach = 0
    next_eval = cfg.evaluation_seconds
    ai, t, done = 0, 0.0, 0
    total = len(arr)
    while t <= horizon:
        while ai < total and arr[ai] <= t:
            ingest.append((ai, arr[ai]))
            ai += 1
        # workers move items from the ingest queue to the endpoint backlog
        for u in tasks:
            if u.ready_at <= t and u.busy_until <= t and ingest:
                iid, a = ingest.pop(0)
                u.busy_until = t + cfg.dispatch_seconds
                backlog.append((iid, a))
        # endpoint instances serve up to `concurrency` items each
        for idx, u in enumerate(insts):
            slots = inflight_inst[idx]
            for end, iid in [x for x in slots if x[0] <= t]:
                res.end_to_end.append(end - arr[iid])
            slots[:] = [x for x in slots if x[0] > t]
            if u.ready_at <= t:
                res.instance_seconds += cfg.dt
                if slots:
                    res.busy_instance_seconds += cfg.dt * len(slots) / cfg.concurrency_per_instance
                while len(slots) < cfg.concurrency_per_instance and backlog:
                    iid, a = backlog.pop(0)
                    started[iid] = t
                    res.queue_wait.append(t - a)
                    slots.append((t + cfg.inference_seconds, iid))
        res.task_seconds += cfg.dt * sum(1 for u in tasks if u.ready_at <= t)
        # scaling decisions once per evaluation period (one-minute alarms in the deployment)
        if t >= next_eval:
            next_eval += cfg.evaluation_seconds
            depth = len(ingest) + sum(1 for u in tasks if u.busy_until > t)
            n = len(tasks)
            add = max((c for lo, c in cfg.worker_steps if depth >= lo), default=0)
            if add and n < cfg.workers.max_units and t - last_out_w >= cfg.workers.scale_out_cooldown:
                k = min(add, cfg.workers.max_units - n)
                tasks += [_Unit(t + cfg.workers.start_seconds) for _ in range(k)]
                last_out_w = t
                res.events.append({"t": t, "layer": "workers", "from": n, "to": n + k})
            elif depth == 0 and n > cfg.workers.min_units and t - last_in_w >= cfg.workers.scale_in_cooldown \
                    and t - last_out_w >= cfg.workers.scale_out_cooldown:
                tasks.pop()
                last_in_w = t
                res.events.append({"t": t, "layer": "workers", "from": n, "to": n - 1})
            m = len(insts)
            queued = len(backlog) + sum(len(s) for s in inflight_inst)
            if m == 0:
                zero_breach = zero_breach + 1 if queued > 0 else 0
                want = 1 if (cfg.scale_from_zero and zero_breach >= 2) else (
                    math.ceil(queued / cfg.target_backlog_per_instance) if queued > cfg.target_backlog_per_instance else 0)
            else:
                want = math.ceil(queued / cfg.target_backlog_per_instance) if queued else 0
            want = max(cfg.endpoint.min_units, min(cfg.endpoint.max_units, want))
            if want > m and t - last_out_e >= cfg.endpoint.scale_out_cooldown:
                insts += [_Unit(t + cfg.endpoint.start_seconds) for _ in range(want - m)]
                inflight_inst += [[] for _ in range(want - m)]
                last_out_e = t
                res.events.append({"t": t, "layer": "endpoint", "from": m, "to": want})
            elif want < m and t - last_in_e >= cfg.endpoint.scale_in_cooldown and t - last_out_e >= cfg.endpoint.scale_in_cooldown:
                idle = [i for i, s in enumerate(inflight_inst) if not s]
                if idle:
                    i = idle[-1]
                    insts.pop(i)
                    inflight_inst.pop(i)
                    last_in_e = t
                    res.events.append({"t": t, "layer": "endpoint", "from": m, "to": m - 1})
        if int(t) % 15 == 0:
            res.samples.append({"t": t, "ingest_backlog": len(ingest), "endpoint_backlog": len(backlog),
                                "tasks": len(tasks), "instances": len(insts),
                                "instances_ready": sum(1 for u in insts if u.ready_at <= t)})
        done = len(res.end_to_end)
        if done == total and ai == total and t > sum(p.seconds for p in phases) and not insts and not tasks:
            break
        t += cfg.dt
    res.makespan = max((started[i] + cfg.inference_seconds for i in started), default=0.0)
    return res


def fixed(cfg: SimConfig, workers: int, instances: int) -> SimConfig:
    """Same policies with min = max (the fixed-capacity reference)."""
    from dataclasses import replace

    return replace(cfg, workers=replace(cfg.workers, min_units=workers, max_units=workers),
                   endpoint=replace(cfg.endpoint, min_units=instances, max_units=instances))
