"""Interfaces between the business logic and the infrastructure (AWS adapters or local stand-ins)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Message:
    body: str
    receipt: str
    receive_count: int = 1
    sent_at_ms: int = 0
    message_id: str = ""


class Queue(Protocol):
    def receive(self, max_messages: int, wait_seconds: int) -> list[Message]: ...
    def delete(self, msg: Message) -> None: ...
    def change_visibility(self, msg: Message, seconds: int) -> None: ...
    def send(self, body: str, delay_seconds: int = 0) -> None: ...


class ObjectStore(Protocol):
    def head_size(self, bucket: str, key: str) -> int | None: ...
    def get_bounded(self, bucket: str, key: str, max_bytes: int) -> bytes: ...
    def put(self, bucket: str, key: str, data: bytes, content_type: str) -> None: ...
    def get_uri_bounded(self, uri: str, max_bytes: int) -> bytes: ...


class TooLarge(Exception):  # noqa: N818
    pass


@dataclass(frozen=True)
class AsyncInvocation:
    inference_id: str
    output_location: str


class Inference(Protocol):
    def invoke_async(self, *, inference_id: str, input_uri: str, content_type: str,
                     invocation_timeout_seconds: int) -> AsyncInvocation: ...


class TransientError(Exception):
    """Retry later (throttling, unavailable dependency). The message stays on the queue."""


@dataclass
class IndexOutcome:
    result: str  # created | updated | duplicate


class SearchIndex(Protocol):
    def put(self, doc_id: str, doc: dict[str, Any], version: int) -> IndexOutcome: ...
    def get(self, doc_id: str) -> dict[str, Any] | None: ...
    def search(self, request: Any) -> dict[str, Any]: ...


@dataclass
class Metrics:
    """Collects counters/timings; the runner flushes them as CloudWatch EMF log lines."""

    counters: dict[str, float] = field(default_factory=dict)
    timings: dict[str, list[float]] = field(default_factory=dict)

    def incr(self, name: str, value: float = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + value

    def timing(self, name: str, ms: float) -> None:
        self.timings.setdefault(name, []).append(max(0.0, ms))
