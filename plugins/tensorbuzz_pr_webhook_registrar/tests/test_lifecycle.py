import json
from uuid import uuid4

import pytest

from plugins.tensorbuzz_pr_webhook_registrar.tensorbuzz_pr_webhook_registrar.lifecycle import Registrar
from plugins.tensorbuzz_pr_webhook_registrar.tensorbuzz_pr_webhook_registrar.models import UNMONITORED
from plugins.tensorbuzz_pr_webhook_registrar.tensorbuzz_pr_webhook_registrar.route_store import RouteStore
from test_cli_validation import valid_spec
from test_tensorbuzz_client import Transport
from plugins.tensorbuzz_pr_webhook_registrar.tensorbuzz_pr_webhook_registrar.tensorbuzz_client import HostHTTPTransport, TensorBuzzClient


def test_registration_failure_compensates_and_is_unmonitored(tmp_path):
    spec = valid_spec(tmp_path).validate(require_generation=True)
    transport = Transport(); store = RouteStore(tmp_path / "routes.json")
    registrar = Registrar(TensorBuzzClient(transport), store, tmp_path / "state", verifier=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("proof failed")))
    receipt = registrar.register(spec, "https://host")
    assert receipt.status == UNMONITORED
    assert store.load() == {} and transport.objects == {}


def test_rotate_proves_new_before_removing_old_and_preserves_review(tmp_path):
    first = valid_spec(tmp_path).validate(require_generation=True)
    transport = Transport(); store = RouteStore(tmp_path / "routes.json")
    registrar = Registrar(TensorBuzzClient(transport), store, tmp_path / "state", verifier=lambda *a, **k: {"ingress_accepted": True, "delivery_verified": True, "replay_suppressed": True, "agent_continuation_verified": True, "completion_verified": True})
    old = registrar.register(first, "https://host")
    second = valid_spec(tmp_path, head="b"*40, build_group_id=str(uuid4())).validate(require_generation=True)
    new = registrar.rotate(second, "https://host")
    assert new.status == "agent_continuation_verified"
    assert first.ci_route_name not in store.load()
    assert second.ci_route_name in store.load() and second.review_route_name in store.load()
    assert len([n for n in store.load() if n == second.review_route_name]) == 1


def test_cleanup_provider_first_and_keeps_evidence_on_failure(tmp_path):
    spec = valid_spec(tmp_path).validate(require_generation=True)
    transport = Transport(); store = RouteStore(tmp_path / "routes.json")
    registrar = Registrar(TensorBuzzClient(transport), store, tmp_path / "state", verifier=lambda *a, **k: {"ingress_accepted": True, "delivery_verified": True, "replay_suppressed": True, "agent_continuation_verified": True, "completion_verified": True})
    registrar.register(spec, "https://host")
    cleaned = registrar.cleanup(spec)
    assert cleaned.status == "cleaned" and store.load() == {} and transport.objects == {}


def test_status_rejects_historical_destination_reuse(tmp_path):
    spec = valid_spec(tmp_path).validate(require_generation=True)
    store = RouteStore(tmp_path / "routes.json")
    store.install("old", {spec.review_route_name: {"secret": "s", "deliver": "telegram", "deliver_extra": {"chat_id": "-999", "message_thread_id": "1"}, "registrar": {"attempt_id": "old", "repo": spec.repo, "pr": spec.pr}}})
    registrar = Registrar(TensorBuzzClient(Transport()), store, tmp_path / "state", verifier=lambda *a, **k: {})
    with pytest.raises(Exception, match="destination"):
        registrar.preflight(spec)


def test_failed_rotation_preserves_last_good_receipt_and_cleanup_ids(tmp_path):
    first = valid_spec(tmp_path).validate(require_generation=True)
    transport = Transport(); store = RouteStore(tmp_path / "routes.json")
    good = {"ingress_accepted": True, "delivery_verified": True,
            "replay_suppressed": True, "agent_continuation_verified": True,
            "completion_verified": True}
    registrar = Registrar(TensorBuzzClient(transport), store, tmp_path / "state",
                          verifier=lambda *_a, **_k: good)
    old = registrar.register(first, "https://host")
    old_ids = (old.ci_provider_subscription_id, old.review_provider_subscription_id)
    registrar.verifier = lambda *_a, **_k: {**good, "completion_verified": False}
    second = valid_spec(tmp_path, head="b"*40,
                        build_group_id=str(uuid4())).validate(require_generation=True)
    failed = registrar.rotate(second, "https://host")
    assert failed.status == UNMONITORED
    preserved = registrar.receipts.load(first)
    assert preserved is not None
    assert (preserved.ci_provider_subscription_id,
            preserved.review_provider_subscription_id) == old_ids
    cleaned = registrar.cleanup(first)
    assert cleaned.status == "cleaned"
    assert all(ident not in transport.objects for ident in old_ids)


def test_operator_registration_is_unmonitored_without_qualified_contract(tmp_path):
    credentials = tmp_path / "credentials.json"
    credentials.write_text("{}", encoding="utf-8")
    credentials.chmod(0o600)
    spec = valid_spec(tmp_path).validate(require_generation=True)
    registrar = Registrar(TensorBuzzClient(HostHTTPTransport(credentials)),
                          RouteStore(tmp_path / "routes.json"), tmp_path / "state",
                          verifier=lambda *_a, **_k: pytest.fail("proof must not run"))
    receipt = registrar.register(spec, "https://host")
    assert receipt.status == UNMONITORED
    assert "868fc9e5-0999-40cf-bdca-9d29981a62e1" in receipt.errors[0]


def test_rotation_retirement_failure_keeps_committed_new_generation(tmp_path, monkeypatch):
    first = valid_spec(tmp_path).validate(require_generation=True)
    transport = Transport(); store = RouteStore(tmp_path / "routes.json")
    proof = {"ingress_accepted": True, "delivery_verified": True,
             "replay_suppressed": True, "agent_continuation_verified": True,
             "completion_verified": True}
    registrar = Registrar(TensorBuzzClient(transport), store, tmp_path / "state",
                          verifier=lambda *_a, **_k: proof)
    old = registrar.register(first, "https://host")
    second = valid_spec(tmp_path, head="c"*40,
                        build_group_id=str(uuid4())).validate(require_generation=True)
    original_remove = store.remove_owned
    def fail_old_retirement(attempt_id, names):
        if names == [old.ci_route_name]:
            raise OSError("retirement write failed")
        return original_remove(attempt_id, names)
    monkeypatch.setattr(store, "remove_owned", fail_old_retirement)

    rotated = registrar.rotate(second, "https://host")

    assert rotated.status == "cleanup_pending"
    committed = registrar.receipts.load(second)
    assert committed is not None
    assert committed.ci_provider_subscription_id == rotated.ci_provider_subscription_id
    assert committed.ci_provider_subscription_id in transport.objects
    assert committed.review_provider_subscription_id == old.review_provider_subscription_id
    assert second.ci_route_name in store.load()
    assert old.ci_route_name in store.load()
    assert old.ci_provider_subscription_id not in transport.objects
    attempts = list((tmp_path / "state" / "receipts" / "attempts").glob("*.json"))
    evidence = json.loads(attempts[-1].read_text(encoding="utf-8"))
    assert evidence["cleanup_state"] == "cleanup_pending"
    assert old.ci_provider_subscription_id in evidence["retired_provider_subscription_ids"]
    assert old.ci_provider_subscription_id not in evidence["pending_provider_subscription_ids"]
    assert old.ci_route_name in evidence["pending_route_names"]
    monkeypatch.setattr(store, "remove_owned", original_remove)
    cleaned = registrar.cleanup(second)
    assert cleaned.status == "cleaned"
    assert store.load() == {}
