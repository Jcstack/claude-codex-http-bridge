# Recovering from an early quota reset

The provider can restore allowance before a previously reported weekly retry
deadline. An origin with that old local
credential-quota gate may never make another inference request until the old
deadline. The managed translator could also retain a second copy of the gate,
even after the origin recovered or its selected account changed.

The guarded local translator now delegates cooldown ownership to the origin.
Its upstream is still authenticated and restricted to the private origin route;
actual provider 429 responses and retry hints are preserved. Direct project-key
profiles retain their own normal cooldown protection.

## Automatic reconciliation

Subscription launches and `bridge start`/`bridge reload` ensure one private
monitor per installation. It does not start or restart an inference service.
It checks the local selected-account state every 30 seconds. Only an active,
credential-wide quota gate permits an upstream usage query, at most once per
minute per account. Healthy operation makes no extra upstream usage queries.

Recovery requires a fresh response from the selected account's fixed verified
HTTPS usage endpoint: `allowed=true`, `limit_reached=false`, reported windows
below 100%, and any additional model limits also available. Missing,
inconsistent or exhausted data is rejected. Model-only rate throttles and
authentication failures are not cleared by this mechanism.

This source also refuses automatic recovery if the selected bridge
record does not contain a person identifier in its local metadata. A shared
workspace ID alone is not enough to bind a quota result to one person. This is a
fail-closed compatibility limitation: the local gate remains in place until
the identity is available. A monitor already running older code retains that
code until safely restarted; changing its source file alone does not reload it.

After reading usage, the monitor revalidates the selected identity, the
PID-owned origin listener and the current quota-only restriction. Under the
account-operation lock, it calls the existing localhost V8
`routing/cooldown/reset` operation for exactly that credential. It never redeems
credits, changes accounts, renews tokens, alters normal reasoning effort, runs
model inference, replays a tool action, or cancels an existing stream. A real
upstream restriction can still reject the next request and establish a new gate.
Failed checks do not justify bypassing it.

The monitor exits when its origin is no longer running. Its lock and allowlisted
status stay in the private `runtime-quota` directory. No prompts, account IDs,
credential values, raw usage bodies or upstream errors are written there.
Workspace proxy settings and API keys are not inherited by the helper. It uses
the existing origin refresh owner; no second refresh owner is created. The
ordinary `usage` and `diagnostics` commands remain read-only.

## On-demand check

```text
claude-codex quota recover
```

This performs the same checks once. `recovered` means the local stale gate was
released; `no_stale_credential_quota` means no matching local gate needed work.
Other statuses preserve the gate. The command does not enable a project-API
profile or change the current Claude conversation. A running client can retry
its message after recovery without being closed.

## Verification

The installed Astra route initially returned a local cooldown of about 5.8 days
while fresh selected-account usage was 0%. After targeted reconciliation and
releasing the duplicate relay gate, synthetic Responses and Messages requests
both returned 200 in about 2.2 seconds. Origin, ingress, account selection and
local keys were preserved; no user prompt was replayed.

The credential-free managed-runtime gate now injects a 429 with a seven-day
retry hint, restores the fake origin, and requires the very next request to
reach it successfully without restarting the relay. The separate transport
gate still requires genuine credential cooldowns to suppress repeated upstream
calls. Recovery tests cover exhausted/unknown/model-specific quota, auth errors,
account races, throttled checks, exclusive monitoring and isolated child imports.
