"""Delete the SageMaker AI lineage entities that the service created automatically for this project's endpoint.

Creating an endpoint makes SageMaker AI record ML lineage: a context `<ENDPOINT>-<N>-aws-endpoint`, an action
`<ENDPOINT>-<N>-1-aws-endpoint`, and artifacts for the model data and the container image, linked by
associations. They are tagged with the endpoint's tags, survive `cdk destroy`, and were found by the residual
check in the first sandbox run.

Only entities that can be attributed to this project are deleted: contexts/actions whose name starts with
`<PREFIX>-endpoint-`, and artifacts whose every association points to one of those actions (an artifact that is
also linked to anything else, e.g. a shared image used by another endpoint, is kept and reported).
Dry run by default; `--delete` also requires CONFIRM_LINEAGE=yes. Output redacts account IDs.

    uv run python scripts/cleanup_lineage.py [--config config/sandbox.json] [--delete]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
ACCT = re.compile(r"\b\d{12}\b")


def redact(v) -> str:
    return ACCT.sub("111122223333", str(v))


def pages(client, op, key, **kw):
    out = []
    for page in client.get_paginator(op).paginate(**kw):
        out.extend(page.get(key, []))
    return out


def plan(sm, prefix: str) -> dict:
    stem = f"{prefix}-endpoint-"
    contexts = [c["ContextArn"] for c in pages(sm, "list_contexts", "ContextSummaries")
                if c["ContextName"].startswith(stem)]
    actions = [a["ActionArn"] for a in pages(sm, "list_actions", "ActionSummaries") if a["ActionName"].startswith(stem)]
    ours = set(contexts) | set(actions)
    assoc, artifacts, kept = [], set(), set()
    for arn in actions:
        for a in pages(sm, "list_associations", "AssociationSummaries", DestinationArn=arn):
            assoc.append((a["SourceArn"], a["DestinationArn"]))
            if ":artifact/" in a["SourceArn"]:
                artifacts.add(a["SourceArn"])
        for a in pages(sm, "list_associations", "AssociationSummaries", SourceArn=arn):
            assoc.append((a["SourceArn"], a["DestinationArn"]))
    for art in sorted(artifacts):
        links = [(a["SourceArn"], a["DestinationArn"]) for a in
                 pages(sm, "list_associations", "AssociationSummaries", SourceArn=art)
                 + pages(sm, "list_associations", "AssociationSummaries", DestinationArn=art)]
        if any((s if d == art else d) not in ours for s, d in links):
            kept.add(art)
    return {"contexts": contexts, "actions": actions, "associations": sorted(set(assoc)),
            "artifacts": sorted(artifacts - kept), "kept": sorted(kept)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/sandbox.json")
    ap.add_argument("--delete", action="store_true")
    a = ap.parse_args()
    p = ROOT / a.config
    cfg = json.loads((p if p.exists() else p.with_name(p.stem + ".example.json")).read_text())
    sm = boto3.client("sagemaker", region_name=cfg["region"])
    pl = plan(sm, cfg["project"])
    for k in ("contexts", "actions", "artifacts", "kept"):
        print(f"  {k:<10} {len(pl[k])}: " + ", ".join(redact(x.rsplit('/', 1)[-1]) for x in pl[k]))
    if not a.delete:
        print("dry run; re-run with --delete and CONFIRM_LINEAGE=yes")
        return 0
    if os.environ.get("CONFIRM_LINEAGE") != "yes":
        print("refusing: set CONFIRM_LINEAGE=yes", file=sys.stderr)
        return 2
    failed = 0
    for src, dst in pl["associations"]:
        try:
            sm.delete_association(SourceArn=src, DestinationArn=dst)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFound":
                failed += 1
    for arn in pl["actions"]:
        try:
            sm.delete_action(ActionName=arn.rsplit("/", 1)[-1])
        except ClientError:
            failed += 1
    for arn in pl["contexts"]:
        try:
            sm.delete_context(ContextName=arn.rsplit("/", 1)[-1])
        except ClientError:
            failed += 1
    for arn in pl["artifacts"]:
        try:
            sm.delete_artifact(ArtifactArn=arn)
        except ClientError:
            failed += 1
    print(f"deleted lineage entities; {failed} failure(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
