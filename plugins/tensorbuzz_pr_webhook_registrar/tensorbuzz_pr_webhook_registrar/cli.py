from __future__ import annotations

import argparse
import json
from pathlib import Path

from hermes_constants import get_hermes_home

from .lifecycle import Registrar
from .models import RegistrationSpec, UNMONITORED, ValidationError
from .route_store import RouteStore
from .tensorbuzz_client import HostHTTPTransport, TensorBuzzClient
from .verification import PublicIngressVerifier


def _add_scope(parser: argparse.ArgumentParser, *, generation: bool = True) -> None:
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tensorbuzz-project-id", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head", required=True)
    if generation:
        parser.add_argument("--build-group-id", required=True)
    else:
        parser.add_argument("--build-group-id")
    parser.add_argument("--deliver", required=True)
    parser.add_argument("--continuation-prompt-file", required=True, type=Path)
    policy = parser.add_mutually_exclusive_group()
    policy.add_argument("--workflow-owner", action="store_true", default=True)
    policy.add_argument("--deliver-only", action="store_true")


def setup_cli(parser: argparse.ArgumentParser) -> None:
    parser.prog = "hermes tensorbuzz-pr-webhooks"
    actions = parser.add_subparsers(dest="registrar_action", required=True)
    for name in ("validate", "register", "rotate", "cleanup", "status"):
        child = actions.add_parser(name)
        _add_scope(child, generation=name != "status")
        if name in {"register", "rotate"}:
            child.add_argument("--public-webhook-base-url", required=True)
        if name != "validate":
            child.add_argument("--credentials", required=True, type=Path,
                               help="Protected host JSON file; never a project path")


def _spec(args: argparse.Namespace) -> RegistrationSpec:
    return RegistrationSpec(
        repo=args.repo, tensorbuzz_project_id=args.tensorbuzz_project_id, pr=args.pr,
        head=args.head, build_group_id=getattr(args, "build_group_id", None),
        deliver=args.deliver, continuation_prompt_file=args.continuation_prompt_file,
        workflow_owner=not bool(args.deliver_only), deliver_only=bool(args.deliver_only),
    )


def command(args: argparse.Namespace) -> int:
    try:
        action = args.registrar_action
        spec = _spec(args).validate(require_generation=action != "status")
        if action == "validate":
            print(spec.new_receipt("validate").to_json())
            return 0
        home = get_hermes_home()
        client = TensorBuzzClient(HostHTTPTransport(args.credentials))
        state_dir = home / "tensorbuzz_pr_webhook_registrar"
        base_url = getattr(args, "public_webhook_base_url", "")
        verifier = PublicIngressVerifier(base_url, state_dir / "completion_markers") if base_url else None
        registrar = Registrar(client, RouteStore(home / "webhook_subscriptions.json"),
                              state_dir, verifier=verifier or (lambda *_a, **_k: {}))
        if action == "register":
            receipt = registrar.register(spec, args.public_webhook_base_url)
        elif action == "rotate":
            receipt = registrar.rotate(spec, args.public_webhook_base_url)
        elif action == "cleanup":
            receipt = registrar.cleanup(spec)
        else:
            receipt = registrar.status(spec)
        print(receipt.to_json())
        return 0 if receipt.status not in {UNMONITORED, "cleanup_pending"} else 2
    except (ValidationError, PermissionError, ValueError) as exc:
        print(json.dumps({"command": getattr(args, "registrar_action", "unknown"),
                          "status": UNMONITORED, "errors": [str(exc)]}, ensure_ascii=False))
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(prog="hermes-tensorbuzz-pr-webhooks")
    setup_cli(parser)
    return command(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
