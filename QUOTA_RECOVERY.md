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
credential-wide quota gate permits an automatic upstream usage query. Healthy
operation makes no extra upstream usage queries.

On macOS, the source login-recovery check also attempts to start a watcher
when the inference service is healthy and a read-only observation finds no
conflicting monitor lock. The watcher takes its own exclusive lock, so a
concurrent start cannot create two active watchers. An unavailable, unsupported
or conflicting observation does not trigger a start. This is best-effort
recovery after a monitor exits, not proof of its liveness or a live replacement
of an older monitor. The check targets a 120-second cadence and the new watcher
still observes its startup quiet period; scheduling and sleep can add delay.
This source change is not yet installed locally.

The source now keeps automatic probe timestamps in a private, bounded ledger,
using only a short account fingerprint and monotonic time. It writes a
reservation before contacting the usage endpoint, so a later monitor can retain
the one-minute reservation interval after a process crash or replacement.
This bounds reservations, not exact provider request start times: scheduling,
filesystem delays and an old request still in flight can change their spacing.
Each monitor start begins a conservative one-minute global quiet period before
its first automatic probe. This covers a legacy monitor that exited before
writing any status and avoids comparing monotonic values across system boots.
The 30-second polling cadence can add delay after the quiet period. Malformed,
oversized, inaccessible or symlinked state blocks automatic probing rather than
resetting the limiter. A manually requested `quota recover` remains a separate
immediate check and is not governed by this automatic ledger.
The checks cover process termination, not an operating-system crash or power
failure; filesystem durability and clock behavior have platform limits. A
valid-looking ledger is not cryptographically authenticated against same-user
tampering or rollback.

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
The local monitor source has been updated, but the existing monitor process
was deliberately left running on its previous code.

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
claude-codex quota status
claude-codex quota recover
```

`status` reads only the last allowlisted monitor result, reported protocol and
whether probe state passes structural and local-permission checks. It does not
authenticate the ledger, contact a service, report account or process
identifiers, or establish that the monitor is still alive. On macOS it also
uses `F_GETLK` to report a conflicting lock, no conflict, missing lock file, or
an unavailable observation without taking the lock. A conflicting lock does
not identify its owner, and either result can change immediately. No conflict
does not prove a watcher is absent. Other platforms currently report this
observation as unsupported. A missing
protocol can indicate an older monitor or unavailable state. `recover`
performs the checks once. `recovered` means the local stale gate was
released; `no_stale_credential_quota` means no matching local gate needed work.
Other statuses preserve the gate. The command does not enable a project-API
profile or change the current Claude conversation. A running client can retry
its message after recovery without being closed.

Updating the source makes `status` available to new command invocations. An
older monitor already in memory does not gain the ledger or identity checks
until it is safely replaced. An absent ledger or missing protocol in `status`
therefore does not prove that the running monitor uses the new behavior.

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
