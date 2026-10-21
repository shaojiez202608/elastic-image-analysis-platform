"""ECS entry point: `python -m imaging.worker_main dispatcher|indexer`."""

from __future__ import annotations

import logging
import sys

import boto3

from .adapters.aws import OpenSearchIndex, S3Store, SageMakerAsync, SqsQueue
from .jobs import DynamoJobStore
from .pipeline import Dispatcher, Indexer
from .ports import Metrics
from .runner import Runner
from .settings import Settings


def build(role: str, s: Settings) -> Runner:
    metrics = Metrics()
    store = S3Store()
    jobs = DynamoJobStore(boto3.resource("dynamodb").Table(s.jobs_table))
    if role == "dispatcher":
        handler = Dispatcher(s, store=store, jobs=jobs,
                             inference=SageMakerAsync(s.endpoint_name, allowed_bucket=s.images_bucket),
                             results=SqsQueue(s.results_queue_url), metrics=metrics).handle
        queue = SqsQueue(s.ingest_queue_url)
    elif role == "indexer":
        handler = Indexer(s, store=store, jobs=jobs, index=OpenSearchIndex(s.search_endpoint, s.region),
                          ingest=SqsQueue(s.ingest_queue_url), dlq=SqsQueue(s.ingest_dlq_url), metrics=metrics).handle
        queue = SqsQueue(s.results_queue_url)
    else:
        raise SystemExit("role must be dispatcher or indexer")
    return Runner(queue, handler, service=role, namespace=s.metrics_namespace, metrics=metrics,
                  batch=s.receive_batch)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    argv = sys.argv[1:] if argv is None else argv
    build(argv[0] if argv else "", Settings.from_env()).run_forever()


if __name__ == "__main__":
    main()
