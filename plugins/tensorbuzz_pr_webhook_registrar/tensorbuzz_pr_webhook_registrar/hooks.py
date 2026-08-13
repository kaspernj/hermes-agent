from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

from .continuation_lease import LeaseStore
from .route_store import RouteStore

PROOF_EXPECTED = "TENSORBUZZ_WEBHOOK_PROOF_COMPLETE"


def _state_dir() -> Path:
    return get_hermes_home() / "tensorbuzz_pr_webhook_registrar"


def _nested(payload: dict, *paths: str) -> Any:
    for path in paths:
        value: Any = payload
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                value = None
                break
            value = value[part]
        if value is not None:
            return value
    return None


def _matches_scope(metadata: dict, payload: dict) -> bool:
    repo = _nested(payload, "repository", "repo", "repository.full_name", "project.repository")
    pr = _nested(payload, "pullRequestNumber", "pull_request_number", "pr", "pull_request.number")
    if str(repo) != str(metadata.get("repo")) or str(pr) != str(metadata.get("pr")):
        return False
    if "build_group_id" in metadata:
        head = _nested(payload, "head", "headSha", "head_sha", "pull_request.head.sha")
        group = _nested(payload, "buildGroupId", "build_group_id", "build_group.id")
        return str(head).lower() == str(metadata.get("head", "")).lower() and str(group) == str(metadata["build_group_id"])
    return True


def pre_gateway_dispatch(event=None, **_kwargs):
    source = getattr(event, "source", None)
    if str(getattr(getattr(source, "platform", None), "value", getattr(source, "platform", ""))) != "webhook":
        return None
    chat_id = str(getattr(source, "chat_id", ""))
    if not chat_id.startswith("webhook:"):
        return None
    try:
        _, route_name, delivery_id = chat_id.split(":", 2)
        route = RouteStore(get_hermes_home() / "webhook_subscriptions.json").load().get(route_name)
        metadata = (route or {}).get("registrar")
        if not metadata:
            return None
        payload = getattr(event, "raw_message", None)
        if not isinstance(payload, dict) or not _matches_scope(metadata, payload):
            return {"action": "skip", "reason": "TensorBuzz callback scope does not match registered generation"}
        generation = str(metadata.get("generation", metadata.get("head", "persistent-review")))
        result = LeaseStore(_state_dir() / "continuation_leases.json").acquire(route_name, generation, delivery_id)
        if not result.admitted:
            return {"action": "skip", "reason": f"TensorBuzz continuation lease is {result.state}"}
        _save_evidence("admission_markers", delivery_id, {
            "schema_version": 1, "route": route_name, "delivery_id": delivery_id,
            "generation": generation, "admission_verified": True,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"action": "allow"}
    except (OSError, ValueError, json.JSONDecodeError):
        return {"action": "skip", "reason": "TensorBuzz registrar state unavailable"}


def _save_evidence(kind: str, identifier: str, marker: dict) -> None:
    directory = _state_dir() / kind
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{identifier}.json"
    fd, name = tempfile.mkstemp(dir=directory, prefix=".marker.", suffix=".tmp")
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(marker, handle, sort_keys=True); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temp, 0o600); os.replace(temp, target); os.chmod(target, 0o600)
    finally:
        temp.unlink(missing_ok=True)


def post_llm_call(session_id: str = "", assistant_response: str = "",
                  platform: str = "", **_kwargs) -> None:
    if platform != "webhook" or not session_id:
        return
    # Store only a boolean derived from exact equality; never persist output.
    _save_evidence("proof_outcomes", session_id, {
        "schema_version": 1, "session_id": session_id,
        "proof_outcome_verified": assistant_response == PROOF_EXPECTED,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    })


def on_session_end(session_id: str = "", completed: bool = False,
                   interrupted: bool = False, platform: str = "", **_kwargs) -> None:
    if platform != "webhook" or not session_id:
        return
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        try:
            session = db.get_session(session_id)
        finally:
            db.close()
        chat_id = str((session or {}).get("chat_id", ""))
        if not chat_id.startswith("webhook:"):
            return
        _, route, delivery_id = chat_id.split(":", 2)
        route_data = RouteStore(get_hermes_home() / "webhook_subscriptions.json").load().get(route, {})
        metadata = route_data.get("registrar", {})
        generation = str(metadata.get("generation", metadata.get("head", "persistent-review")))
        leases = LeaseStore(_state_dir() / "continuation_leases.json")
        leases.release(route, generation, delivery_id, completed=bool(completed and not interrupted))
        admission_path = _state_dir() / "admission_markers" / f"{delivery_id}.json"
        outcome_path = _state_dir() / "proof_outcomes" / f"{session_id}.json"
        admission = json.loads(admission_path.read_text(encoding="utf-8")) if admission_path.exists() else {}
        outcome = json.loads(outcome_path.read_text(encoding="utf-8")) if outcome_path.exists() else {}
        _save_evidence("completion_markers", delivery_id, {
            "schema_version": 1, "route": route, "delivery_id": delivery_id,
            "session_id": session_id, "session_key": str((session or {}).get("session_key", "")),
            "admission_verified": bool(admission.get("admission_verified")),
            "proof_outcome_verified": bool(outcome.get("proof_outcome_verified")),
            "completed": bool(completed and not interrupted),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        # Completion proof fails closed when the marker is absent. Hooks must
        # never expose route contents or break session finalization.
        return
