# Stable loopback ingress

This standard-library-only Go process provides a fixed authenticated endpoint
while model backends change. It is activated by the optional source-locked
[runtime controller](../RUNTIME.md), which is now used for new launches on the
reviewed local installation. It does not migrate already-open sessions from
the legacy direct proxy listener.

Every admitted request retains its backend snapshot until the stream ends. A
new request reads the current private manifest. Replacing that manifest changes
new-request routing without closing existing streams. The authenticated
`GET /bridge/status` reports per-generation active counts so a controller can
wait for the old generation to reach zero before stopping its backend. The
gateway does not stop backends itself or retry partially delivered requests.

Build and test from this directory:

```sh
go test -race -timeout 60s ./...
go build -o /private/path/bridge-ingress .
```

On native Windows use `go test -timeout 60s ./...` when a race-capable C toolchain
is unavailable. The Windows tests protect fixture directories with NTFS ACLs.

Create a private runtime directory, a stable random frontend key, and a separate
backend key file. The backend manifest contains only local routing metadata:

```json
{
  "generation": "candidate-a",
  "port": 18317,
  "pid": 12345,
  "key_file": "/private/runtime/backend-key"
}
```

Use real paths and the actual verified backend listener PID. Never publish a
runtime directory, key file or manifest. Files must be inside the private root
and may not be symlinks. POSIX directories require mode 0700 and files no group
or other access. Windows requires a protected current-user-only root ACL.

```text
bridge-ingress --root /private/runtime --init-tls
bridge-ingress --root /private/runtime --listen 127.0.0.1:18318 --key-file /private/runtime/frontend-key --backend-file /private/runtime/backend.json --tls
```

Before a handoff, start and authenticate-check the replacement backend, write a
new generation manifest to a private temporary file, then atomically replace
`backend.json`. Poll `/bridge/status` with the frontend key. Keep the old backend
alive until its active count is zero. Do not reuse a generation ID for changed
configuration. Invalid manifests fail closed for new requests; restoring a
valid manifest provides rollback. Backend ownership is checked **after the TCP
connection is established and before sending the backend key** (`lsof` on
POSIX, `netstat` on Windows).

The gateway binds only 127.0.0.1, requires a local key, forwards only GET/POST
under `/v1/`, and never forwards management endpoints. Request bodies are bounded
at 64 MiB. It replaces inbound credentials with the backend's private key and
preserves session/agent headers for cache identity. Streams flush immediately;
client cancellation propagates upstream. Raw requests and upstream errors are
not logged. `/bridge/status` is metadata-only and requires authentication.

Tests verify an old stream completing on backend A while a new request reaches
B, cancellation, key replacement, session-header preservation, unauthorized
requests, wrong-PID rejection, invalid manifests and rollback. This proves the
isolated routing primitive. A [real subscription probe](../tests/live_gateway_handoff.py)
also completed admitted requests across two handoffs and replayed a tool result
and encrypted reasoning. Cache reuse succeeded in one handoff and missed in
another; see [the measured results](../OPERATIONS.md). The optional runtime
controller is integrated and activated locally, with three-platform real-client
fixtures and a persistent live subscription session. Sustained load tests and
replacement of the existing direct origin remain open. Plain HTTP mode is
retained for isolated fixtures; the managed controller always enables TLS.
Restarting this ingress process itself is not a seamless operation; it must
remain running while its dependent sessions execute.
