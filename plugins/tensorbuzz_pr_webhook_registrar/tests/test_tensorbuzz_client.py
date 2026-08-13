from uuid import uuid4
from typing import Any

import pytest

from plugins.tensorbuzz_pr_webhook_registrar.tensorbuzz_pr_webhook_registrar.tensorbuzz_client import (
    HostHTTPTransport, ProviderContractError, TensorBuzzClient,
)


class Transport:
    def __init__(self):
        self.calls: list[tuple[str, Any]] = []
        self.objects: dict[str, dict[str, Any]] = {}

    def create_callback(self, scope: dict[str, Any]) -> str:
        ident = f"sub-{len(self.objects)+1}"
        self.calls.append(("create", scope))
        self.objects[ident] = {"id": ident, "active": True, **scope}
        return ident

    def read_callback(self, provider_id: str) -> dict[str, Any]:
        self.calls.append(("read", provider_id))
        return self.objects[provider_id]

    def delete_callback(self, provider_id: str) -> None:
        self.calls.append(("delete", provider_id))
        self.objects.pop(provider_id, None)


def test_exact_ci_and_review_contract_and_readback():
    t = Transport(); client = TensorBuzzClient(t)
    project, group = str(uuid4()), str(uuid4())
    ci = client.create_ci(project, 9, group, "https://host/ci", "secret-ref")
    review = client.create_review(project, 9, "https://host/review", "secret-ref")
    assert client.verify(ci.id, ci.expected)
    assert client.verify(review.id, review.expected)
    creates = [value for action, value in t.calls if action == "create"]
    ci_body, review_body = creates
    assert ci_body["events"] == ["build_group.completed"] and ci_body["one_time"] is True
    assert ci_body["build_group_id"] == group and ci_body["pr"] == 9
    assert review_body["one_time"] is False and "build_group_id" not in review_body
    assert set(review_body["events"]) == {"github.code_review_comment", "github.code_review_submitted", "github.code_review_thread_resolved"}


def test_missing_scoped_readback_fails_with_provider_task():
    class Bad(Transport):
        def read_callback(self, provider_id: str) -> dict[str, Any]:
            result = super().read_callback(provider_id)
            result.pop("pr", None)
            return result
    client = TensorBuzzClient(Bad())
    created = client.create_ci(str(uuid4()), 1, str(uuid4()), "https://x", "ref")
    with pytest.raises(ProviderContractError, match="868fc9e5-0999-40cf-bdca-9d29981a62e1"):
        client.verify(created.id, created.expected)


def test_compensation_deletes_only_created_ids_reverse_order():
    t = Transport(); client = TensorBuzzClient(t)
    p = str(uuid4()); g = str(uuid4())
    a = client.create_ci(p, 1, g, "https://x/a", "ref")
    b = client.create_review(p, 1, "https://x/b", "ref")
    client.compensate([a.id, b.id])
    deletes = [value for action, value in t.calls if action == "delete"]
    assert deletes == [b.id, a.id]


def test_unqualified_host_transport_cannot_mutate(tmp_path):
    credentials = tmp_path / "credentials.json"
    credentials.write_text('{"base_url":"https://provider.invalid","token":"not-used"}')
    credentials.chmod(0o600)
    transport = HostHTTPTransport(credentials)
    with pytest.raises(ProviderContractError, match="868fc9e5-0999-40cf-bdca-9d29981a62e1"):
        transport.request("POST", "/guessed", {"mutation": True})


def test_operator_client_fails_before_create_read_or_delete(tmp_path):
    credentials = tmp_path / "credentials.json"
    credentials.write_text("{}")
    credentials.chmod(0o600)
    client = TensorBuzzClient(HostHTTPTransport(credentials))
    with pytest.raises(ProviderContractError, match="868fc9e5-0999-40cf-bdca-9d29981a62e1"):
        client.create_ci(str(uuid4()), 1, str(uuid4()), "https://host/hook", "opaque")
    with pytest.raises(ProviderContractError, match="868fc9e5-0999-40cf-bdca-9d29981a62e1"):
        client.read("opaque-id")
    with pytest.raises(ProviderContractError, match="868fc9e5-0999-40cf-bdca-9d29981a62e1"):
        client.delete("opaque-id")
