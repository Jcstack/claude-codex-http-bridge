# Ingress and original-listener migration

This is an implementation and acceptance plan, not an enabled migration. The
running Claude clients, original service and managed ingress are not restarted
by this document. Claude Code remains the harness, and interrupted tool calls
must never be silently replayed.

## Current boundary

The managed backend can be replaced while an admitted stream drains. Existing
managed clients connect to one loopback TLS port and trust its private CA.
The current ingress owns its listener itself through `ListenAndServeTLS`; it
does not expose a supported listener handoff protocol to a successor. Clients
connected directly to the older original endpoint have a different port and
do not pass through the managed ingress. The original service also owns the
subscription credentials and account-management interface.

Changing only a later settings file is not a proven migration for every open
Claude process: startup settings can have higher precedence and the observed
client did not reload those settings in the tested configuration. An actual
computer reboot terminates running processes and streams; reusing a port and
CA on login can support later reconnection or resume but cannot complete an
interrupted stream retroactively.

## Candidate path for new managed installations

1. Start a small, reviewed socket owner before the gateway workers. Bind only
   `127.0.0.1` and retain the existing private TLS and client-key boundary.
2. Give each reviewed worker an authorized duplicate of the listener using an
   OS-specific mechanism. A worker handles its accepted connections; a
   replacement accepts new connections while the previous worker drains.
   The supervisor authenticates its control channel and verifies executable
   identity and bundle hashes before passing any listener or secret.
3. Prepare the replacement, test both authenticated routes and only then
   change admission. On failure keep the old listener and worker; never replay
   an already admitted request. Record enough private state for crash recovery
   without writing prompts, OAuth material or raw request bodies to the repo.
4. A login recovery path must reuse the private CA, key and saved loopback port,
   refuse an occupied port, and avoid signaling a process based only on a stale
   PID. A clean reboot, crash during handoff and account switching need separate
   tests; none is proven by a successful catalog check.

Go's [`Server.ServeTLS`](https://pkg.go.dev/net/http#Server.ServeTLS) can serve
an already supplied listener. On macOS,
[`launch_activate_socket`](https://developer.apple.com/documentation/xpc/launch_activate_socket)
returns descriptors for sockets configured for a launchd job. On Linux,
[systemd socket activation](https://www.freedesktop.org/software/systemd/man/latest/daemon.html)
is one option. On Windows,
[`WSADuplicateSocket`](https://learn.microsoft.com/en-us/windows/win32/api/winsock2/nf-winsock2-wsaduplicatesocketa)
provides a cooperative sharing mechanism. These are platform primitives, not a
tested bridge design or proof of identical semantics across the three systems.
The solution should also work without requiring an administrator or trusting an
unrelated local listener. Socket activation and login services are optional
platform integrations, not a reason to drop ordinary manual startup.

## Existing ingress and direct sessions

The present ingress cannot be assumed to have handed its socket to such a
supervisor. Rebinding the same port or killing it would interrupt active
connections; accepting a new port does not by itself update old clients.
Whether a sidecar can be inserted behind the old ingress to strengthen origin
verification without touching existing streams is an experiment, not an
implementation. A sidecar with its own guard key could reduce exposure of the
original service key, but the old ingress would still authenticate that sidecar
only by its existing PID/port check. A same-user impersonator could therefore
receive the guard key or request data in a race. Sidecar failure and restart
could also temporarily fail new requests. It must not be marketed as complete
same-user isolation or as a replacement for ingress migration.

The original endpoint has the same first-migration problem and also owns OAuth
state. Any future direct-session migration needs to preserve account selection,
native protocol state and cache identity, protect management credentials, and
provide rollback without changing the user's native Codex login. Until a proven
handoff exists, let direct sessions finish on their existing service and use
the managed endpoint for new sessions; resuming a session is a new client
process, not proof that the old stream was preserved.

## Controller-source publication

The current installer replaces controller modules individually. An immutable
versioned source directory and an atomic selection are a possible way to avoid
mixed module generations, but moving the current files there is not sufficient.
On this Mac, a disposable directory experiment set the launcher's installation
root to a synthetic parent while loading copied code from a child generation.
The launcher used the parent, while the model, usage and status-line helpers
still resolved their root from the child. A synthetic status-line update wrote
its model ledger under the child. The autostart entry's current source-directory
check would reject that parent, and a source lock stored only at the parent was
not found by the copied runtime module. These are observations of a deliberately
naive layout, not evidence of a defect in the running installation.

A safe design must bind each launched process to one complete, verified source
generation while keeping credentials, account selection, service state, login
recovery, status-line preferences and session ledgers in the authorized private
installation root. Executables spawned later by a running controller must use a
consistent generation. Define how the generation and root are authenticated,
how Windows ACLs and macOS launch agents are handled, what happens if the
selection or files are replaced during a launch, how old processes retain their
source files, and how rollback and cleanup avoid deleting an in-use generation.
Existing sessions and autostart state cannot be assumed to migrate merely
because an atomic pointer changes. No versioned installer is activated.

## Acceptance before any live activation

- Exercise new and old real locked Claude clients with fake models, including
  active streams, parallel tools, cancellation, background work, resume and
  large requests. Verify each independently observed tool effect at most once.
- Cover controller/worker crash at every publication point, a busy listener,
  PID reuse controls, malformed private records, invalid CA or keys, failed
  readiness, rollback, and loss of the supervisor. Existing work must either
  complete or return an explicit error; never invent a completed event.
- Run native macOS, Linux and Windows tests, then actual user-login recovery on
  each supported host. Tests after a real reboot must be distinguished from a
  synthetic process restart and require a separate safe opportunity.
- Measure local latency, resource use and cache behavior with repeated paired
  samples. A stable local session key does not prove an upstream cache hit.
  Check current client versions and representative plugins without accessing
  the user's real data in fixtures.
