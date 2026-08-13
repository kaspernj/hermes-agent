from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import RegistrationSpec


class RouteCollision(RuntimeError):
    pass


class RouteStore:
    """Locked, atomic access to Hermes' authoritative dynamic route file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load_unlocked(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("webhook route store must contain an object")
        return data

    def load(self) -> dict[str, dict]:
        with self._locked():
            return self._load_unlocked()

    def _save_unlocked(self, routes: dict[str, dict]) -> None:
        prior = self.path.stat() if self.path.exists() else None
        fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(routes, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, 0o600)
            if prior is not None:
                try:
                    os.chown(temp, prior.st_uid, prior.st_gid)
                except PermissionError:
                    pass
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temp.unlink(missing_ok=True)

    def check_collisions(self, attempt_id: str, routes: dict[str, dict]) -> None:
        current = self.load()
        for name in routes:
            existing = current.get(name)
            owner = (existing or {}).get("registrar", {}).get("attempt_id")
            if existing is not None and owner != attempt_id:
                raise RouteCollision(f"route '{name}' is owned by another registration")

    def install(self, attempt_id: str, routes: dict[str, dict]) -> None:
        with self._locked():
            current = self._load_unlocked()
            for name in routes:
                existing = current.get(name)
                owner = (existing or {}).get("registrar", {}).get("attempt_id")
                if existing is not None and owner != attempt_id:
                    raise RouteCollision(f"route '{name}' is owned by another registration")
            current.update(routes)
            self._save_unlocked(current)

    def readback(self, expected: dict[str, dict]) -> dict[str, dict]:
        current = self.load()
        for name, route in expected.items():
            if current.get(name) != route:
                raise RuntimeError(f"authoritative local readback failed for route '{name}'")
        return {name: current[name] for name in expected}

    def remove_owned(self, attempt_id: str, names: list[str]) -> list[str]:
        removed: list[str] = []
        with self._locked():
            current = self._load_unlocked()
            for name in names:
                route = current.get(name)
                if route and route.get("registrar", {}).get("attempt_id") == attempt_id:
                    del current[name]
                    removed.append(name)
            if removed:
                self._save_unlocked(current)
        remaining = self.load()
        if any(name in remaining for name in removed):
            raise RuntimeError("local cleanup readback failed")
        return removed

    def replace_prompt_owned(self, attempt_id: str, names: list[str], prompt: str) -> None:
        with self._locked():
            current = self._load_unlocked()
            for name in names:
                route = current.get(name)
                if not route or route.get("registrar", {}).get("attempt_id") != attempt_id:
                    raise RouteCollision(f"cannot activate unowned route '{name}'")
                route["prompt"] = prompt
                route["registrar"]["activation"] = "production"
            self._save_unlocked(current)


def _delivery(spec: RegistrationSpec) -> dict[str, str]:
    chat, topic = spec.delivery_parts
    return {"chat_id": chat, "message_thread_id": topic}


def build_routes(spec: RegistrationSpec, attempt_id: str, ci_provider_id: str,
                 review_provider_id: str, callback_secret: str, prompt: str) -> dict[str, dict]:
    common = {"secret": callback_secret, "prompt": prompt, "deliver": "telegram",
              "deliver_extra": _delivery(spec), "deliver_only": bool(spec.deliver_only)}
    base_meta = {"schema_version": 1, "attempt_id": attempt_id, "repo": spec.repo,
                 "tensorbuzz_project_id": spec.tensorbuzz_project_id, "pr": spec.pr,
                 "workflow_owner": spec.workflow_owner, "activation": "proof"}
    ci_meta = {**base_meta, "head": spec.head, "build_group_id": spec.build_group_id,
               "provider_subscription_id": ci_provider_id, "generation": spec.head}
    review_meta = {**base_meta, "provider_subscription_id": review_provider_id,
                   "generation": "persistent-review"}
    return {
        spec.ci_route_name: {**common, "description": f"TensorBuzz exact CI generation for {spec.repo} PR #{spec.pr}",
                             "events": ["build_group.completed"], "registrar": ci_meta},
        spec.review_route_name: {**common, "description": f"TensorBuzz reviews for {spec.repo} PR #{spec.pr}",
                                 "events": list(spec.review_events), "registrar": review_meta},
    }
