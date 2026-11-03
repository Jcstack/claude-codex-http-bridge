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

On macOS, the login-recovery check attempts to start a watcher when the
inference service is healthy, an existing monitor lock has no observed conflict,
and the PID in a private monitor receipt is confirmed absent using signal zero.
Missing, malformed, inaccessible or ambiguous evidence blocks the attempt. PID
reuse can conservatively prevent recovery. The watcher takes its own exclusive
lock; ordinary concurrent starts using the same lock file are serialized. Lock
file replacement and same-user manipulation remain limitations. This is
best-effort recovery after a monitor exits, not proof of liveness or a live
replacement of an older monitor. The check targets a 120-second cadence and
the new watcher still observes its startup quiet period; scheduling and sleep
can add delay. The hardened controller was installed on the active Mac with a
private rollback, without restarting its login agent or inference services. An
actual reboot was not tested.

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

The installed recovery implementation requests the fixed usage endpoint over
direct, certificate-verified HTTPS; it does not use an environment or system
HTTP proxy and does not follow redirects. This policy also applies to the
on-demand recovery command. Transparent operating-system or network routing
is outside this application's control. If the origin can infer through a proxy
but direct HTTPS to the usage endpoint is blocked, the local quota gate cannot
be safely reconciled by this check. `check_unavailable` does not establish
that this is the cause: local state, TLS, DNS and other failures are possible.
A [local transport fixture](tests/test_account_transport.py) uses only fake
HTTP servers and a synthetic credential: an environment-aware control uses a
fake proxy, while the account helper goes directly to the fake target, refuses
a redirect and does not fall back to that proxy after a fake DNS failure. It
does not test actual TLS, real network policy or the older running monitor.
Adding an explicitly selected proxy requires separate credential, trust and
loopback-isolation review; the helper must not silently forward a local
management key or change from direct to proxied routing.

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

## In-memory source experiment

The separate [memory bootstrap fixture](quota_memory_fixture.py) explores one
part of a future handoff. It checks a caller-supplied source manifest, copies
the reviewed bytes into a bounded frame, and passes that frame through an
inherited unlinked file descriptor to a temporary owned child. The isolated
child checks the frame digest and individual file hashes and loads the listed
Python modules from memory. Synthetic local tests replace source files and a
published snapshot after capture, corrupt a frame and its inherited descriptor,
and try an unlisted local import. The descriptor corruption is performed by
the test parent before the owned child reads it; it is not evidence that an
unrelated process could access that descriptor. A separate synthetic case runs the actual recovery function against
fake account, usage and reset services, with a shortened polling/quiet interval
and a stub account lock. It demonstrates a recovery path in the fixture, not
real upstream behavior or concurrent lock correctness. The tests do not use
real account credentials or signal the user's existing watcher.

The [initial gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41650357114)
passed on macOS and Linux; Windows ran the broader suite but skipped the
POSIX-descriptor fixture. A later macOS-only coordinator control substituted
the memory child, replaced the snapshot during startup, and restored it before
the coordinator rechecked. The changed source did not run; the signal was
mocked, the prior owned synthetic watcher stayed alive, and both test children
were cleaned up. This does not demonstrate an actual handoff or authenticate a
production replacement process.

A separate macOS fixture used a real generation-bound signal only on its
own synthetic monitor. It waited for a separate fixture to release the account
lock, reported a new protocol-2 child and ledger, and left an unrelated owned
process alive. The existing read-only identity observer rejects that memory
child because it recognizes script-based commands, so repeated handoff and
identity verification remain unimplemented. A second synthetic handoff
attempt refused before spawning or signaling and left the successor alive.
That fixture passed in the [three-platform gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41651716086);
the owned signal and handoff test runs only on macOS.
This does not authorize signaling the real monitor or establish safety for an
in-flight real quota request.

This is not a production launcher or OS sandbox. It trusts the manifest, parent
code, interpreter and standard library; it does not prove protection against
same-user manipulation of the parent or child process, complete dependency
coverage, signal-safe identity, actual quota recovery, or Windows handoff.
The experiment remains separate from the fixture-only handoff coordinator and
is not installed or used by the running service.

## Cooperative exec observation

The isolated [exec-lock fixture](tests/test_quota_exec_lock_fixture.py) explores
a possible later-generation watcher that cooperatively replaces itself. On a
temporary local filesystem, an owned process holds an advisory `flock` and
passes its open descriptor across `execve`; a separately opened contender
repeatedly tries the lock before, during and after the transition. A
close-on-exec control verifies that the contender can observe a released lock
while the replacement process remains alive. A second contender blocks in
`flock` and acquires only after the exec owner releases the lock. The replacement
checks its PID and descriptor's file identity. This is a bounded fixture, not a proof that every
instant was observed or that an open descriptor is itself proof of lock
ownership. Linux's [flock manual](https://man7.org/linux/man-pages/man2/flock.2.html)
documents preservation across `execve`; Apple's [flock manual](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/flock.2.html)
describes advisory lock and duplicate-descriptor behavior. Platform and
filesystem semantics still require their own checks.

Two further local controls distinguish failure modes: when a synthetic
`execve` fails before replacing the process, the old test process continues
holding its lock; when the replacement crashes after a successful exec, the
contender acquires the released lock. Recovery from a crash therefore still
needs a separate verified monitor and durable probe-throttling policy.
The controls passed on macOS and Linux in the [three-platform gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41653951055);
the POSIX fixture was skipped on Windows.

This fixture does not implement an updater, authenticate a source manifest,
drain an in-flight account operation, handle process crashes, or preserve a
lock on Windows. It cannot upgrade the current legacy watcher, which has no
cooperative self-exec protocol. It is not installed or connected to the
running service.

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
