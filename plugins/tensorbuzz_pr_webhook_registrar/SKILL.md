---
name: tensorbuzz-pr-webhook-registrar
description: Operate exact-generation TensorBuzz PR callbacks from the protected Hermes host.
---

# TensorBuzz PR webhook registrar

Use `hermes tensorbuzz-pr-webhooks` only on the Hermes host. First run
`validate` with explicit repository, project UUID, PR, full 40-hex head,
build-group UUID, exact `telegram:CHAT:TOPIC`, and a host prompt file. Then run
`register`; run `rotate` after every push; run `cleanup` after merge/close.

Never request, print, copy, or pass TensorBuzz tokens, callback secrets,
Telegram tokens, prompt contents, receipt-store contents, or
`webhook_subscriptions.json`. Refer to credentials only by a protected host
file path. Never enter a project container for these operations.

Do not accept a provider create, local route, Telegram notification, or signed
delivery alone as success. Require provider/local readback, signed-v2 delivery,
same-ID duplicate suppression, exactly one agent admission, and exactly one
completion marker. A workflow-owner route must have `deliver_only: false`.

If any step fails, report `UNMONITORED — registration incomplete` and verify
attempt-owned compensation. Never fall back to polling or cron. For a missing
scoped provider contract, record only missing field names and cite task
`868fc9e5-0999-40cf-bdca-9d29981a62e1`.
