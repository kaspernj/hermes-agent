from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


class VerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerificationResult:
    ingress_accepted: bool
    delivery_verified: bool
    replay_suppressed: bool
    agent_continuation_verified: bool
    completion_verified: bool

    def to_dict(self) -> dict[str, bool]:
        return {"ingress_accepted": self.ingress_accepted,
                "delivery_verified": self.delivery_verified,
                "replay_suppressed": self.replay_suppressed,
                "agent_continuation_verified": self.agent_continuation_verified,
                "completion_verified": self.completion_verified}


def signed_v2_headers(secret: str, body: bytes, delivery_id: str,
                      *, timestamp: int | None = None) -> dict[str, str]:
    timestamp = int(time.time()) if timestamp is None else timestamp
    signed = str(timestamp).encode() + b"." + body
    signature = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-Webhook-Timestamp": str(timestamp),
            "X-Webhook-Signature-V2": signature, "X-Request-ID": delivery_id}


async def verify_adapter_ingress(adapter, route_name: str, secret: str, payload: dict,
                                 delivery_id: str) -> VerificationResult:
    route = adapter._routes.get(route_name)
    if not route:
        raise VerificationError("proof route is not visible to the authoritative adapter")
    if route.get("deliver_only"):
        raise VerificationError("deliver-only route cannot prove workflow-owner agent admission")
    body = json.dumps(payload, separators=(",", ":")).encode()
    event_type = str(payload.get("event_type", "build_group.completed"))
    headers = signed_v2_headers(secret, body, delivery_id)
    headers["X-GitHub-Event"] = event_type
    calls = 0
    admissions = 0
    original = adapter.handle_message

    async def observed(event):
        nonlocal calls, admissions
        calls += 1
        result = original(event)
        if asyncio.iscoroutine(result):
            await result
        admissions += 1

    adapter.handle_message = observed
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    try:
        async with TestClient(TestServer(app)) as client:
            first = await client.post(f"/webhooks/{route_name}", data=body, headers=headers)
            first_body = await first.json()
            replay = await client.post(f"/webhooks/{route_name}", data=body, headers=headers)
            replay_body = await replay.json()
            for _ in range(100):
                if admissions:
                    break
                await asyncio.sleep(0.01)
    finally:
        adapter.handle_message = original
    accepted = first.status == 202 and first_body.get("status") == "accepted"
    deduped = replay.status == 200 and replay_body.get("status") == "duplicate"
    if not accepted or not deduped or calls != 1 or admissions != 1:
        raise VerificationError("signed ingress/replay/admission proof did not satisfy exactly-once gates")
    # This local helper proves ingress and one adapter admission only. It has
    # no Telegram ACK or completed agent-session evidence.
    return VerificationResult(True, False, True, True, False)


def delivery_ledger_evidence(spec, routes: dict, marker: dict) -> bool:
    """Read Hermes' existing durable post-SendResult delivery evidence."""
    route = routes.get(spec.ci_route_name, {})
    chat, topic = spec.delivery_parts
    extra = route.get("deliver_extra", {})
    if (route.get("deliver") != "telegram" or str(extra.get("chat_id")) != chat
            or str(extra.get("message_thread_id")) != topic):
        return False
    session_key = marker.get("session_key")
    if not session_key:
        return False
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / "state.db"
    if not path.exists():
        return False
    try:
        with sqlite3.connect(path) as connection:
            row = connection.execute(
                "SELECT state FROM delivery_obligations "
                "WHERE session_key = ? AND platform = 'webhook' "
                "ORDER BY updated_at DESC LIMIT 1",
                (session_key,),
            ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0] == "delivered")


class PublicIngressVerifier:
    """Bounded proof against public ingress plus the gateway completion hook."""

    def __init__(self, public_base_url: str, marker_dir: Path, *, timeout_seconds: float = 30.0,
                 delivery_evidence=delivery_ledger_evidence):
        self.public_base_url = public_base_url.rstrip("/")
        self.marker_dir = Path(marker_dir)
        self.timeout_seconds = timeout_seconds
        self.delivery_evidence = delivery_evidence

    @staticmethod
    def _post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try: detail = json.loads(exc.read().decode("utf-8"))
            except Exception: detail = {}
            return exc.code, detail

    def __call__(self, spec, routes: dict, secret: str) -> dict[str, bool]:
        # Prove the exact CI generation on a synthetic delivery; this never
        # consumes TensorBuzz's real one-time callback.
        delivery_id = f"tb-proof-{int(time.time() * 1000)}"
        payload = {"event_type": "build_group.completed", "repository": spec.repo,
                   "pullRequestNumber": spec.pr, "head": spec.head,
                   "buildGroupId": spec.build_group_id, "proof": True}
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = signed_v2_headers(secret, body, delivery_id)
        headers["X-GitHub-Event"] = "build_group.completed"
        url = f"{self.public_base_url}/webhooks/{spec.ci_route_name}"
        first_status, first = self._post(url, body, headers)
        replay_status, replay = self._post(url, body, headers)
        ingress = first_status == 202 and first.get("status") == "accepted"
        replayed = replay_status == 200 and replay.get("status") == "duplicate"
        marker_path = self.marker_dir / f"{delivery_id}.json"
        deadline = time.monotonic() + self.timeout_seconds
        marker = {}
        while time.monotonic() < deadline:
            if marker_path.exists():
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                break
            time.sleep(0.1)
        admitted = bool(marker.get("admission_verified") and marker.get("delivery_id") == delivery_id
                        and marker.get("route") == spec.ci_route_name and marker.get("session_id"))
        completed = bool(admitted and marker.get("completed") and marker.get("proof_outcome_verified"))
        delivered = False
        # on_session_end precedes outbound delivery. Poll the existing ledger
        # until the same bounded deadline rather than racing its delivered ACK.
        while completed and time.monotonic() < deadline:
            if self.delivery_evidence(spec, routes, marker):
                delivered = True
                break
            time.sleep(0.1)
        marker_path.unlink(missing_ok=True)
        return {"ingress_accepted": ingress, "delivery_verified": delivered,
                "replay_suppressed": replayed,
                "agent_continuation_verified": admitted,
                "completion_verified": completed}
