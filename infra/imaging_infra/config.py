"""Single parameter file -> typed, validated settings. All tunables live in config/*.json.

Validation runs at synth time, so a bad profile fails before anything is deployed. Sizing fields
(instance types) have no defaults in code: the sandbox template carries values marked as
"sandbox-only deployable values"; the handover template leaves them blank and must be filled in
(or synthesized with -c synthCheck=true for template/cdk-nag checks only).
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

RE_PROJECT = re.compile(r"^[a-z][a-z0-9-]{2,18}[a-z0-9]$")
RE_REGION = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")
RE_TAG = re.compile(r"^[A-Za-z0-9 _.:/=+@-]{1,128}$")
RE_SM_INSTANCE = re.compile(r"^ml\.[a-z][a-z0-9]*\.[a-z0-9]+$")
RE_OS_INSTANCE = re.compile(r"^[a-z][a-z0-9]*\.[a-z0-9]+\.search$")
RE_OS_VERSION = re.compile(r"^OpenSearch_\d+\.\d+$")
RE_VERSION = re.compile(r"^\d+\.\d+(\.\d+)?$")
RE_PY = re.compile(r"^py3\d{1,2}$")
RE_CATEGORY = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.IGNORECASE)
FARGATE_CPU_MEM = {256: (512, 2048), 512: (1024, 4096), 1024: (2048, 8192), 2048: (4096, 16384), 4096: (8192, 30720)}
REQUIRED_TAGS = ("Project", "Purpose", "Teardown")
SIZING_HELP = ("choose a SageMaker AI instance type that the target Region offers for endpoints (Service Quotas lists "
               "'<TYPE> for endpoint usage' per Region; make preflight checks it) and an OpenSearch instance type from "
               "https://docs.aws.amazon.com/opensearch-service/latest/developerguide/supported-instance-types.html")
SYNTH_CHECK_SM_INSTANCE = "ml.synthcheck.placeholder"
SYNTH_CHECK_OS_INSTANCE = "synthcheck.placeholder.search"


class ConfigError(ValueError):
    pass


def _req(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


@dataclass(frozen=True)
class Step:
    lower: int
    change: int


@dataclass(frozen=True)
class ServiceScaling:
    min_tasks: int
    max_tasks: int
    steps: tuple[Step, ...]
    scale_in_cooldown_seconds: int
    scale_out_cooldown_seconds: int


@dataclass(frozen=True)
class Workers:
    cpu: int
    memory_mib: int
    receive_batch: int
    dispatcher: ServiceScaling
    indexer: ServiceScaling


@dataclass(frozen=True)
class Inference:
    instance_type: str
    architecture: str  # arm64 (Graviton CPU) | x86 (CPU or GPU)
    accelerator: str  # cpu | gpu
    framework_version: str
    py_version: str
    min_instances: int
    max_instances: int
    initial_instances: int
    target_backlog_per_instance: float
    scale_in_cooldown_seconds: int
    scale_out_cooldown_seconds: int
    scale_from_zero: bool
    max_concurrent_invocations_per_instance: int
    invocation_timeout_seconds: int
    max_inference_attempts: int
    retry_base_delay_seconds: int
    network_isolation: bool
    fault_injection: bool
    fault_sleep_seconds: int
    top_k: int


@dataclass(frozen=True)
class Search:
    engine_version: str
    instance_type: str
    data_nodes: int
    volume_gib: int


@dataclass(frozen=True)
class AppConfig:
    project: str
    region: str
    availability_zones: tuple[str, ...]
    removal_policy: str
    tags: dict[str, str]
    vpc_cidr: str
    flow_logs: bool
    log_retention_days: int
    upload_expiry_days: int
    output_expiry_days: int
    max_upload_bytes: int
    presign_expires_seconds: int
    categories: tuple[str, ...]
    max_pixels: int
    min_side: int
    max_side: int
    normalize_max_side: int
    visibility_timeout_seconds: int
    max_receive_count: int
    message_retention_days: int
    workers: Workers
    inference: Inference
    search: Search
    api_endpoint_type: str
    api_throttle_rate: float
    api_throttle_burst: int
    alarm_email: str
    oldest_message_alarm_seconds: int
    failure_alarm_count: int
    metrics_namespace: str = field(default="")

    @property
    def destroy_on_delete(self) -> bool:
        return self.removal_policy == "destroy"

    def name(self, *parts: str) -> str:
        return "-".join((self.project, *parts))


def resolve_profile(path: str | Path) -> Path:
    p = Path(path)
    if p.exists():
        return p
    example = p.with_name(p.stem + ".example.json")
    hint = f"; run `make init` to copy {example}" if example.exists() else ""
    raise FileNotFoundError(f"configuration file {p} not found{hint}")


def _scaling(d: dict[str, Any], label: str) -> ServiceScaling:
    steps = tuple(Step(int(s["lower"]), int(s["change"])) for s in d["steps"])
    _req(0 <= d["min_tasks"] <= d["max_tasks"] <= 50, f"{label}: need 0 <= min_tasks <= max_tasks <= 50")
    _req(d["max_tasks"] >= 1, f"{label}: max_tasks must be >= 1")
    _req(len(steps) >= 1 and steps[0].lower >= 1, f"{label}: first scale-out step must start at >= 1 message")
    _req(all(a.lower < b.lower for a, b in zip(steps, steps[1:], strict=False)), f"{label}: steps must ascend")
    _req(all(s.change >= 1 for s in steps), f"{label}: scale-out changes must be >= 1")
    for k in ("scale_in_cooldown_seconds", "scale_out_cooldown_seconds"):
        _req(0 <= d[k] <= 3600, f"{label}: {k} out of range")
    return ServiceScaling(d["min_tasks"], d["max_tasks"], steps, d["scale_in_cooldown_seconds"],
                          d["scale_out_cooldown_seconds"])


def load(path: str | Path, *, synth_check: bool = False) -> AppConfig:
    raw = json.loads(resolve_profile(path).read_text())
    return parse(raw, synth_check=synth_check)


def parse(raw: dict[str, Any], *, synth_check: bool = False) -> AppConfig:  # noqa: C901 - one flat validator
    try:
        net, st, up, lim, q, w, inf, se, api, obs = (raw[k] for k in (
            "network", "storage", "upload", "image_limits", "queues", "workers", "inference", "search", "api",
            "observability"))
    except KeyError as e:
        raise ConfigError(f"missing section {e}") from None
    _req(bool(RE_PROJECT.match(raw.get("project", ""))), "project must match ^[a-z][a-z0-9-]{2,18}[a-z0-9]$")
    _req(raw["project"].startswith("proto-"), "project prefix must start with proto-")
    _req(bool(RE_REGION.match(raw.get("region", ""))), "region invalid")
    azs = tuple(net["availability_zones"])
    _req(len(azs) == 2 and all(a.startswith(raw["region"]) for a in azs) and len(set(azs)) == 2,
         "network.availability_zones: exactly two distinct AZs of the region (fixed so synth needs no AWS calls)")
    _req(raw.get("removal_policy") in ("destroy", "retain"), "removal_policy must be destroy or retain")
    tags = dict(raw.get("tags", {}))
    _req(all(k in tags for k in REQUIRED_TAGS), f"tags must include {REQUIRED_TAGS}")
    _req(all(RE_TAG.match(k) and RE_TAG.match(v) for k, v in tags.items()), "tag keys/values invalid")
    try:
        cidr = ipaddress.ip_network(net["vpc_cidr"])
    except ValueError:
        raise ConfigError("network.vpc_cidr invalid") from None
    _req(cidr.prefixlen <= 20 and cidr.is_private, "network.vpc_cidr must be a private /20 or larger")
    _req(st["log_retention_days"] in (1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365), "log_retention_days invalid")
    _req(1 <= st["upload_expiry_days"] <= 3650 and 1 <= st["output_expiry_days"] <= 3650, "expiry days out of range")
    _req(1024 <= up["max_upload_bytes"] <= 1024 ** 3, "upload.max_upload_bytes out of range (<= 1 GiB async payload)")
    _req(60 <= up["presign_expires_seconds"] <= 3600, "upload.presign_expires_seconds out of range")
    cats = tuple(up["categories"])
    _req(1 <= len(cats) <= 20 and all(RE_CATEGORY.match(c) for c in cats) and "general" in cats,
         "upload.categories: 1-20 lowercase names including 'general'")
    _req(lim["max_pixels"] > 0 and 1 <= lim["min_side"] <= lim["max_side"] and lim["normalize_max_side"] >= 224,
         "image_limits invalid (normalize_max_side must be >= 224 for the model's crop)")
    _req(30 <= q["visibility_timeout_seconds"] <= 43200, "queues.visibility_timeout_seconds out of range")
    _req(2 <= q["max_receive_count"] <= 1000, "queues.max_receive_count out of range")
    _req(1 <= q["message_retention_days"] <= 14, "queues.message_retention_days 1-14")
    _req(w["cpu"] in FARGATE_CPU_MEM, "workers.cpu must be a Fargate CPU value")
    lo, hi = FARGATE_CPU_MEM[w["cpu"]]
    _req(lo <= w["memory_mib"] <= hi and w["memory_mib"] % 1024 == 0, "workers.memory_mib invalid for workers.cpu")
    _req(1 <= w["receive_batch"] <= 10, "workers.receive_batch 1-10")
    workers = Workers(w["cpu"], w["memory_mib"], w["receive_batch"], _scaling(w["dispatcher"], "dispatcher"),
                      _scaling(w["indexer"], "indexer"))

    sm_type = inf.get("instance_type") or (SYNTH_CHECK_SM_INSTANCE if synth_check else "")
    _req(bool(sm_type), "inference.instance_type is empty: " + SIZING_HELP)
    _req(sm_type == SYNTH_CHECK_SM_INSTANCE or bool(RE_SM_INSTANCE.match(sm_type)), "inference.instance_type invalid")
    _req(inf["architecture"] in ("arm64", "x86"), "inference.architecture must be arm64 or x86")
    _req(inf["accelerator"] in ("cpu", "gpu"), "inference.accelerator must be cpu or gpu")
    _req(not (inf["architecture"] == "arm64" and inf["accelerator"] == "gpu"),
         "arm64 (Graviton) inference images are CPU-only; use architecture x86 for GPU instances")
    _req(bool(RE_VERSION.match(inf["framework_version"])) and bool(RE_PY.match(inf["py_version"])),
         "inference framework/py version invalid")
    _req(0 <= inf["min_instances"] <= inf["max_instances"] <= 20 and inf["max_instances"] >= 1,
         "inference: need 0 <= min_instances <= max_instances <= 20")
    _req(1 <= inf["initial_instances"] <= inf["max_instances"], "inference.initial_instances must be 1..max")
    _req(not (inf["min_instances"] == 0 and not inf["scale_from_zero"]),
         "min_instances 0 needs scale_from_zero (otherwise a small backlog never wakes the endpoint)")
    _req(inf["target_backlog_per_instance"] > 0, "inference.target_backlog_per_instance must be > 0")
    _req(1 <= inf["max_concurrent_invocations_per_instance"] <= 1000, "max_concurrent_invocations_per_instance 1-1000")
    _req(1 <= inf["invocation_timeout_seconds"] <= 3600, "invocation_timeout_seconds 1-3600 (API range)")
    _req(1 <= inf["max_inference_attempts"] <= 10, "max_inference_attempts 1-10")
    _req(1 <= inf["retry_base_delay_seconds"] <= 900, "retry_base_delay_seconds 1-900 (SQS max delay)")
    _req(inf["fault_sleep_seconds"] > inf["invocation_timeout_seconds"] or not inf["fault_injection"],
         "fault_sleep_seconds must exceed invocation_timeout_seconds to demonstrate a timeout")
    _req(1 <= inf["top_k"] <= 10, "inference.top_k 1-10")
    _req(q["visibility_timeout_seconds"] >= 60, "visibility timeout must cover one dispatch")

    os_type = se.get("instance_type") or (SYNTH_CHECK_OS_INSTANCE if synth_check else "")
    _req(bool(os_type), "search.instance_type is empty: " + SIZING_HELP)
    _req(os_type == SYNTH_CHECK_OS_INSTANCE or bool(RE_OS_INSTANCE.match(os_type)), "search.instance_type invalid")
    _req(bool(RE_OS_VERSION.match(se["engine_version"])), "search.engine_version must look like OpenSearch_2.19")
    _req(1 <= se["data_nodes"] <= 10 and 10 <= se["volume_gib"] <= 1024, "search sizing out of range")

    _req(api["endpoint_type"] in ("REGIONAL", "PRIVATE"), "api.endpoint_type must be REGIONAL or PRIVATE")
    _req(api["throttle_rate"] > 0 and api["throttle_burst"] >= 1, "api throttling must be positive")
    _req(obs["alarm_email"] == "" or bool(RE_EMAIL.match(obs["alarm_email"])), "observability.alarm_email invalid")
    _req(obs["oldest_message_alarm_seconds"] >= 60 and obs["failure_alarm_count"] >= 1, "alarm thresholds invalid")

    return AppConfig(
        project=raw["project"], region=raw["region"], availability_zones=azs, removal_policy=raw["removal_policy"],
        tags=tags, vpc_cidr=str(cidr), flow_logs=bool(net["flow_logs"]), log_retention_days=st["log_retention_days"],
        upload_expiry_days=st["upload_expiry_days"], output_expiry_days=st["output_expiry_days"],
        max_upload_bytes=up["max_upload_bytes"], presign_expires_seconds=up["presign_expires_seconds"],
        categories=cats, max_pixels=lim["max_pixels"], min_side=lim["min_side"], max_side=lim["max_side"],
        normalize_max_side=lim["normalize_max_side"], visibility_timeout_seconds=q["visibility_timeout_seconds"],
        max_receive_count=q["max_receive_count"], message_retention_days=q["message_retention_days"],
        workers=workers,
        inference=Inference(sm_type, inf["architecture"], inf["accelerator"], inf["framework_version"],
                            inf["py_version"], inf["min_instances"], inf["max_instances"], inf["initial_instances"],
                            float(inf["target_backlog_per_instance"]), inf["scale_in_cooldown_seconds"],
                            inf["scale_out_cooldown_seconds"], bool(inf["scale_from_zero"]),
                            inf["max_concurrent_invocations_per_instance"], inf["invocation_timeout_seconds"],
                            inf["max_inference_attempts"], inf["retry_base_delay_seconds"],
                            bool(inf["network_isolation"]), bool(inf["fault_injection"]),
                            inf["fault_sleep_seconds"], inf["top_k"]),
        search=Search(se["engine_version"], os_type, se["data_nodes"], se["volume_gib"]),
        api_endpoint_type=api["endpoint_type"], api_throttle_rate=float(api["throttle_rate"]),
        api_throttle_burst=int(api["throttle_burst"]), alarm_email=obs["alarm_email"],
        oldest_message_alarm_seconds=obs["oldest_message_alarm_seconds"],
        failure_alarm_count=obs["failure_alarm_count"], metrics_namespace=raw["project"],
    )


def with_overrides(cfg: AppConfig, **kw: Any) -> AppConfig:
    return replace(cfg, **kw)
