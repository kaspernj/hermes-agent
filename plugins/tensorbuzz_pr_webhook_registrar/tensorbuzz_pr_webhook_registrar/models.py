from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID

UNMONITORED = "UNMONITORED — registration incomplete"
REVIEW_EVENTS = (
    "github.code_review_comment",
    "github.code_review_submitted",
    "github.code_review_thread_resolved",
)
_REPO = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?$"
)
_HEAD = re.compile(r"^[0-9a-fA-F]{40}$")
_DELIVERY = re.compile(r"^telegram:(-?[0-9]+):([0-9]+)$")


class ValidationError(ValueError):
    pass


def _uuid(value: str, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError(f"{label} must be a UUID") from exc


@dataclass(frozen=True)
class RegistrationSpec:
    repo: str
    tensorbuzz_project_id: str
    pr: int
    head: str
    deliver: str
    continuation_prompt_file: Path
    build_group_id: str | None = None
    workflow_owner: bool = True
    deliver_only: bool = False
    review_events: tuple[str, ...] = REVIEW_EVENTS

    @property
    def safe_repo_slug(self) -> str:
        return self.repo.lower().replace("/", "-").replace(".", "-")

    @property
    def ci_route_name(self) -> str:
        return f"tb-{self.safe_repo_slug}-pr-{self.pr}-ci-{self.head[:12]}"

    @property
    def review_route_name(self) -> str:
        return f"tb-{self.safe_repo_slug}-pr-{self.pr}-review"

    @property
    def delivery_parts(self) -> tuple[str, str]:
        match = _DELIVERY.fullmatch(self.deliver)
        if not match:
            raise ValidationError("delivery must be exactly telegram:<numeric-chat-id>:<numeric-topic-id>")
        return match.group(1), match.group(2)

    def validate(self, *, require_generation: bool) -> "RegistrationSpec":
        if not _REPO.fullmatch(self.repo):
            raise ValidationError("repository must be owner/name")
        if not isinstance(self.pr, int) or isinstance(self.pr, bool) or self.pr <= 0:
            raise ValidationError("PR number must be positive")
        if not _HEAD.fullmatch(self.head):
            raise ValidationError("head must be exactly 40 hexadecimal characters")
        project_id = _uuid(self.tensorbuzz_project_id, "TensorBuzz project ID")
        build_id = self.build_group_id
        if require_generation:
            if not build_id:
                raise ValidationError("build-group ID is required")
            build_id = _uuid(build_id, "build-group ID")
        elif build_id:
            build_id = _uuid(build_id, "build-group ID")
        chat, topic = self.delivery_parts
        if int(chat) == 0 or int(topic) <= 0:
            raise ValidationError("Telegram chat ID must be non-zero and topic ID must be positive")
        if self.workflow_owner and self.deliver_only:
            raise ValidationError("workflow-owner routes cannot be deliver-only")
        path = Path(self.continuation_prompt_file)
        if self.workflow_owner and (not path.is_file() or not path.read_text(encoding="utf-8").strip()):
            raise ValidationError("workflow-owner routes require a non-empty continuation prompt file")
        return replace(self, tensorbuzz_project_id=project_id, build_group_id=build_id,
                       head=self.head.lower(), continuation_prompt_file=path)

    def load_prompt(self) -> str:
        value = self.continuation_prompt_file.read_text(encoding="utf-8").strip()
        if not value:
            raise ValidationError("continuation prompt file is empty")
        return value

    def new_receipt(self, command: str) -> "Receipt":
        return Receipt(command=command, status="validated", repository=self.repo, pr=self.pr,
                       head=self.head, build_group_id=self.build_group_id, destination=self.deliver,
                       ci_route_name=self.ci_route_name, review_route_name=self.review_route_name)


@dataclass
class Receipt:
    schema_version: int = 1
    command: str = ""
    status: str = ""
    repository: str = ""
    pr: int = 0
    head: str = ""
    build_group_id: str | None = None
    destination: str = ""
    ci_route_name: str = ""
    review_route_name: str = ""
    ci_provider_subscription_id: str | None = None
    review_provider_subscription_id: str | None = None
    provider_readback: bool = False
    local_readback: bool = False
    ingress_accepted: bool = False
    delivery_verified: bool = False
    replay_suppressed: bool = False
    agent_continuation_verified: bool = False
    completion_verified: bool = False
    cleanup_state: str | None = None
    pending_provider_subscription_ids: list[str] = field(default_factory=list)
    retired_provider_subscription_ids: list[str] = field(default_factory=list)
    pending_route_names: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    _FORBIDDEN: ClassVar[tuple[str, ...]] = ("secret", "token", "prompt", "route_store")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in data:
            if any(term in key.lower() for term in self._FORBIDDEN):
                raise AssertionError(f"receipt field is not secret-free: {key}")
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Receipt":
        allowed = {name for name in cls.__dataclass_fields__ if not name.startswith("_")}
        return cls(**{key: val for key, val in value.items() if key in allowed})
