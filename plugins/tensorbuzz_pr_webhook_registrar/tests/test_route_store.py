import json
import os
import pytest

from plugins.tensorbuzz_pr_webhook_registrar.tensorbuzz_pr_webhook_registrar.route_store import RouteCollision, RouteStore, build_routes
from test_cli_validation import valid_spec


def test_collision_preserves_unrelated_route(tmp_path):
    path = tmp_path / "webhook_subscriptions.json"
    path.write_text(json.dumps({"taken": {"description": "other"}}))
    store = RouteStore(path)
    with pytest.raises(RouteCollision):
        store.install("attempt", {"taken": {"description": "ours"}})
    assert json.loads(path.read_text())["taken"]["description"] == "other"


def test_atomic_install_has_exact_agent_capable_metadata(tmp_path):
    spec = valid_spec(tmp_path).validate(require_generation=True)
    routes = build_routes(spec, "attempt-1", "ci-id", "review-id", "callback-secret", "safe prompt")
    store = RouteStore(tmp_path / "webhook_subscriptions.json")
    store.install("attempt-1", routes)
    persisted = store.readback(routes)
    ci = persisted[spec.ci_route_name]
    review = persisted[spec.review_route_name]
    assert ci["deliver_only"] is False
    assert ci["events"] == ["build_group.completed"]
    assert ci["registrar"]["head"] == spec.head
    assert ci["registrar"]["build_group_id"] == spec.build_group_id
    assert ci["registrar"]["provider_subscription_id"] == "ci-id"
    assert ci["prompt"] == "safe prompt"
    assert review["events"] == list(spec.review_events)
    assert "build_group_id" not in review["registrar"]
    assert oct(os.stat(store.path).st_mode & 0o777) == "0o600"


def test_compensation_removes_only_attempt_owned_routes(tmp_path):
    store = RouteStore(tmp_path / "webhook_subscriptions.json")
    store.install("a", {"ours": {"secret": "x", "registrar": {"attempt_id": "a"}}})
    store.install("b", {"theirs": {"secret": "x", "registrar": {"attempt_id": "b"}}})
    store.remove_owned("a", ["ours", "theirs"])
    assert set(store.load()) == {"theirs"}
