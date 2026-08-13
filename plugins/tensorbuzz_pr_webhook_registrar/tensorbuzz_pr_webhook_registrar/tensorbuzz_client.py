from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .models import REVIEW_EVENTS

PROVIDER_CONTRACT_TASK = "868fc9e5-0999-40cf-bdca-9d29981a62e1"


class ProviderContractError(RuntimeError):
    pass


def _unqualified_contract() -> ProviderContractError:
    return ProviderContractError(
        "TensorBuzz scoped create/readback/delete contract is not qualified; "
        f"provider-contract task {PROVIDER_CONTRACT_TASK}"
    )


class ProviderAdapter(Protocol):
    """Semantic boundary to be implemented from authoritative provider evidence."""

    def create_callback(self, scope: dict[str, Any]) -> str: ...
    def read_callback(self, provider_id: str) -> dict[str, Any]: ...
    def delete_callback(self, provider_id: str) -> None: ...


class HostHTTPTransport:
    """Fail-closed host adapter until the provider contract is qualified.

    The credential path is retained only for future adapter compatibility. Its
    contents are not read while the provider contract is unqualified, and no
    URL, request body, response envelope, or provider mutation is attempted.
    """

    def __init__(self, credential_file: Path, *, timeout: float = 15.0):
        self.credential_file = Path(credential_file)
        self.timeout = timeout

    def request(self, method: str, path: str, json_body: dict | None = None) -> dict:
        """Compatibility guard: even direct callers cannot send guessed HTTP."""
        raise _unqualified_contract()

    def create_callback(self, scope: dict[str, Any]) -> str:
        raise _unqualified_contract()

    def read_callback(self, provider_id: str) -> dict[str, Any]:
        raise _unqualified_contract()

    def delete_callback(self, provider_id: str) -> None:
        raise _unqualified_contract()


@dataclass(frozen=True)
class CreatedSubscription:
    id: str
    expected: dict[str, Any]


class TensorBuzzClient:
    """Provider-neutral lifecycle logic over an injected qualified adapter."""

    def __init__(self, transport: ProviderAdapter):
        self.transport = transport

    def _create(self, expected: dict[str, Any]) -> CreatedSubscription:
        ident = self.transport.create_callback(expected)
        if not isinstance(ident, str) or not ident:
            raise ProviderContractError(
                f"callback creation returned no opaque ID; provider-contract task {PROVIDER_CONTRACT_TASK}"
            )
        return CreatedSubscription(ident, expected)

    def create_ci(self, project_id: str, pr: int, build_group_id: str,
                  callback_url: str, secret_reference: str) -> CreatedSubscription:
        return self._create({"project_id": project_id, "pr": pr,
                             "build_group_id": build_group_id,
                             "events": ["build_group.completed"], "one_time": True,
                             "callback_url": callback_url, "callback_secret": secret_reference})

    def create_review(self, project_id: str, pr: int, callback_url: str,
                      secret_reference: str) -> CreatedSubscription:
        return self._create({"project_id": project_id, "pr": pr,
                             "events": list(REVIEW_EVENTS), "one_time": False,
                             "callback_url": callback_url, "callback_secret": secret_reference})

    def read(self, provider_id: str) -> dict[str, Any]:
        return self.transport.read_callback(provider_id)

    def verify(self, provider_id: str, expected: dict[str, Any]) -> bool:
        actual = self.read(provider_id)
        required = {"id", "active", "project_id", "pr", "events", "one_time"}
        if expected.get("build_group_id") is not None:
            required.add("build_group_id")
        if not required.issubset(actual):
            missing = ", ".join(sorted(required - set(actual)))
            raise ProviderContractError(
                f"scoped callback readback omits required fields ({missing}); "
                f"provider-contract task {PROVIDER_CONTRACT_TASK}"
            )
        if actual["id"] != provider_id or actual["active"] is not True:
            return False
        for key in ("project_id", "pr", "build_group_id", "one_time"):
            if key in expected and actual.get(key) != expected[key]:
                return False
        return set(actual.get("events", [])) == set(expected["events"])

    def delete(self, provider_id: str) -> None:
        self.transport.delete_callback(provider_id)
        try:
            actual = self.read(provider_id)
        except KeyError:
            return
        if actual.get("active", True):
            raise RuntimeError(f"provider cleanup readback failed for opaque ID {provider_id}")

    def compensate(self, provider_ids: list[str]) -> None:
        failures = []
        for provider_id in reversed(provider_ids):
            try:
                self.delete(provider_id)
            except ProviderContractError:
                raise
            except Exception as exc:
                failures.append(f"{provider_id}: {type(exc).__name__}")
        if failures:
            raise RuntimeError("provider compensation incomplete for opaque IDs: " + ", ".join(failures))
