"""Custom resource (CDK Provider framework onEvent handler) run as the OpenSearch FGAC master IAM role.

Creates the strict-mapping index and two least-privilege FGAC roles, mapped to IAM role ARNs as
backend roles (the documented way to map IAM principals with fine-grained access control):
  imaging_writer  -> indexer task role : index/get documents in the images index only (action group `index`)
  imaging_reader  -> query API role    : read/search the images index only
Idempotent: re-running on Update re-applies the same definitions. Delete does nothing (the domain is
deleted with its stack).
"""

from __future__ import annotations

import json
import os

from imaging.adapters.aws import OpenSearchIndex
from imaging.documents import INDEX_NAME, MAPPING

# "index" is the documented default action group for creating/updating documents (index*, update*, bulk*,
# mapping put) https://docs.opensearch.org/latest/security/access-control/default-action-groups/ . Single-document
# writes are executed as a one-item bulk request, whose top-level action indices:data/write/bulk is authorised as a
# cluster permission; without it every write was refused with security_exception in the sandbox run.
WRITER = {"cluster_permissions": ["indices:data/write/bulk"],
          "index_permissions": [{"index_patterns": [INDEX_NAME],
                                 "allowed_actions": ["index", "indices:data/read/get"]}]}
READER = {"cluster_permissions": [],
          "index_permissions": [{"index_patterns": [INDEX_NAME], "allowed_actions": ["read"]}]}


def apply(client: OpenSearchIndex, writer_arn: str, reader_arn: str) -> list[str]:
    done = []
    status, resp = client.request("PUT", f"/{INDEX_NAME}", MAPPING)
    if status == 400 and "resource_already_exists_exception" in json.dumps(resp):
        done.append("index:exists")
    elif status in (200, 201):
        done.append("index:created")
    else:
        raise RuntimeError(f"create index failed with {status}")
    for name, body, arn in (("imaging_writer", WRITER, writer_arn), ("imaging_reader", READER, reader_arn)):
        st, _ = client.request("PUT", f"/_plugins/_security/api/roles/{name}", body)
        if st not in (200, 201):
            raise RuntimeError(f"role {name} failed with {st}")
        st, _ = client.request("PUT", f"/_plugins/_security/api/rolesmapping/{name}",
                               {"backend_roles": [arn], "hosts": [], "users": []})
        if st not in (200, 201):
            raise RuntimeError(f"role mapping {name} failed with {st}")
        done.append(f"role:{name}")
    return done


def on_event(event, context=None, client: OpenSearchIndex | None = None):
    props = event.get("ResourceProperties", {})
    if event.get("RequestType") == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "search-bootstrap")}
    client = client or OpenSearchIndex(props["Endpoint"], os.environ["AWS_REGION"])
    done = apply(client, props["WriterRoleArn"], props["ReaderRoleArn"])
    print(json.dumps({"event": "search_bootstrap", "done": done}))
    return {"PhysicalResourceId": "search-bootstrap", "Data": {"Index": INDEX_NAME}}
