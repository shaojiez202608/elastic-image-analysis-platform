"""Remove this project's objects from the shared CDK bootstrap asset bucket.

Only objects identified by asset hash are touched:
- every `*.assets.json` in the given cloud-assembly directories (default: cdk.out and its
  sub-directories) contributes the stack templates and this project's own Lambda code
  (display names `ApiFn/Code`, `SearchBootstrapFn/Code`);
- `--hash-list` files add object keys computed by re-synthesizing earlier commits.
CDK's generic provider packages (custom-resource handlers such as `Custom::*`, `LogRetention*`,
`AWS679f53fac002430cb0da5b7982bd2287`) are shared with other projects and never deleted, and the
CDKToolkit stack is not modified. Objects that cannot be identified are not deleted.

`--content-scan` adds a second, content-based identification for objects left by earlier
deployments that were synthesized from uncommitted trees (their hashes cannot be reproduced): a
template counts as ours only if it carries BOTH this project's Project tag value and one of the
stack-name prefixes; a zip counts only if it contains this project's Lambda package
(`search_bootstrap/handler.py`). Provider packages never match either rule.

Dry run by default; `--delete` also requires CONFIRM_ASSETS=yes. Deletes every version of a key
(the bucket is versioned). Prints keys and counts only, never the account ID.

    uv run python scripts/cleanup_bootstrap_assets.py [--assembly cdk.out ...] [--hash-list f.json] [--delete]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import boto3

SHARED_MARKERS = ("Custom::", "LogRetention", "AWS679f53fac002430cb0da5b7982bd2287", "BucketNotificationsHandler")
OWN_CODE = ("ApiFn/Code", "SearchBootstrapFn/Code")


def own_keys_from_assembly(root: Path, prefix: str) -> set[str]:
    keys = set()
    for f in root.rglob("*.assets.json"):
        if not f.name.startswith(prefix + "-"):
            continue
        for _h, asset in json.loads(f.read_text()).get("files", {}).items():
            name = asset.get("displayName", "")
            if any(m in name for m in SHARED_MARKERS):
                continue
            if name.endswith(" Template") or name in OWN_CODE:
                for dest in asset["destinations"].values():
                    keys.add(dest["objectKey"])
    return keys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assembly", action="append", default=None, help="cloud assembly dir(s), default cdk.out")
    ap.add_argument("--hash-list", action="append", default=[], help="JSON list of object keys from older synths")
    ap.add_argument("--config", default="config/sandbox.json", help="profile whose 'project' is the stack prefix")
    ap.add_argument("--prefix", default=None, help="overrides the profile's project prefix")
    ap.add_argument("--extra-prefix", action="append", default=[], help="older stack prefixes to include")
    ap.add_argument("--qualifier", default="hnb659fds")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--content-scan", action="store_true", help="also identify objects by content (see above)")
    ap.add_argument("--tag-value", default="elastic-imaging")
    args = ap.parse_args()

    if not args.prefix:
        cfg = Path(__file__).resolve().parents[1] / args.config
        cfg = cfg if cfg.exists() else cfg.with_name(cfg.stem + ".example.json")
        args.prefix = json.loads(cfg.read_text())["project"]
    keys: set[str] = set()
    for d in args.assembly or ["cdk.out"]:
        for prefix in [args.prefix, *args.extra_prefix]:
            keys |= own_keys_from_assembly(Path(d), prefix)
    for f in args.hash_list:
        keys |= set(json.loads(Path(f).read_text()))

    account = boto3.client("sts").get_caller_identity()["Account"]
    bucket = f"cdk-{args.qualifier}-assets-{account}-{args.region}"
    s3 = boto3.client("s3", region_name=args.region)
    by_key: dict[str, list[dict]] = {}
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
        for v in page.get("Versions", []) + page.get("DeleteMarkers", []):
            by_key.setdefault(v["Key"], []).append(v)
    present = [(k, by_key[k]) for k in sorted(keys) if k in by_key]
    if args.content_scan:
        prefixes = [args.prefix, *args.extra_prefix]
        for key, versions in sorted(by_key.items()):
            if key in keys or not versions or max(v.get("Size", 0) for v in versions) > 2_000_000:
                continue
            try:
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            except s3.exceptions.NoSuchKey:  # latest version is a delete marker
                continue
            ours = False
            if key.endswith(".json"):
                text = body.decode("utf-8", errors="replace")
                ours = f'"{args.tag_value}"' in text and any(f'"{p}-' in text for p in prefixes)
            elif key.endswith(".zip"):
                try:
                    ours = "search_bootstrap/handler.py" in zipfile.ZipFile(io.BytesIO(body)).namelist()
                except zipfile.BadZipFile:
                    ours = False
            if ours:
                present.append((key, versions))
                print(f"  content-identified: {key}")
    print(f"identified {len(keys)} candidate keys; {len(present)} present in the bootstrap bucket")
    for key, versions in present:
        print(f"  {key} ({len(versions)} version(s))")
    if not args.delete:
        print("dry run; re-run with --delete and CONFIRM_ASSETS=yes to remove them")
        return 0
    if os.environ.get("CONFIRM_ASSETS") != "yes":
        print("refusing: set CONFIRM_ASSETS=yes to delete", file=sys.stderr)
        return 2
    failed = 0
    for key, versions in present:
        for v in versions:
            try:
                s3.delete_object(Bucket=bucket, Key=key, VersionId=v["VersionId"])
            except Exception as exc:  # noqa: BLE001 - warn and continue with the rest
                failed += 1
                print(f"  WARNING could not delete {key}@{v['VersionId']}: {type(exc).__name__}", file=sys.stderr)
    print(f"deleted {sum(len(v) for _, v in present) - failed} object version(s), {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
