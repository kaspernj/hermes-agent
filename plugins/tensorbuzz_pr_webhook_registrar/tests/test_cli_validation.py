from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from plugins.tensorbuzz_pr_webhook_registrar.tensorbuzz_pr_webhook_registrar.models import (
    RegistrationSpec,
    ValidationError,
)


def valid_spec(tmp_path: Path, **overrides) -> RegistrationSpec:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Continue CI ownership for this exact PR generation.")
    values: dict[str, Any] = dict(
        repo="acme/widget", tensorbuzz_project_id=str(uuid4()), pr=7,
        head="a" * 40, build_group_id=str(uuid4()),
        deliver="telegram:-100123:42", continuation_prompt_file=prompt,
        workflow_owner=True,
    )
    values.update(overrides)
    return RegistrationSpec(**values)


@pytest.mark.parametrize("field,value", [
    ("repo", "bad"), ("repo", "a/b/c"), ("pr", 0), ("head", "abc"),
    ("repo", "a/-bad"), ("repo", "a/bad."),
    ("head", "g" * 40), ("tensorbuzz_project_id", "nope"),
    ("build_group_id", "nope"), ("deliver", "slack:1:2"),
    ("deliver", "telegram:chat:topic"),
    ("deliver", "telegram:0:1"), ("deliver", "telegram:-100:0"),
])
def test_rejects_invalid_explicit_scope(tmp_path, field, value):
    with pytest.raises(ValidationError):
        valid_spec(tmp_path, **{field: value}).validate(require_generation=True)


def test_workflow_owner_requires_agent_route_and_prompt(tmp_path):
    with pytest.raises(ValidationError):
        valid_spec(tmp_path, deliver_only=True).validate(require_generation=True)
    missing = tmp_path / "missing.md"
    with pytest.raises(ValidationError):
        valid_spec(tmp_path, continuation_prompt_file=missing).validate(require_generation=True)


def test_valid_scope_loads_prompt_without_receipt_leak(tmp_path):
    spec = valid_spec(tmp_path, head="A" * 40)
    validated = spec.validate(require_generation=True)
    assert validated.head == "a" * 40
    receipt = validated.new_receipt("validate")
    serialized = receipt.to_dict()
    assert "prompt" not in str(serialized).lower()
    assert "secret" not in str(serialized).lower()


def test_plugin_uses_generic_cli_and_hook_registration():
    import plugins.tensorbuzz_pr_webhook_registrar as plugin

    calls = {"cli": [], "hooks": []}
    class Context:
        def register_cli_command(self, **kwargs): calls["cli"].append(kwargs)
        def register_hook(self, name, callback): calls["hooks"].append((name, callback))
    plugin.register(Context())
    assert calls["cli"][0]["name"] == "tensorbuzz-pr-webhooks"
    assert {name for name, _ in calls["hooks"]} == {
        "pre_gateway_dispatch", "post_llm_call", "on_session_end",
    }
