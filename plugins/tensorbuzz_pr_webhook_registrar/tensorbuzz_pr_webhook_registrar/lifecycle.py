from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .continuation_lease import LeaseStore
from .hooks import PROOF_EXPECTED
from .models import Receipt, RegistrationSpec, UNMONITORED, ValidationError
from .route_store import RouteCollision, RouteStore, build_routes
from .tensorbuzz_client import ProviderContractError, TensorBuzzClient

PROOF_PROMPT = (
    "Verification only. Read this callback, do not use tools or modify state, "
    f"and complete with exactly: {PROOF_EXPECTED}"
)


class ReceiptStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def path_for(self, spec: RegistrationSpec) -> Path:
        return self.directory / f"{spec.safe_repo_slug}-pr-{spec.pr}.json"

    def save(self, spec: RegistrationSpec, receipt: Receipt) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.path_for(spec)
        fd, name = tempfile.mkstemp(dir=self.directory, prefix=".receipt.", suffix=".tmp")
        temp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(receipt.to_json()); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temp, 0o600); os.replace(temp, target); os.chmod(target, 0o600)
        finally:
            temp.unlink(missing_ok=True)

    def save_attempt(self, spec: RegistrationSpec, attempt_id: str, receipt: Receipt) -> None:
        attempts = self.directory / "attempts"
        attempts.mkdir(parents=True, exist_ok=True)
        target = attempts / f"{spec.safe_repo_slug}-pr-{spec.pr}-{attempt_id}.json"
        fd, name = tempfile.mkstemp(dir=attempts, prefix=".attempt.", suffix=".tmp")
        temp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(receipt.to_json()); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temp, 0o600); os.replace(temp, target); os.chmod(target, 0o600)
        finally:
            temp.unlink(missing_ok=True)

    def load(self, spec: RegistrationSpec) -> Receipt | None:
        path = self.path_for(spec)
        return Receipt.from_dict(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else None

    def load_matching_attempt(self, spec: RegistrationSpec) -> Receipt | None:
        attempts = self.directory / "attempts"
        if not attempts.exists():
            return None
        for path in sorted(attempts.glob(f"{spec.safe_repo_slug}-pr-{spec.pr}-*.json"), reverse=True):
            receipt = Receipt.from_dict(json.loads(path.read_text(encoding="utf-8")))
            if Registrar._receipt_matches_spec(receipt, spec, require_build_group=True):
                return receipt
        return None

    def remove_matching_attempts(self, spec: RegistrationSpec) -> None:
        attempts = self.directory / "attempts"
        if not attempts.exists():
            return
        for path in attempts.glob(f"{spec.safe_repo_slug}-pr-{spec.pr}-*.json"):
            receipt = Receipt.from_dict(json.loads(path.read_text(encoding="utf-8")))
            if Registrar._receipt_matches_spec(receipt, spec, require_build_group=True):
                path.unlink(missing_ok=True)

    def remove(self, spec: RegistrationSpec) -> None:
        self.path_for(spec).unlink(missing_ok=True)


class Registrar:
    def __init__(self, client: TensorBuzzClient, route_store: RouteStore, state_dir: Path,
                 *, verifier: Callable[..., Any]):
        self.client = client
        self.route_store = route_store
        self.receipts = ReceiptStore(Path(state_dir) / "receipts")
        self.leases = LeaseStore(Path(state_dir) / "continuation_leases.json")
        self.verifier = verifier

    def preflight(self, spec: RegistrationSpec, *, allow_existing_review: bool = False) -> None:
        current = self.route_store.load()
        for name in (spec.ci_route_name, spec.review_route_name):
            route = current.get(name)
            if not route:
                continue
            metadata = route.get("registrar", {})
            if name == spec.review_route_name:
                extra = route.get("deliver_extra", {})
                expected_chat, expected_topic = spec.delivery_parts
                if (str(extra.get("chat_id")) != expected_chat or
                        str(extra.get("message_thread_id")) != expected_topic):
                    raise ValidationError("historical review route destination does not match exact requested destination")
                if (allow_existing_review and metadata.get("repo") == spec.repo
                        and metadata.get("pr") == spec.pr):
                    continue
            raise RouteCollision(f"route '{name}' already exists")

    @staticmethod
    def _proof_dict(value: Any) -> dict[str, bool]:
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        return dict(value or {})

    @staticmethod
    def _error_evidence(exc: Exception) -> str:
        # ProviderContractError messages contain only missing field names and
        # the provider-contract task ID by construction. Other exception
        # messages may contain transport details, so retain only their class.
        return str(exc) if isinstance(exc, ProviderContractError) else type(exc).__name__

    @staticmethod
    def _receipt_matches_spec(receipt: Receipt, spec: RegistrationSpec,
                              *, require_build_group: bool) -> bool:
        if (receipt.repository != spec.repo or receipt.pr != spec.pr
                or receipt.head.lower() != spec.head.lower()):
            return False
        if require_build_group:
            return receipt.build_group_id == spec.build_group_id
        return spec.build_group_id is None or receipt.build_group_id == spec.build_group_id

    @staticmethod
    def _generation_mismatch_receipt(spec: RegistrationSpec, command: str) -> Receipt:
        receipt = spec.new_receipt(command)
        receipt.status = UNMONITORED
        receipt.cleanup_state = "not_started"
        receipt.errors = ["generation identity does not match the persisted receipt"]
        return receipt

    def register(self, spec: RegistrationSpec, public_base_url: str) -> Receipt:
        spec = spec.validate(require_generation=True)
        receipt = spec.new_receipt("register")
        attempt_id = str(uuid4()); created: list[str] = []; installed: list[str] = []
        try:
            self.preflight(spec)
            callback_secret = secrets.token_urlsafe(32)
            ci = self.client.create_ci(spec.tensorbuzz_project_id, spec.pr, spec.build_group_id or "",
                                       f"{public_base_url.rstrip('/')}/webhooks/{spec.ci_route_name}", callback_secret)
            created.append(ci.id)
            review = self.client.create_review(spec.tensorbuzz_project_id, spec.pr,
                                                f"{public_base_url.rstrip('/')}/webhooks/{spec.review_route_name}", callback_secret)
            created.append(review.id)
            if not self.client.verify(ci.id, ci.expected) or not self.client.verify(review.id, review.expected):
                raise RuntimeError("provider scoped readback did not match")
            receipt.ci_provider_subscription_id = ci.id
            receipt.review_provider_subscription_id = review.id
            receipt.provider_readback = True
            routes = build_routes(spec, attempt_id, ci.id, review.id, callback_secret, PROOF_PROMPT)
            self.route_store.install(attempt_id, routes); installed = list(routes)
            self.route_store.readback(routes); receipt.local_readback = True
            proof = self._proof_dict(self.verifier(spec, routes, callback_secret))
            receipt.ingress_accepted = bool(proof.get("ingress_accepted"))
            receipt.delivery_verified = bool(proof.get("delivery_verified"))
            receipt.replay_suppressed = bool(proof.get("replay_suppressed"))
            receipt.agent_continuation_verified = bool(proof.get("agent_continuation_verified"))
            receipt.completion_verified = bool(proof.get("completion_verified"))
            if not all((receipt.ingress_accepted, receipt.delivery_verified, receipt.replay_suppressed,
                        receipt.agent_continuation_verified, receipt.completion_verified)):
                raise RuntimeError("workflow-owner proof gates did not all pass")
            self.route_store.replace_prompt_owned(attempt_id, installed, spec.load_prompt())
            receipt.status = "agent_continuation_verified"
            self.receipts.save(spec, receipt)
            return receipt
        except Exception as exc:
            cleanup_errors = []
            if installed:
                try: self.route_store.remove_owned(attempt_id, installed)
                except Exception as cleanup_exc: cleanup_errors.append(type(cleanup_exc).__name__)
            if created:
                result = self.client.compensate(created)
                receipt.retired_provider_subscription_ids = result.deleted_ids
                receipt.pending_provider_subscription_ids = result.remaining_ids
                if result.remaining_ids:
                    cleanup_errors.append("ProviderCompensationIncomplete")
            receipt.status = UNMONITORED
            receipt.cleanup_state = "cleanup_pending" if cleanup_errors else "compensated"
            receipt.errors = [self._error_evidence(exc)] + cleanup_errors
            self.receipts.save(spec, receipt)
            return receipt

    def rotate(self, spec: RegistrationSpec, public_base_url: str) -> Receipt:
        spec = spec.validate(require_generation=True)
        old = self.receipts.load(spec)
        if old is None or not old.review_provider_subscription_id:
            return self.register(spec, public_base_url)
        receipt = spec.new_receipt("rotate"); attempt_id = str(uuid4())
        created: list[str] = []; installed: list[str] = []
        committed = False
        try:
            self.preflight(spec, allow_existing_review=True)
            callback_secret = secrets.token_urlsafe(32)
            ci = self.client.create_ci(spec.tensorbuzz_project_id, spec.pr, spec.build_group_id or "",
                                       f"{public_base_url.rstrip('/')}/webhooks/{spec.ci_route_name}", callback_secret)
            created.append(ci.id)
            if not self.client.verify(ci.id, ci.expected):
                raise RuntimeError("new CI generation provider readback failed")
            # Reuse the already-proven persistent review callback. The new CI
            # generation is independently installed and proved before old CI
            # state is touched.
            routes = build_routes(spec, attempt_id, ci.id, old.review_provider_subscription_id,
                                  callback_secret, PROOF_PROMPT)
            ci_routes = {spec.ci_route_name: routes[spec.ci_route_name]}
            self.route_store.install(attempt_id, ci_routes); installed = list(ci_routes)
            self.route_store.readback(ci_routes)
            proof = self._proof_dict(self.verifier(spec, ci_routes, callback_secret))
            if not all(bool(proof.get(key)) for key in (
                "ingress_accepted", "delivery_verified", "replay_suppressed",
                "agent_continuation_verified", "completion_verified",
            )):
                raise RuntimeError("new CI generation proof failed")
            self.route_store.replace_prompt_owned(attempt_id, installed, spec.load_prompt())
            receipt.status = "agent_continuation_verified"
            receipt.ci_provider_subscription_id = ci.id
            receipt.review_provider_subscription_id = old.review_provider_subscription_id
            receipt.provider_readback = receipt.local_readback = True
            receipt.ingress_accepted = True
            receipt.delivery_verified = receipt.replay_suppressed = True
            receipt.agent_continuation_verified = receipt.completion_verified = True
            # Commit point: the proved new generation is the durable last-good
            # identity before any old resource is retired.
            self.receipts.save(spec, receipt)
            committed = True
        except Exception as exc:
            if committed:  # retirement is intentionally outside this block
                raise
            cleanup_errors = []
            if installed:
                try: self.route_store.remove_owned(attempt_id, installed)
                except Exception as cleanup_exc: cleanup_errors.append(type(cleanup_exc).__name__)
            if created:
                result = self.client.compensate(created)
                receipt.retired_provider_subscription_ids = result.deleted_ids
                receipt.pending_provider_subscription_ids = result.remaining_ids
                if result.remaining_ids:
                    cleanup_errors.append("ProviderCompensationIncomplete")
            receipt.status = UNMONITORED
            receipt.cleanup_state = "cleanup_pending" if cleanup_errors else "compensated"
            receipt.errors = [self._error_evidence(exc)] + cleanup_errors
            self.receipts.save_attempt(spec, attempt_id, receipt); return receipt

        # Retirement phase. A partial failure is cleanup-pending evidence; it
        # must never compensate the already-committed new generation.
        receipt.pending_provider_subscription_ids = (
            [old.ci_provider_subscription_id] if old.ci_provider_subscription_id else []
        )
        receipt.pending_route_names = [old.ci_route_name]
        try:
            if old.ci_provider_subscription_id:
                self.client.delete(old.ci_provider_subscription_id)
                receipt.pending_provider_subscription_ids = []
                receipt.retired_provider_subscription_ids = [old.ci_provider_subscription_id]
            old_routes = self.route_store.load()
            old_ci = old_routes.get(old.ci_route_name)
            if old_ci:
                old_attempt = old_ci.get("registrar", {}).get("attempt_id", "")
                self.route_store.remove_owned(old_attempt, [old.ci_route_name])
            receipt.pending_route_names = []
            receipt.cleanup_state = "verified_absent"
            self.receipts.save(spec, receipt)
            return receipt
        except Exception as exc:
            receipt.status = "cleanup_pending"
            receipt.cleanup_state = "cleanup_pending"
            receipt.errors = [self._error_evidence(exc)]
            try:
                self.receipts.save(spec, receipt)
            finally:
                self.receipts.save_attempt(spec, attempt_id, receipt)
            return receipt

    def cleanup(self, spec: RegistrationSpec) -> Receipt:
        spec = spec.validate(require_generation=True)
        primary = self.receipts.load(spec)
        from_attempt = False
        if primary and self._receipt_matches_spec(primary, spec, require_build_group=True):
            receipt = primary
        else:
            receipt = self.receipts.load_matching_attempt(spec)
            from_attempt = receipt is not None
        if receipt is None:
            return self._generation_mismatch_receipt(spec, "cleanup")
        receipt.command = "cleanup"
        ids = [provider_id for provider_id in dict.fromkeys(
            [value for value in (receipt.ci_provider_subscription_id,
                                 receipt.review_provider_subscription_id) if value]
            + receipt.pending_provider_subscription_ids
        ) if provider_id not in receipt.retired_provider_subscription_ids]
        try:
            # Provider IDs first, then exact locally-owned routes, each with readback.
            compensation = self.client.compensate(ids)
            if compensation.remaining_ids:
                receipt.pending_provider_subscription_ids = compensation.remaining_ids
                receipt.retired_provider_subscription_ids = list(dict.fromkeys(
                    receipt.retired_provider_subscription_ids + compensation.deleted_ids
                ))
                if compensation.provider_contract_unqualified:
                    raise ProviderContractError(
                        "TensorBuzz scoped delete contract is not qualified; "
                        "provider-contract task 868fc9e5-0999-40cf-bdca-9d29981a62e1"
                    )
                raise RuntimeError("provider compensation incomplete")
            current = self.route_store.load()
            route_names = list(dict.fromkeys(
                receipt.pending_route_names if from_attempt else (
                    [receipt.ci_route_name, receipt.review_route_name]
                    + receipt.pending_route_names
                )
            ))
            for name in route_names:
                route = current.get(name)
                if route:
                    owner = route.get("registrar", {}).get("attempt_id", "")
                    self.route_store.remove_owned(owner, [name])
                self.leases.remove(name)
            receipt.status = "cleaned"; receipt.cleanup_state = "verified_absent"
            if from_attempt:
                self.receipts.remove_matching_attempts(spec)
            else:
                self.receipts.remove(spec)
            return receipt
        except Exception as exc:
            receipt.status = UNMONITORED if isinstance(exc, ProviderContractError) else "cleanup_pending"
            receipt.cleanup_state = "cleanup_pending"
            receipt.errors = [self._error_evidence(exc)]
            if from_attempt:
                self.receipts.save_attempt(spec, str(uuid4()), receipt)
            else:
                self.receipts.save(spec, receipt)
            return receipt

    def status(self, spec: RegistrationSpec) -> Receipt:
        spec = spec.validate(require_generation=False)
        receipt = self.receipts.load(spec)
        if receipt is None or not self._receipt_matches_spec(
                receipt, spec, require_build_group=spec.build_group_id is not None):
            return self._generation_mismatch_receipt(spec, "status")
        receipt.command = "status"
        routes = self.route_store.load()
        receipt.local_readback = all(name in routes for name in (receipt.ci_route_name, receipt.review_route_name))
        provider_ok = []
        checks = (
            (receipt.ci_provider_subscription_id,
             {"build_group_id": receipt.build_group_id, "events": ["build_group.completed"],
              "one_time": True, "project_id": spec.tensorbuzz_project_id, "pr": spec.pr}),
            (receipt.review_provider_subscription_id,
             {"project_id": spec.tensorbuzz_project_id, "pr": spec.pr,
              "events": list(spec.review_events), "one_time": False}),
        )
        for ident, expected in checks:
            if ident:
                try: provider_ok.append(self.client.verify(ident, expected))
                except Exception: provider_ok.append(False)
        receipt.provider_readback = bool(provider_ok) and all(provider_ok)
        lease = self.leases.status(receipt.ci_route_name)
        if lease["state"] == "active": receipt.status = "continuation_active"
        elif lease["state"] == "handoff_ready": receipt.status = "handoff_ready"
        elif lease["state"] == "stale": receipt.status = "stale_generation"
        elif receipt.agent_continuation_verified and receipt.provider_readback and receipt.local_readback:
            receipt.status = "agent_continuation_verified"
        elif receipt.delivery_verified: receipt.status = "delivery_verified"
        elif receipt.provider_readback and receipt.local_readback: receipt.status = "registered_unverified"
        else: receipt.status = UNMONITORED
        return receipt
