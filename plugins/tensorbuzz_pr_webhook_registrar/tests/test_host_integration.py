import json
from types import SimpleNamespace

import pytest

import plugins.tensorbuzz_pr_webhook_registrar as plugin
from gateway.config import Platform, PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from plugins.tensorbuzz_pr_webhook_registrar.tensorbuzz_pr_webhook_registrar.hooks import PROOF_EXPECTED
from plugins.tensorbuzz_pr_webhook_registrar.tensorbuzz_pr_webhook_registrar.route_store import RouteStore, build_routes
from plugins.tensorbuzz_pr_webhook_registrar.tensorbuzz_pr_webhook_registrar.verification import VerificationError, verify_adapter_ingress
from test_cli_validation import valid_spec


class Context:
    def __init__(self): self.cli = []; self.hooks = {}
    def register_cli_command(self, **kwargs): self.cli.append(kwargs)
    def register_hook(self, name, callback): self.hooks[name] = callback


@pytest.mark.asyncio
async def test_real_import_registration_ingress_lease_and_completion(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx = Context(); plugin.register(ctx)
    assert ctx.cli[0]["name"] == "tensorbuzz-pr-webhooks"
    assert {"pre_gateway_dispatch", "post_llm_call", "on_session_end"} <= set(ctx.hooks)

    spec = valid_spec(tmp_path).validate(require_generation=True)
    routes = build_routes(spec, "proof", "ci", "review", "test-secret", "read-only proof")
    RouteStore(tmp_path / "webhook_subscriptions.json").install("proof", routes)
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0, "secret": "global"}))
    adapter._reload_dynamic_routes()
    captured = []
    async def capture(event): captured.append(event)
    adapter.handle_message = capture
    payload = {"event_type": "build_group.completed", "repository": spec.repo,
               "pullRequestNumber": spec.pr, "head": spec.head,
               "buildGroupId": spec.build_group_id, "proof": True}
    ingress = await verify_adapter_ingress(adapter, spec.ci_route_name, "test-secret", payload, "same-id")
    assert ingress.ingress_accepted and ingress.replay_suppressed
    assert len(captured) == 1

    event = captured[0]
    assert ctx.hooks["pre_gateway_dispatch"](event=event)["action"] == "allow"
    duplicate = SimpleNamespace(source=event.source, raw_message=event.raw_message)
    assert ctx.hooks["pre_gateway_dispatch"](event=duplicate)["action"] == "skip"

    ctx.hooks["post_llm_call"](session_id="session-1", platform="webhook",
                               assistant_response=PROOF_EXPECTED)
    class DB:
        def get_session(self, _): return {"chat_id": event.source.chat_id}
        def close(self): pass
    monkeypatch.setattr("hermes_state.SessionDB", DB)
    ctx.hooks["on_session_end"](session_id="session-1", completed=True,
                                interrupted=False, platform="webhook")
    marker = json.loads((tmp_path / "tensorbuzz_pr_webhook_registrar" /
                         "completion_markers" / "same-id.json").read_text())
    assert marker["admission_verified"] is True
    assert marker["proof_outcome_verified"] is True
    assert marker["completed"] is True
    lease = json.loads((tmp_path / "tensorbuzz_pr_webhook_registrar" /
                        "continuation_leases.json").read_text())
    assert lease[spec.ci_route_name]["state"] == "handoff_ready"


@pytest.mark.parametrize("response", [
    "\nTENSORBUZZ_WEBHOOK_PROOF_COMPLETE\n",
    "TENSORBUZZ_WEBHOOK_PROOF_COMPLETE ",
])
def test_registered_proof_hook_requires_byte_exact_marker(tmp_path, monkeypatch, response):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx = Context(); plugin.register(ctx)
    ctx.hooks["post_llm_call"](session_id="session-1", platform="webhook",
                               assistant_response=response)
    marker = json.loads((tmp_path / "tensorbuzz_pr_webhook_registrar" /
                         "proof_outcomes" / "session-1.json").read_text())
    assert marker["proof_outcome_verified"] is False


def test_registered_proof_hook_accepts_only_exact_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx = Context(); plugin.register(ctx)
    ctx.hooks["post_llm_call"](session_id="session-1", platform="webhook",
                               assistant_response=PROOF_EXPECTED)
    marker = json.loads((tmp_path / "tensorbuzz_pr_webhook_registrar" /
                         "proof_outcomes" / "session-1.json").read_text())
    assert marker["proof_outcome_verified"] is True


@pytest.mark.asyncio
async def test_deliver_only_workflow_owner_still_fails():
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {"proof": {
        "secret": "s", "prompt": "x", "deliver": "telegram", "deliver_only": True}},
        "host": "127.0.0.1", "port": 0}))
    with pytest.raises(VerificationError):
        await verify_adapter_ingress(adapter, "proof", "s", {}, "id")
