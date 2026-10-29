# Security

Never commit or publish OAuth JSON, access or refresh tokens, API keys, `config.yaml`, `proxy.sha256`, proxy binaries, logs, Claude transcripts, model caches, or files from an installed runtime directory. This repository can be public; Git history must never contain credentials.

The proxy writes OAuth credentials only after the user completes `claude-codex codex-login`. On POSIX the installer creates the installed root and OAuth directory with mode `0700`, and config/credential files with mode `0600`. On native Windows it requires NTFS and applies protected ACLs that allow only the current user to access the installation and runtime data. The shared localhost service receives a random key when it starts and keeps it in a private file until `claude-codex bridge stop`. The proxy accepts requests only on `127.0.0.1`; stop it after background work has finished.

Optional HUD quota telemetry queries the official Codex app-server only when the native Codex login and the bridge's single OAuth login have the same account ID. The wrapper reads these IDs locally to compare them; it does not copy tokens or credential files. The private runtime cache stores only reported quota percentages, reset times, a fetch timestamp, and a SHA-256 account fingerprint. The wrapper does not log query subprocess output or errors. If the accounts differ or the service does not report a window, HUD does not invent a value.

Review updates to the pinned proxy release and its security advisories before changing the version or hashes. Do not enable LAN binding, request body logging, or the management interface without a separate security review.

Browser MCP is opt-in. When used, page text and screenshots returned by the tool enter the Claude Code conversation and are sent to the Codex model endpoint. The recommended isolated browser starts without everyday Chrome cookies. Attaching to a signed-in Chrome profile gives the agent access to that profile's tabs and storage, so it requires an explicit per-session choice and normal tool permissions.

If a credential is accidentally committed, revoke or rotate it first, then remove it from the repository and its full Git history before any further push. Deleting only the latest file does not remove it from earlier commits.
