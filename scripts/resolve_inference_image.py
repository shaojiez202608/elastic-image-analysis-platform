"""Resolve the prebuilt SageMaker AI PyTorch inference image (AWS Deep Learning Containers) for the profile.

The registry account per Region comes from the image-URI table that the SageMaker Python SDK ships
(pinned commit + SHA-256 below), so no registry account number is stored in this repository. With
--verify the tag is checked with ecr:DescribeImages (read-only).

    uv run python scripts/resolve_inference_image.py [--config config/sandbox.json] [--verify]
Prints the image URI on stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK_COMMIT = "61213e938b667a3c41167eb2b61844ec5356265d"  # pragma: allowlist secret (public git commit, 2026-10-21)
TABLE_URL = ("https://raw.githubusercontent.com/aws/sagemaker-python-sdk/"
             f"{SDK_COMMIT}/sagemaker-core/src/sagemaker/core/image_uri_config/pytorch.json")
TABLE_SHA256 = "2c07f8e5331b047b01592add2d00b30c2ca05b289801beedc049020c5797f337"  # pragma: allowlist secret (checksum of the pinned file)


def tag_for(version: str, py: str, accelerator: str, container: str | None) -> str:
    # DLC tag scheme for SageMaker images: <VERSION>-<cpu|gpu>-<PY>[-<OS>]-sagemaker
    parts = [version, accelerator, py] + ([container] if container else []) + ["sagemaker"]
    return "-".join(parts)


def resolve(table: dict, *, region: str, architecture: str, accelerator: str, version: str, py: str) -> str:
    scope = table["inference_graviton" if architecture == "arm64" else "inference"]
    v = scope["versions"].get(version)
    if v is None:
        raise SystemExit(f"framework_version {version} not in the SDK table for {architecture}")
    if py not in v["py_versions"]:
        raise SystemExit(f"py_version {py} not offered for {version}")
    registry = v["registries"].get(region)
    if registry is None:
        raise SystemExit(f"no {architecture} PyTorch inference image listed for {region}")
    container = (v.get("container_version") or {}).get(accelerator)
    if architecture == "x86" and accelerator == "gpu":
        container = container or None
    return f"{registry}.dkr.ecr.{region}.amazonaws.com/{v['repository']}:{tag_for(version, py, accelerator, container)}"


def fetch_table() -> dict:
    with urllib.request.urlopen(TABLE_URL, timeout=30) as r:  # noqa: S310 - https URL checked or fixed  # nosec B310
        raw = r.read(5_000_000)
    if hashlib.sha256(raw).hexdigest() != TABLE_SHA256:
        raise SystemExit("image table checksum mismatch: refusing to use it")
    return json.loads(raw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/sandbox.json")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    cfg_path = ROOT / a.config
    cfg = json.loads((cfg_path if cfg_path.exists() else cfg_path.with_name(cfg_path.stem + ".example.json")).read_text())
    inf = cfg["inference"]
    uri = resolve(fetch_table(), region=cfg["region"], architecture=inf["architecture"],
                  accelerator=inf["accelerator"], version=inf["framework_version"], py=inf["py_version"])
    if a.verify:
        import boto3

        reg, _, rest = uri.partition(".dkr.ecr.")
        repo, _, tag = rest.split("/", 1)[1].partition(":")
        boto3.client("ecr", region_name=cfg["region"]).describe_images(
            registryId=reg, repositoryName=repo, imageIds=[{"imageTag": tag}])
        print("verified: tag exists", file=sys.stderr)
    print(uri)
    return 0


if __name__ == "__main__":
    sys.exit(main())
