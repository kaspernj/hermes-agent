import pytest

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from plugins.tensorbuzz_pr_webhook_registrar.tensorbuzz_pr_webhook_registrar.verification import VerificationError, verify_adapter_ingress


@pytest.mark.asyncio
async def test_signed_delivery_replay_and_one_agent_completion():
    secret = "local-test-secret"; admitted = []; completed = []
    route = {"secret": secret, "events": ["build_group.completed"], "prompt": "read only", "deliver": "log", "deliver_only": False}
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {"proof": route}, "host": "127.0.0.1", "port": 0}))
    async def handle(event): admitted.append(event.message_id); completed.append(event.message_id)
    adapter.handle_message = handle
    result = await verify_adapter_ingress(adapter, "proof", secret, {"event_type": "build_group.completed"}, "proof-id")
    assert result.ingress_accepted and result.replay_suppressed
    assert not result.delivery_verified
    assert result.agent_continuation_verified
    assert not result.completion_verified
    assert admitted == ["proof-id"] and completed == ["proof-id"]


@pytest.mark.asyncio
async def test_deliver_only_fails_workflow_owner_proof():
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {"proof": {"secret": "s", "prompt": "x", "deliver": "telegram", "deliver_only": True}}, "host": "127.0.0.1", "port": 0}))
    with pytest.raises(VerificationError):
        await verify_adapter_ingress(adapter, "proof", "s", {}, "id")


def test_public_proof_keeps_ingress_delivery_admission_completion_separate(tmp_path):
    from plugins.tensorbuzz_pr_webhook_registrar.tensorbuzz_pr_webhook_registrar.verification import PublicIngressVerifier

    verifier = PublicIngressVerifier("https://host", tmp_path, timeout_seconds=0,
                                     delivery_evidence=lambda *_: False)
    verifier._post = lambda *_: (202, {"status": "accepted"})
    # Replay is not proved and no durable admission/completion evidence exists.
    spec = type("Spec", (), {"repo": "a/b", "pr": 1, "head": "a"*40,
                              "build_group_id": "g", "ci_route_name": "route"})()
    result = verifier(spec, {}, "s")
    assert result["ingress_accepted"] is True
    assert result["delivery_verified"] is False
    assert result["agent_continuation_verified"] is False
    assert result["completion_verified"] is False
