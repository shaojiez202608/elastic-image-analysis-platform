"""Job state with conditional transitions (the idempotency backbone).

States:
    PENDING -> DISPATCHING -> INVOKED -> DONE
                    |            |-> RETRY_SCHEDULED -> DISPATCHING (next attempt)
                    |            '-> FAILED (attempts exhausted)
                    '-> REJECTED (bad file, permanent)

Every transition is conditional on the current status (and attempt), so a duplicate SQS delivery,
a duplicate S3 event or a duplicate SNS notification cannot move a job twice. DISPATCHING carries a
lease: a worker that crashed between claiming and invoking leaves a lease that expires, after which
the redelivered message can claim the job again.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

PENDING, DISPATCHING, INVOKED, RETRY_SCHEDULED = "PENDING", "DISPATCHING", "INVOKED", "RETRY_SCHEDULED"
DONE, REJECTED, FAILED = "DONE", "REJECTED", "FAILED"
FINAL = (DONE, REJECTED, FAILED)
STATES = (PENDING, DISPATCHING, INVOKED, RETRY_SCHEDULED, *FINAL)


@dataclass(frozen=True)
class Job:
    image_id: str
    status: str
    attempt: int = 0
    category: str = "general"
    tags: tuple[str, ...] = ()
    content_type: str = ""
    filename: str = ""
    uploaded_at: str = ""
    fault: str = ""
    lease_until: float = 0.0
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    reason: str = ""
    source: str = "api"

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> Job:
        known = {k: item[k] for k in cls.__dataclass_fields__ if k in item}
        for k in ("attempt", "width", "height", "size_bytes"):
            if k in known:
                known[k] = int(known[k])
        if "lease_until" in known:
            known["lease_until"] = float(known["lease_until"])
        if "tags" in known:
            known["tags"] = tuple(sorted(known["tags"]))
        return cls(**known)

    def to_item(self) -> dict[str, Any]:
        item = {k: getattr(self, k) for k in self.__dataclass_fields__}
        item["tags"] = list(self.tags)
        return item


class JobStore(Protocol):
    def get(self, image_id: str) -> Job | None: ...
    def create(self, job: Job) -> bool: ...
    def transition(self, image_id: str, *, expect: Iterable[str], to: str, expect_attempt: int | None = None,
                   lease_expired_before: float | None = None, **fields: Any) -> bool: ...


class MemoryJobStore:
    """In-process implementation with the same conditional semantics (local mode and tests)."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, image_id: str) -> Job | None:
        with self._lock:
            item = self._items.get(image_id)
            return Job.from_item(copy.deepcopy(item)) if item else None

    def create(self, job: Job) -> bool:
        with self._lock:
            if job.image_id in self._items:
                return False
            self._items[job.image_id] = job.to_item()
            return True

    def transition(self, image_id, *, expect, to, expect_attempt=None, lease_expired_before=None, **fields) -> bool:
        if to not in STATES:
            raise ValueError(f"unknown state {to}")
        with self._lock:
            item = self._items.get(image_id)
            if item is None or not _allowed(item, set(expect), expect_attempt, lease_expired_before):
                return False
            item.update(fields, status=to)
            if "tags" in fields:
                item["tags"] = list(fields["tags"])
            return True

    def all(self) -> list[Job]:
        with self._lock:
            return [Job.from_item(copy.deepcopy(i)) for i in self._items.values()]


def _allowed(item: dict, expect: set[str], expect_attempt: int | None, lease_expired_before: float | None) -> bool:
    status = item["status"]
    if status == DISPATCHING and DISPATCHING in expect and lease_expired_before is not None:
        if float(item.get("lease_until", 0)) >= lease_expired_before:
            return False
    elif status not in expect:
        return False
    return expect_attempt is None or int(item.get("attempt", 0)) == expect_attempt


class DynamoJobStore:
    """DynamoDB implementation: ConditionExpression on status/attempt/lease (one item per image)."""

    def __init__(self, table, now: Callable[[], float] | None = None):
        self.table = table  # boto3 Table resource
        self._now = now

    def get(self, image_id: str) -> Job | None:
        item = self.table.get_item(Key={"image_id": image_id}, ConsistentRead=True).get("Item")
        return Job.from_item(item) if item else None

    def create(self, job: Job) -> bool:
        from botocore.exceptions import ClientError

        item = _to_dynamo(job.to_item())
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(image_id)")
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def transition(self, image_id, *, expect, to, expect_attempt=None, lease_expired_before=None, **fields) -> bool:
        from botocore.exceptions import ClientError

        if to not in STATES:
            raise ValueError(f"unknown state {to}")
        expect = sorted(set(expect))
        names = {"#s": "status"}
        values: dict[str, Any] = {":to": to}
        sets = ["#s = :to"]
        for i, (k, v) in enumerate(sorted(fields.items())):
            names[f"#f{i}"] = k
            values[f":f{i}"] = _to_dynamo(list(v) if k == "tags" else v)
            sets.append(f"#f{i} = :f{i}")
        plain = [s for s in expect if not (s == DISPATCHING and lease_expired_before is not None)]
        conds = []
        if plain:
            keys = []
            for i, s in enumerate(plain):
                values[f":e{i}"] = s
                keys.append(f":e{i}")
            conds.append(f"#s IN ({', '.join(keys)})")
        if DISPATCHING in expect and lease_expired_before is not None:
            values[":disp"] = DISPATCHING
            values[":lease"] = _to_dynamo(lease_expired_before)
            names["#l"] = "lease_until"
            conds.append("(#s = :disp AND #l < :lease)")
        cond = "(" + " OR ".join(conds) + ")"
        if expect_attempt is not None:
            names["#a"] = "attempt"
            values[":ea"] = expect_attempt
            cond += " AND #a = :ea"
        try:
            self.table.update_item(
                Key={"image_id": image_id}, UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression=f"attribute_exists(image_id) AND {cond}",
                ExpressionAttributeNames=names, ExpressionAttributeValues=values)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise


def _to_dynamo(v: Any) -> Any:
    from decimal import Decimal

    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: _to_dynamo(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_to_dynamo(x) for x in v]
    return v
