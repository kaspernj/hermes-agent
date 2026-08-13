"""Hermes plugin entry point for the host-owned TensorBuzz registrar."""

from .tensorbuzz_pr_webhook_registrar.cli import command, setup_cli
from .tensorbuzz_pr_webhook_registrar.hooks import on_session_end, post_llm_call, pre_gateway_dispatch


def register(ctx) -> None:
    ctx.register_cli_command(
        name="tensorbuzz-pr-webhooks",
        help="Manage exact-scope TensorBuzz PR webhook callbacks",
        setup_fn=setup_cli,
        handler_fn=command,
        description="Validate, register, inspect, rotate, and clean up host-owned PR callbacks.",
    )
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    ctx.register_hook("post_llm_call", post_llm_call)
    ctx.register_hook("on_session_end", on_session_end)
