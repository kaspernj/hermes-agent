from types import SimpleNamespace
from uuid import uuid4

from gateway.config import Platform

from plugins.tensorbuzz_pr_webhook_registrar.tensorbuzz_pr_webhook_registrar import hooks
from plugins.tensorbuzz_pr_webhook_registrar.tensorbuzz_pr_webhook_registrar.route_store import RouteStore, build_routes
from test_cli_validation import valid_spec


def _event(route, delivery, payload):
    source = SimpleNamespace(platform=Platform.WEBHOOK, chat_id=f"webhook:{route}:{delivery}")
    return SimpleNamespace(source=source, raw_message=payload)


def test_ingress_hook_rejects_stale_scope_before_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    spec = valid_spec(tmp_path).validate(require_generation=True)
    routes = build_routes(spec, "attempt", "ci", "review", "secret", "safe")
    RouteStore(tmp_path / "webhook_subscriptions.json").install("attempt", routes)
    stale = {"repository": spec.repo, "pullRequestNumber": spec.pr,
             "head": "b" * 40, "buildGroupId": spec.build_group_id}
    result = hooks.pre_gateway_dispatch(event=_event(spec.ci_route_name, "d1", stale))
    assert result["action"] == "skip"
    assert "scope" in result["reason"]


def test_ingress_hook_admits_one_writer_and_fences_second(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    spec = valid_spec(tmp_path).validate(require_generation=True)
    routes = build_routes(spec, "attempt", "ci", "review", "secret", "safe")
    RouteStore(tmp_path / "webhook_subscriptions.json").install("attempt", routes)
    payload = {"repository": spec.repo, "pullRequestNumber": spec.pr,
               "head": spec.head, "buildGroupId": spec.build_group_id}
    first = hooks.pre_gateway_dispatch(event=_event(spec.ci_route_name, "d1", payload))
    second = hooks.pre_gateway_dispatch(event=_event(spec.ci_route_name, "d2", payload))
    assert first["action"] == "allow"
    assert second["action"] == "skip"
    assert "active" in second["reason"]


def test_non_registrar_webhook_is_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert hooks.pre_gateway_dispatch(event=_event("ordinary", "d", {})) is None
