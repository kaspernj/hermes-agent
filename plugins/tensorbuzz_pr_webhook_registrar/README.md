# TensorBuzz PR webhook registrar

This host-owned Hermes plugin manages an exact TensorBuzz callback pair for a
pull request: one one-time `build_group.completed` callback scoped to the full
head/build-group generation, and one persistent callback for the supported
GitHub review events. It writes only Hermes' authoritative
`webhook_subscriptions.json`; it does not add a model tool, route store,
poller, cron job, or project-container integration.

## Host configuration

Enable `tensorbuzz_pr_webhook_registrar` in the host's `plugins.enabled`
configuration. Keep the continuation prompt and credential JSON on the host.
The credential file must have mode `0600` and contain `base_url` and `token`.
Never place it, the callback secret, Telegram bot credentials, the prompt, or
Hermes' route store in a project checkout or Compose container.

No command accepts a token or callback secret. JSON receipts contain only
explicit scope, route names, opaque provider IDs, proof booleans, lifecycle
state, and error classes.

## Operator flow

Use the plugin command registered by `ctx.register_cli_command`:

```text
hermes tensorbuzz-pr-webhooks validate \
  --repo OWNER/REPO --tensorbuzz-project-id PROJECT_UUID --pr PR \
  --head FULL_40_HEX_SHA --build-group-id BUILD_GROUP_UUID \
  --deliver telegram:CHAT_ID:TOPIC_ID \
  --continuation-prompt-file /protected/host/continuation.md --workflow-owner
```

After validation, use the same explicit scope with `register`, plus
`--credentials /protected/host/tensorbuzz.json` and
`--public-webhook-base-url https://HOST`. After every push, invoke `rotate`
with the new full head and build group. Use `status` for fresh provider/local
readback. After merge or close, invoke `cleanup` with the exact generation;
provider IDs are deleted first and local routes/leases only after provider
readback.

The bundled host adapter currently fails closed before every provider create,
read, or delete because this checkout contains no authoritative TensorBuzz
scoped callback contract. Therefore operator `register`, `rotate`, and
`cleanup` report `UNMONITORED — registration incomplete` without making a
provider request. A concrete adapter may be enabled only after provider-contract
task `868fc9e5-0999-40cf-bdca-9d29981a62e1` supplies authoritative create,
readback, and delete evidence.

Registration uses a harmless no-tools proof prompt first. It separately proves
signed-v2 ingress acceptance, same-ID replay suppression, one real
webhook-agent admission, the exact proof response and completed session, and a
successful exact-target delivery recorded by Hermes' delivery ledger before activating the supplied
continuation prompt. Workflow-owner routes always have `deliver_only: false`.
A deliver-only route cannot pass workflow-owner proof.

The registrar posts a bounded synthetic signed-v2 callback to the supplied
public base URL and waits for the plugin's secret-free gateway completion
marker. If public ingress, replay suppression, admission, or completion is not
proved, it fails closed with `UNMONITORED — registration incomplete`. This
prevents provider creation or local JSON persistence from being misreported as
monitoring.

## State and recovery

States distinguish `registered_unverified`, `delivery_verified`,
`agent_continuation_verified`, `continuation_active`, `handoff_ready`,
`stale_generation`, `cleanup_pending`, and the literal failure state
`UNMONITORED — registration incomplete`. A stale writer lease is never stolen;
the operator must verify the prior session and explicitly recover/release it.
Rotation proves the new generation before removing the old exact CI callback.
Compensation removes only IDs and routes created by that attempt.

If `WebhookSubscription.createCallback` or ID readback cannot expose active,
event type, one-time, project, PR, and build-group scope, record the missing
non-secret fields and hand the issue to provider-contract task
`868fc9e5-0999-40cf-bdca-9d29981a62e1`. Do not emulate provider scope in
Hermes and do not substitute polling.
