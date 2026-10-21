"""Queue consumer loop shared by the dispatcher and the indexer services.

- Long-polls the queue, hands each message to the handler, deletes it on ACK.
- On any exception the message stays on the queue with a backoff visibility timeout based on its
  receive count; after maxReceiveCount deliveries SQS moves it to the DLQ (redrive policy).
- Only the exception class name is logged (no message bodies, no exception text).
- Metrics are flushed as CloudWatch Embedded Metric Format lines on stdout (awslogs driver).
- SIGTERM (ECS scale-in / deployment) stops polling after the current batch.
"""

from __future__ import annotations

import json
import logging
import signal
import time
from collections.abc import Callable

from .ports import Message, Metrics, Queue

log = logging.getLogger("imaging")


def backoff_seconds(receive_count: int, base: int = 10, cap: int = 900) -> int:
    return int(min(cap, base * 2 ** max(0, receive_count - 1)))


def emf_line(namespace: str, service: str, m: Metrics, ts_ms: int) -> str | None:
    if not m.counters and not m.timings:
        return None
    metrics = [{"Name": k, "Unit": "Count"} for k in sorted(m.counters)]
    metrics += [{"Name": k, "Unit": "Milliseconds"} for k in sorted(m.timings)]
    doc = {"_aws": {"Timestamp": ts_ms, "CloudWatchMetrics": [
        {"Namespace": namespace, "Dimensions": [["Service"]], "Metrics": metrics}]}, "Service": service}
    doc.update(m.counters)
    # EMF accepts a value array per metric.
    doc.update({k: v[:100] for k, v in m.timings.items()})
    return json.dumps(doc)


class Runner:
    def __init__(self, queue: Queue, handler: Callable[[Message], str], *, service: str, namespace: str,
                 metrics: Metrics, batch: int = 10, wait_seconds: int = 20, base_backoff: int = 10,
                 emit: Callable[[str], None] = print):
        self.queue, self.handler, self.service, self.namespace = queue, handler, service, namespace
        self.metrics, self.batch, self.wait, self.base_backoff, self.emit = metrics, batch, wait_seconds, base_backoff, emit
        self.stopping = False

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopping", True))

    def run_once(self) -> int:
        msgs = self.queue.receive(self.batch, self.wait)
        for msg in msgs:
            started = time.monotonic()
            try:
                self.handler(msg)
                self.queue.delete(msg)
                self.metrics.incr("MessagesProcessed")
            except Exception as exc:  # noqa: BLE001 - every failure becomes a delayed redelivery
                delay = backoff_seconds(msg.receive_count, self.base_backoff)
                log.warning(json.dumps({"event": "message_retry", "error": type(exc).__name__,
                                        "receive_count": msg.receive_count, "delay_s": delay}))
                self.metrics.incr("MessageErrors")
                try:
                    self.queue.change_visibility(msg, delay)
                except Exception as exc2:  # noqa: BLE001
                    log.warning(json.dumps({"event": "visibility_error", "error": type(exc2).__name__}))
            finally:
                self.metrics.timing("HandlerMs", (time.monotonic() - started) * 1000)
        self.flush()
        return len(msgs)

    def flush(self) -> None:
        line = emf_line(self.namespace, self.service, self.metrics, int(time.time() * 1000))
        if line:
            self.emit(line)
        self.metrics.counters.clear()
        self.metrics.timings.clear()

    def run_forever(self) -> None:
        self.install_signal_handlers()
        while not self.stopping:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - e.g. receive failed; keep the service alive
                log.warning(json.dumps({"event": "receive_error", "error": type(exc).__name__}))
                time.sleep(5)
