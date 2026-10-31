# Security

Never commit or publish OAuth JSON, access or refresh tokens, API keys, `config.yaml`, `proxy.sha256`, proxy binaries, logs, Claude transcripts, model caches, or files from an installed runtime directory. This repository can be public; Git history must never contain credentials.

The optional `claude-codex api` profile stores a project API key only in the installed root's `openai-api-key` file (0600 on POSIX, protected current-user NTFS ACL on Windows). Its separate `runtime-api` service config temporarily contains that key while the API bridge runs; the service binds loopback and requires a separate random client key. The profile sends the project key only to the fixed official `api.openai.com` model-list and Responses endpoints. Official model documentation is fetched without a credential, and redirects are rejected during metadata discovery. The API proxy uses a separate empty `api-auth` credential directory, so it does not load the subscription OAuth files. Stop its service after background work finishes to remove the per-service config. API Platform data-retention rules still apply to requests sent to OpenAI.

Private file permissions protect the key from other OS users, not from programs running as your own user. Claude Code [command hooks run with the user's file permissions](https://code.claude.com/docs/en/hooks#security-considerations); review project hooks and plugins before starting an API-key session in an unfamiliar repository. For an untrusted checkout, Claude Code documents `--bare` or disabling hooks for that run. The bridge itself never installs a remote classifier or sends task excerpts to a third-party routing service.

The proxy writes OAuth credentials only after the user completes `claude-codex codex-login`. On POSIX the installer creates the installed root and OAuth directory with mode `0700`, and config/credential files with mode `0600`. On native Windows it requires NTFS and applies protected ACLs that allow only the current user to access the installation and runtime data. The shared localhost service receives a random key when it starts and keeps it in a private file until `claude-codex bridge stop`. The proxy accepts requests only on `127.0.0.1`; stop it after background work has finished.

Optional HUD quota telemetry follows exactly one enabled bridge account and excludes disabled backups. It uses the official Codex app-server only when native Codex has the same account ID. Otherwise it reads the selected bridge credential locally and sends its access token only to the fixed `https://chatgpt.com/backend-api/wham/usage` endpoint, with verified TLS and no redirects or ambient proxies. Native Codex auth is never replaced to make these accounts match. The private runtime cache stores only reported quota percentages, reset times, a fetch timestamp, and a SHA-256 account fingerprint. It rejects old-account caches and never invents a missing window. Raw usage responses and credentials are not logged.

[Account authorization and switching](ACCOUNTS.md) use a loopback-only OAuth callback and fixed OpenAI endpoints. Switching uses the already-enabled, PID-verified loopback management interface and its retained private credential, restricted in this helper to auth-file listing, model publication, upload and status changes. No LAN binding or management-panel access is enabled. Validated target credentials are enabled before old credentials are disabled; existing credentials are retained, never deleted. An account-only recovery journal stays private until the selection is verified. An isolated validator may copy an account record into a temporary private directory; it never copies it into this source repository or the native Codex directory.

Installed inactive-account validation receives only an access-token snapshot;
outer and nested refresh grants are excluded because the origin still refreshes
disabled credentials. A new staged grant may refresh privately before it is
installed. Older interrupted refresh writes retain private checkpoints outside
the repository; recovery never enables an account implicitly or overwrites a
newer generation. Checks and account changes share a lock.

Model restoration keeps only a model ID, a workspace-path hash and a timestamp in the private runtime, separated by Claude configuration root and provider route. Session IDs name the files. The transcript fallback reads a bounded tail and extracts parent-message model metadata; it never prints or copies message/tool content, titles, or raw workspace paths into that ledger. Unknown models and sidechain replies do not become a parent-model selection. The status-line fast path writes nothing when the model is unchanged.

The optional [managed runtime](RUNTIME.md) uses a private loopback TLS identity, a stable frontend key and a separate origin guard key. The translator's configured credential is only the guard key; the ingress verifies the original listener PID before sending its localhost key. It keeps OpenAI OAuth in the original service. New runtime binaries are checked against a platform/source lock and their hashes, and are installed into immutable directories. Those hashes do not replace trusted build provenance. Private CA keys, per-launch environment settings, certificates and runtime logs are never part of the source bundle. Proxy children start from private runtime directories so a project `.env` is not loaded incidentally.

The read-only diagnostic report never opens key files or contacts an endpoint. It emits only allowlisted counters, timing, hashed tags and compaction metadata. Origin and translator observations may describe the same request and are not additive. It does not export raw transcripts, error bodies or project paths.

Review updates to the pinned proxy release and its security advisories before changing the version or hashes. Do not enable LAN binding, request body logging, or the management interface without a separate security review.

Browser MCP is opt-in. When used, page text and screenshots returned by the tool enter the Claude Code conversation and are sent to the Codex model endpoint. The recommended isolated browser starts without everyday Chrome cookies. Attaching to a signed-in Chrome profile gives the agent access to that profile's tabs and storage, so it requires an explicit per-session choice and normal tool permissions.

The bridge's WebFetch PDF hook processes the tool result locally and replaces only the binary-PDF summary with a warning and the path Claude Code already returned. It does not upload or copy the PDF. Claude Code's own WebFetch may leave downloaded binaries in its session `tool-results` directory; the hook does not remove them. Review that directory according to your local retention policy. [Upstream retention report](https://github.com/anthropics/claude-code/issues/104178).

If a credential is accidentally committed, revoke or rotate it first, then remove it from the repository and its full Git history before any further push. Deleting only the latest file does not remove it from earlier commits.

The optional [native capability pack](NATIVE_TOOLS.md) pins dependency hashes and immutable source snapshots. Native media calls use only the private TLS endpoint key, never OAuth. Workspace inputs reject symlink/reparse paths, absolute paths and traversal; artifacts stay private. No microphone recording or automatic playback is implemented. Desktop requires an explicit per-workspace window selection and existing OS permissions. The child receives no model credentials or global-input opt-in. Windows dispatches each action directly to one native worker, avoiding upstream uncertain-action retries, and disables screen-region GDI. These are application guardrails, not an OS sandbox against same-user programs or the selected app itself.

New launches default `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, respecting an
explicit operator override. This follows [Claude's documented telemetry control](https://code.claude.com/docs/en/data-usage#telemetry-services);
it can disable Remote Control/feature-flag evaluation and is not a zero-egress
promise. The built-in WebFetch domain safety check and plugin/tool networking
have separate behavior and remain under Claude's own controls. The bridge does
not silently remove those safety checks. Native MCP Python uses isolated mode;
workspace Python startup files cannot execute before its reviewed source.

Capability dependency updates are hash-locked and checked against OSV using only
the public lockfile names/versions. Pillow 13.0.0 replaces the initially selected
12.2 release after upstream advisory review. Codecs are restricted to the formats
the tool accepts. A clean advisory query is not a guarantee against unknown
vulnerabilities, nor do release hashes establish reproducible-build provenance.
