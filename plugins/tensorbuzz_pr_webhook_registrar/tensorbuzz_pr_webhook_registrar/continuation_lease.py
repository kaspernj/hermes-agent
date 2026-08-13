from __future__ import annotations

import json
import fcntl
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class LeaseResult:
    admitted: bool
    state: str
    reason: str


class LeaseStore:
    """Durable generation-scoped writer fence; stale leases need recovery."""

    def __init__(self, path: Path, *, ttl_seconds: int = 3600,
                 clock: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.ttl_seconds = ttl_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(self.lock_path, 0o600); fcntl.flock(fd, fcntl.LOCK_EX); yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)

    def _load_unlocked(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}

    def _save_unlocked(self, value: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".leases.", suffix=".tmp")
        temp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, sort_keys=True)
                handle.flush(); os.fsync(handle.fileno())
            os.chmod(temp, 0o600); os.replace(temp, self.path); os.chmod(self.path, 0o600)
        finally:
            temp.unlink(missing_ok=True)

    def _stale(self, lease: dict) -> bool:
        admitted = datetime.fromisoformat(lease["admitted_at"])
        return (self.clock() - admitted).total_seconds() > self.ttl_seconds

    def acquire(self, route: str, generation: str, delivery_id: str) -> LeaseResult:
        with self._locked():
            data = self._load_unlocked(); current = data.get(route)
            if current and current.get("state") == "active" and current.get("generation") == generation:
                if self._stale(current):
                    current["state"] = "stale"; self._save_unlocked(data)
                    return LeaseResult(False, "stale", "active lease expired; explicit recovery required")
                return LeaseResult(False, "active", "generation already has an active continuation writer")
            data[route] = {"state": "active", "generation": generation, "delivery_id": delivery_id,
                           "admitted_at": self.clock().isoformat(), "completed_at": None}
            self._save_unlocked(data)
            return LeaseResult(True, "active", "continuation admitted")

    def release(self, route: str, generation: str, delivery_id: str, *, completed: bool) -> None:
        with self._locked():
            data = self._load_unlocked(); current = data.get(route)
            if not current or current.get("generation") != generation or current.get("delivery_id") != delivery_id:
                raise RuntimeError("lease handoff identity does not match active owner")
            current["state"] = "handoff_ready" if completed else "released"
            current["completed_at"] = self.clock().isoformat()
            self._save_unlocked(data)

    def recover_stale(self, route: str) -> None:
        with self._locked():
            data = self._load_unlocked(); current = data.get(route)
            if not current or current.get("state") != "stale":
                raise RuntimeError("only a visibly stale lease can be recovered")
            current["state"] = "recovered"; current["completed_at"] = self.clock().isoformat(); self._save_unlocked(data)

    def status(self, route: str) -> dict:
        with self._locked():
            data = self._load_unlocked(); current = data.get(route)
            if current and current.get("state") == "active" and self._stale(current):
                current["state"] = "stale"; self._save_unlocked(data)
            return dict(current or {"state": "none"})

    def remove(self, route: str) -> None:
        with self._locked():
            data = self._load_unlocked(); data.pop(route, None); self._save_unlocked(data)
