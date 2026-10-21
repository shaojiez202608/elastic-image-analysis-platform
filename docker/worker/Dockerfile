# Worker image (dispatcher and indexer roles), built for linux/arm64 in CodeBuild - never locally.
# Base: the Docker Official Image for Python, pulled from its Amazon ECR Public mirror (no Docker Hub
# rate limits in CodeBuild). The build applies Debian security updates before installing packages.
FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY docker/worker/requirements.txt /app/requirements.txt
# Hash-pinned dependencies exported from uv.lock (uv export --only-group runtime).
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.txt
COPY src/imaging /app/imaging
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app
USER 10001
# ECS uses the task-definition health check; this one documents the same probe for other runtimes.
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-c", "import imaging.worker_main"]
ENTRYPOINT ["python", "-m", "imaging.worker_main"]
