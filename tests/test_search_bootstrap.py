import pytest

from imaging.documents import INDEX_NAME
from search_bootstrap.handler import apply, on_event


class Client:
    def __init__(self, index_status=200):
        self.calls, self.index_status = [], index_status

    def request(self, method, path, body=None, params=""):
        self.calls.append((method, path, body))
        if path == f"/{INDEX_NAME}":
            return self.index_status, ({"error": {"type": "resource_already_exists_exception"}}
                                       if self.index_status == 400 else {})
        return 200, {}


def test_creates_index_and_least_privilege_roles():
    c = Client()
    done = apply(c, "arn:aws:iam::111122223333:role/w", "arn:aws:iam::111122223333:role/r")
    assert done == ["index:created", "role:imaging_writer", "role:imaging_reader"]
    roles = {p: b for m, p, b in c.calls if "/roles/" in p}
    writer = roles["/_plugins/_security/api/roles/imaging_writer"]["index_permissions"][0]
    assert writer["index_patterns"] == [INDEX_NAME] and "indices:data/write/index" in writer["allowed_actions"]
    assert all(r["cluster_permissions"] == [] for r in roles.values())
    maps = {p: b for m, p, b in c.calls if "/rolesmapping/" in p}
    assert maps["/_plugins/_security/api/rolesmapping/imaging_reader"]["backend_roles"] == ["arn:aws:iam::111122223333:role/r"]


def test_idempotent_when_index_exists():
    assert apply(Client(400), "w", "r")[0] == "index:exists"


def test_failure_raises():
    with pytest.raises(RuntimeError):
        apply(Client(500), "w", "r")


def test_delete_is_noop():
    assert on_event({"RequestType": "Delete", "PhysicalResourceId": "x"})["PhysicalResourceId"] == "x"
