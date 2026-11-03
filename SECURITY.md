# Security

Never commit or publish OAuth JSON, access or refresh tokens, API keys, `config.yaml`, `proxy.sha256`, proxy binaries, logs, Claude transcripts, model caches, or files from an installed runtime directory. This repository can be public; Git history must never contain credentials.

The optional `claude-codex api` profile stores a project API key only in the installed root's `openai-api-key` file (0600 on POSIX, protected current-user NTFS ACL on Windows). Its separate `runtime-api` service config temporarily contains that key while the API bridge runs; the service binds loopback and requires a separate random client key. The profile sends the project key only to the fixed official `api.openai.com` model-list and Responses endpoints. Official model documentation is fetched without a credential, and redirects are rejected during metadata discovery. The API proxy uses a separate empty `api-auth` credential directory, so it does not load the subscription OAuth files. Stop its service after background work finishes to remove the per-service config. API Platform data-retention rules still apply to requests sent to OpenAI.

Private file permissions protect the key from other OS users, not from programs running as your own user. Claude Code [command hooks run with the user's file permissions](https://code.claude.com/docs/en/hooks#security-considerations); review project hooks and plugins before starting an API-key session in an unfamiliar repository. For an untrusted checkout, Claude Code documents `--bare` or disabling hooks for that run. The bridge itself never installs a remote classifier or sends task excerpts to a third-party routing service.

The proxy writes OAuth credentials only after the user completes `claude-codex codex-login`. On POSIX the installer creates the installed root and OAuth directory with mode `0700`, and config/credential files with mode `0600`. On native Windows it requires NTFS and applies protected ACLs that allow only the current user to access the installation and runtime data. The shared localhost service receives a random key when it starts and keeps it in a private file until `claude-codex bridge stop`. The proxy accepts requests only on `127.0.0.1`; stop it after background work has finished.

Optional HUD quota telemetry follows exactly one enabled bridge account and excludes disabled backups. It uses the official Codex app-server only when native Codex has the same account ID. Otherwise it reads the selected bridge credential locally and sends its access token only to the fixed `https://chatgpt.com/backend-api/wham/usage` endpoint, with verified TLS and no redirects or ambient proxies. Native Codex auth is never replaced to make these accounts match. The private runtime cache stores only reported quota percentages, reset times, a fetch timestamp, and a SHA-256 account fingerprint. It rejects old-account caches and never invents a missing window. Raw usage responses and credentials are not logged.

The source's one-time `quota recover --usage-proxy-port PORT` option explicitly
selects an unauthenticated HTTP CONNECT listener at `127.0.0.1:PORT` for the
fixed usage endpoint. This does not enable a background proxy. TLS checks the
official hostname using the configured system trust; a proxy able to present a
trusted interception certificate can read the account token. The proxy sees
the destination and connection timing, and the bridge does not authenticate
which local process owns the port. The local management credential is never
passed to that proxy. The option refuses redirects, ambient-proxy and direct
fallbacks, and does not activate ambient `SSLKEYLOGFILE` logging. Synthetic
tests and the local no-restart installation are described in
[quota recovery](QUOTA_RECOVERY.md). It has not been tested with a real proxy
or an authenticated Windows account.

An additional source change strengthens that account binding. A workspace ID
alone does not distinguish people within a shared workspace: the source now
requires a nonempty person identifier from local credential metadata before
using a cached quota or attempting quota reconciliation. Native app-server
quota also requires matching person metadata, and is discarded if either
selected identity changes during the read. The metadata is not independently
verified as a cryptographic proof of identity. These are before-and-after
checks, so a change away and back between observations is not excluded.
When the bridge person is known but the native person is unavailable, the
fixed selected-bridge endpoint remains the fallback. If the bridge person is
unknown, quota display and automatic stale-cooldown reset remain unavailable
instead of assuming two accounts are the same. The [three-platform identity
gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41534034405)
passed synthetic account, race and detached-helper controls. The local helper
and monitor source were installed with private rollback copies; isolated
imports and a same-account read-only quota check passed. New helper processes
and on-demand reconciliation use the checks, but the existing long-running
monitor was not restarted and continues using the code it already loaded.

The earlier quota refresh helper and its native app-server inherited the full
parent environment. A source change restricts both to operating-system,
native `CODEX_HOME`, TLS and explicitly configured proxy settings, and starts
the Python refresh helper in isolated mode. Local synthetic controls confirm
that unrelated API keys and Python import paths are absent while the account
directory and configured proxy remain available. This retains configured
proxy and certificate trust, PATH and native Codex configuration; it is not an
OS sandbox or proof about processes launched by Codex itself. The
[three-platform gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41532735109)
also covers a detached refresh using an owned fake app-server, with native
Windows ACL handling. A local macOS install retained a private rollback copy,
passed an isolated import and a same-account, read-only quota check, and did
not restart the origin, ingress or clients. Existing helper processes keep
their former environments until they exit. A real Windows account, login and
every possible proxy configuration remain unverified.

[Account authorization and switching](ACCOUNTS.md) use a loopback-only OAuth callback and fixed OpenAI endpoints. Switching uses the already-enabled, PID-verified loopback management interface and its retained private credential, restricted in this helper to auth-file listing, model publication, upload and status changes. No LAN binding or management-panel access is enabled. Validated target credentials are enabled before old credentials are disabled; existing credentials are retained, never deleted. An account-only recovery journal stays private until the selection is verified. An isolated validator may copy an account record into a temporary private directory; it never copies it into this source repository or the native Codex directory.

Installed inactive-account validation receives only an access-token snapshot;
outer and nested refresh grants are excluded because the origin still refreshes
disabled credentials. A new staged grant may refresh privately before it is
installed. Older interrupted refresh writes retain private checkpoints outside
the repository; recovery never enables an account implicitly or overwrites a
newer generation. Checks and account changes share a lock.

Model restoration keeps only a model ID, a workspace-path hash and a timestamp in the private runtime, separated by Claude configuration root and provider route. Session IDs name the files. The transcript fallback reads a bounded tail and extracts parent-message model metadata; it never prints or copies message/tool content, titles, or raw workspace paths into that ledger. Unknown models and sidechain replies do not become a parent-model selection. The status-line fast path writes nothing when the model is unchanged.

The optional [managed runtime](RUNTIME.md) uses a private loopback TLS identity, a stable frontend key and a separate origin guard key. The translator's configured credential is only the guard key; the ingress verifies the original listener PID before sending its localhost key. It keeps OpenAI OAuth in the original service. New runtime binaries are checked against a platform/source lock and their hashes, and are installed into immutable directories. Those hashes do not replace trusted build provenance. Private CA keys, per-launch environment settings, certificates and runtime logs are never part of the source bundle. Proxy children start from private runtime directories so a project `.env` is not loaded incidentally.

The Python service controller additionally identifies the process creation
instance when creating or reusing an origin. Its private state retains only a
hash of the PID, creation identity and executable path. Legacy state without
that hash is adopted only after the listener's executable matches this
installation; identity or listener mismatches fail closed. This narrows PID
reuse risk after reboot for controller operations. Account management rechecks
the owner before each management request. It does not yet add process
birth verification to each origin request in the already-running Go ingress;
that hop still checks PID and port before sending the localhost key.
Linux controller parsing reads process-stat data as bytes before extracting the
ASCII start tick, so a kernel-truncated multibyte process name cannot by itself
make an otherwise valid identity unavailable. A cross-platform native gateway
candidate adds a private birth/executable identity check before origin bytes
are sent, with fail-closed handling when the native implementation is missing.
It is not enabled in the already-running ingress by a controller or translator
update; see the candidate and migration limitations in [RUNTIME.md](RUNTIME.md).

New origin processes receive an allowlist of operating-system settings and
explicitly configured HTTP/SOCKS proxy variables, rather than the full shell
environment. This prevents unrelated credential variables and the upstream
proxy's ambient remote-store selectors from changing credential storage.
The isolated account validator uses its existing direct-network policy with
the same restricted environment. A process started before this change keeps
its original environment until it exits; the current origin was not restarted
to apply it. A local check found none of the known storage or management
override variable names in that existing process, without reporting values.

The current source additionally gives the disposable account-validation proxy
its own temporary HOME and, on Windows, temporary USERPROFILE and AppData
directories. Its explicitly selected CA environment is retained, and the
existing direct-network policy is unchanged. A synthetic child confirmed the
environment boundaries in the [three-platform initial gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41646045132).
A later test also paired a former-HOME control with a real model helper
against a fake loopback proxy. Its [expanded gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41646534202)
passed on Linux and Windows but timed out in the macOS real-helper fixture.
A [stage-instrumented rerun](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41647595830)
still failed on macOS during synthetic HTTP-server construction. A local
negative control showed that a stalled reverse lookup can reproduce that stage;
the exact runner cause remains unknown. The fixture explicitly rejects reverse
lookup and uses a numeric loopback identity; its [revised three-platform gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41648079447)
passed. Four synthetic checks with copies of the installed dependencies passed,
and the controller was installed atomically with a private rollback. Read-only
account, quota, tools, autostart and model-list checks passed; no service or
session was restarted. HOME redirection is not an OS sandbox: same-user
filesystem access, system credential stores, arbitrary native code and other
proxy launch paths are not isolated by it.

The source now restricts separate model-catalog helper processes to selected
operating-system and routing variables, retaining the requested PDF-hook and
Windows ACL-debug settings. It also starts those Python children without
environment-based Python configuration or site initialization, and uses UTF-8
for captured output. A synthetic control showed that the earlier child could
inherit an unrelated token-shaped variable and execute a `sitecustomize.py`
from an injected `PYTHONPATH`; it does not show a real credential exposure.
A loopback-only catalog fixture exercises dynamic model and compact-window
selection, a separate synthetic API catalog, and a Unicode installation path.
The parent launcher, other subprocesses, user HOME, filesystem, and same-user
programs remain outside this child-level restriction. This is not an OS
sandbox. The launcher change was installed on the active Mac as a single
atomic replacement, with a private rollback copy and read-only help, model-list
and quota-status checks. The shared policy module and account controller were
then installed in dependency-first order, with a private account-controller
rollback copy and additional read-only account-list and native-tool checks.
The already-running services were not restarted, and in-memory processes keep
the versions they already loaded. The installed launcher retains the equivalent
standalone policy until a later source update.

The earlier parent launch path started Python with normal site initialization.
A temporary fake launcher demonstrated that an injected `PYTHONPATH` could run
synthetic startup code before the launcher; this is not evidence of an actual
credential exposure. The wrapper uses `-E -s -X utf8`, which ignores Python
environment configuration for this interpreter, disables the user site, and
selects UTF-8 while keeping the environment
available to Claude and plugins. The selected interpreter's site and its
`.pth` files still run; the selected interpreter remains a trust boundary. A
separate Python 3.11 virtual environment showed that the stronger `-S` option
changes `sys.prefix` even though creating a child environment with pip worked in
that sample.
[Python's documentation](https://docs.python.org/3.14/library/sys_path_init.html)
describes the changed virtual-environment behavior in 3.14. The POSIX wrapper
was atomically installed on the active Mac with a private rollback copy and
read-only CLI checks. Existing processes were not restarted.

The Windows source resolves the `whoami`, `icacls`, and `netstat` executables
under the directory returned by [GetSystemDirectoryW](https://learn.microsoft.com/en-us/windows/win32/api/sysinfoapi/nf-sysinfoapi-getsystemdirectoryw),
instead of passing bare command names to `CreateProcess`. This narrows exposure
to executable search from a workspace or PATH; it is not an audit of other
launch paths or a boundary against a compromised system directory. Python's
[subprocess documentation](https://docs.python.org/3/library/subprocess.html#popen-constructor)
recommends an explicit executable path on Windows. On a Windows CI runner,
three disposable executables in the working directory were selected by the
bare-name controls, while the system-path implementation completed synthetic
identity, private ACL, and local-listener checks. Real Windows account and
login environments still need separate verification.

New frozen native-capability bundles include the shared helper module. Older
bundles remain eligible when their integrity-checked launcher has no direct
import of that module; a legacy-shaped bundle with the direct dependency is
rejected. This targeted check is not a complete analysis of dynamic imports or
future dependency changes. The updated capability controller was installed
atomically on the active Mac with a private rollback; the existing frozen code
was verified read-only and was not rewritten. Copies of the installed source
also passed a synthetic frozen-server stdio MCP/PDF check in a temporary root.

The macOS login policy stores only selected proxy schemes and bypass rules in
the private runtime directory; proxy addresses and credentials are read from
system settings in memory. When the saved policy expects a system proxy that is
no longer configured, login recovery fails instead of silently connecting
directly. A loopback proxy must accept a TCP connection before the origin starts;
no credentials or inference request are sent in that check. Automatic proxy
scripts and system bypass rules that cannot be mapped
without changing their scope are not evaluated. The local health check verifies
metadata routes, not upstream Internet connectivity or model inference.

The read-only diagnostic report never opens key files or contacts an endpoint. It emits only allowlisted counters, timing, hashed tags and compaction metadata. Origin and translator observations may describe the same request and are not additive. It does not export raw transcripts, error bodies or project paths.

The pinned upstream source starts an Antigravity client-version updater in the
server's normal startup paths, including without an Antigravity inference
request. It immediately requests a public version manifest and repeats every
three hours. The request is constructed without a body or explicitly assigned
Codex token, prompt, `Authorization` or `Cookie` header; it uses Go's default
transport and can be affected by proxies, redirects, and network configuration.
A recipient or intermediary can observe connection metadata. These source
properties do not prove what a particular failed log record transmitted or
whether a connection reached its destination. See the exact pinned
[updater](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/internal/misc/antigravity_version.go)
and [startup calls](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/cmd/server/main.go).
The running binaries have not been changed by this investigation. Restricting
unused-provider traffic must be tested across credential addition, real
Antigravity version discovery and running-session migration before activation;
disabling the separate management-panel updater does not disable this updater.

Separately, the pinned proxy's default general, Codex and Devin model catalog
sources include files under the proxy project's GitHub organization and a
project-operated fallback host. Outside local-catalog mode, the source
configures these refreshers immediately and every three hours; explicit sources
and Home-mode rules can change this behavior. The default catalog GET is built
without a body or explicitly assigned authorization header, but default proxy
and redirect behavior still applies and recipients can observe connection
metadata. Catalog contents are validated structurally, not authenticated as
OpenAI account entitlements. The active origin was observed without the
`--local-model` flag and without explicit catalog-source keys; the observation
does not show whether a particular request reached a remote host. The managed
translator has a separate local-catalog configuration. Do not disable Codex
catalog refresh indiscriminately: model freshness and account-specific discovery
must be preserved or replaced with a separately verified source.

Review updates to the pinned proxy release and its security advisories before changing the version or hashes. Do not enable LAN binding, request body logging, or the management interface without a separate security review.

The separate Claude HUD plugin also needs its own security review. The former
local 0.8.0 installation was within the affected ranges of upstream advisories on
[repository Git configuration](https://github.com/jarrodwatts/claude-hud/security/advisories/GHSA-fq7j-8hxj-jfqh),
[partial-clone fetches](https://github.com/jarrodwatts/claude-hud/security/advisories/GHSA-25hg-g83m-4w35),
and [terminal escapes](https://github.com/jarrodwatts/claude-hud/security/advisories/GHSA-q436-5523-5x82).
Git-status collection was temporarily disabled in the local HUD configuration
using the upstream workaround; the former configuration was backed up privately.
This setting alone does not address the terminal-escape issue or prove prior
compromise. The official plugin manager then updated the local package to
0.15.0, which upstream lists as patched for those advisories; the 0.8.0 cache
and a private backup remain available. No Claude process or bridge service was
restarted.

Upstream 0.15.0 was downloaded at commit `b86e1c3e5255880578de4b7cc87dc383228c2a18`
and each archive file was matched against that Git tree. A controlled invocation
of its own owner security fixture failed on this Mac. The separate
[fail-closed experiment](upstream/patches/claude-hud-v0.15-git-safety.patch)
passed the fixture and a synthetic config-enumeration failure check locally;
it is now installed locally, but is not an upstream fix or a complete audit.
The first [platform run](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41451134573)
passed on macOS and Linux but failed one Git-filter fixture on Windows. Its
first classified failure was the fixture's `git add` positive control, not a
reported command execution by the candidate. A Windows-only test experiment
normalizes the synthetic marker path to forward slashes while retaining the
same assertions; the later three-platform run passed. A passing Unix run alone
would not validate Windows.
The [fixture runner](tests/probe_hud_security.py) runs reviewed plugin code with
a temporary home and filtered environment, not an OS sandbox. The user's
existing HUD command selects plugin versions dynamically, so changing the cache
can affect running sessions at their next status refresh.

A separate [version-gated Git opt-in](upstream/patches/claude-hud-v0.15-guarded-git-optin.patch)
keeps the ordinary HUD Git setting disabled for the old version tested and for
versions that ignore the bridge-specific setting. It enables Git only when a
private opt-in and the reviewed collector's per-process guard are both present.
Its [synthetic controls](tests/probe_hud_guarded_git.py) check the opt-out, a
missing collector guard or launcher relay even with an injected environment
variable, the native `hud preview`/`set`/undo paths, and a fake repository command with an
ordinary-Git positive control. The [three-platform gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41454320316)
passed, including the Windows-specific fixture path adjustment. The local
0.15.0 plugin now has the reviewed collector and guard patches, and its private
`claudeCodexGuardedGit` opt-in is enabled while `gitStatus.enabled` remains
false. A synthetic frame using a copy of the actual HUD config displayed the
Git branch; no live user project was used in that test. This is not proof
against all repository configuration, tampering or future update behavior.
Use the patched HUD's normal `set gitStatus.enabled=false` command to turn off
the guarded display; `set ...=true` and `set --undo` are covered by the fixture.

An existing Bun-based HUD launch could not find Node in one running Claude
process's inherited `PATH`. A separate, locally installed
[launcher compatibility patch](upstream/patches/claude-hud-v0.15-bun-node-fallback.patch)
searches absolute PATH entries and standard system locations without modifying
the parent process. The original upstream launcher was backed up privately.
The installed 0.15.0 package is therefore locally modified, even though its
plugin-manager version remains 0.15.0. A synthetic run using that process's
PATH and a temporary home rendered the HUD without restarting the process.
The compatibility patch and the installed Git-safety patches are separately
reviewable. An official update can overwrite these local patches; verify the
new version before restoring Git. Older versions must keep the ordinary Git
setting disabled.

Upstream 0.15.0 writes its most recent status-line input to a local heartbeat
file for its diagnostic commands, with private POSIX file mode in the tested
fixture. The input can contain session metadata and paths; it is not included in
this repository. Temporary-home tests are not a complete plugin privacy audit.

The local HUD also has a separately reviewed cache-counter display patch. When
the bridge preference is enabled, validated main-session cache counters are sent
to the configured status-line command and may be retained in the HUD heartbeat.
They are usage metadata, not credentials. The preference file is private, and a
new installation defaults to disabled; the current local installation was
explicitly enabled after synthetic checks. Plugin updates can replace the patch,
and the feature is not a claim about every plugin's data handling.

The separate opt-in [one-shot cache-field observation](CACHE_AUDIT.md#one-shot-local-field-observation)
records only fixed Boolean field-shape categories for the already tracked local
session. Its private five-minute control copies the existing short session tag
and includes a random nonce; its private result contains no raw session ID,
prompt, transcript, path, model, token count or cache ratio. It performs no
network request. The two fixed files remain until stopped or replaced, and a
stop cannot retract an already in-flight callback. The filter is not a process
identity or protection against other processes running as the same user.
It was installed locally without restarting sessions; a one-shot observation
was subsequently stopped and its two diagnostic files removed. A real adapter
field-shape observation does not prove what a downstream plugin displayed.
An additional quota-presence scope is off by default and requires the explicit
`--include-quota` option. It retains only fixed Boolean categories, no quota
values, account identifiers, reset times or paths. It observes the adapter's
existing snapshot read and does not itself issue a network request. A stale or
unavailable snapshot is not evidence that a provider does not offer a window.

The experimental [task-stop advisory](taskstop_advisory.py) is not installed or
enabled. It accepts bounded hook input and returns only fixed context for a
specific error shape; it does not echo task IDs, paths, error text or other hook
fields. It does not send messages, access the network or stop processes. Hook
input can nevertheless contain session metadata and must be treated as private.

An isolated locked-client test found that a second `--settings` argument
replaced the bridge-generated launch settings. When that second file changed
only the destination, Claude sent the synthetic inherited local key to the
second synthetic endpoint. No real key or external endpoint was used. A
temporary guard was installed locally after [three-platform unit-test steps](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41463928859),
without restarting the origin, ingress or clients.

The current source composes custom settings into one launch file. It snapshots
bounded user JSON into a private per-launch file,
combines hook event lists and per-model entries, and refuses custom authentication,
provider, proxy, TLS and conflicting compact-window controls. It puts the local
key into the composed settings rather than the child process's initial environment,
so a rejected launch file cannot redirect that inherited key via user settings.
The [three-platform isolated gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41466134016)
used the locked Claude client and fake endpoints to cover normal and `--bare`
startup, hooks, a denied tool, malformed settings and routing. This is
not proof for arbitrary plugins, all settings, future clients, or protection from
other processes running as the same user. The locally installed launcher was
updated from commit `ce66452` after the gate and retested with fake endpoints;
the prior launcher was retained in a private backup. Editing the original per-launch file after startup is not
propagated by this snapshot; existing user/project settings remain Claude's own.
An explicit custom `modelPicker` replaces the generated picker, as in Claude's
settings precedence, so its author is responsible for keeping that list current.
The private `runtime/ui-*` snapshot can contain custom values and remains on
disk after a client exits. The explicit bridge-stop cleanup removes its known
settings files when service state is present; it is not a per-session retention
guarantee, and an already-missing service state needs separate manual cleanup.

An isolated locked-client test exposed a separate status-line scope issue: the
older launcher copied a synthetic user status line into its `--settings` file,
so it ran under `--restricted` and with `--setting-sources project`; direct
Claude controls did not run it. No actual user command was used. The updated
source avoids copying the user status line when restricted, bare or safe mode
is selected by a CLI flag or inherited environment variable, or when user
settings are excluded; an explicitly supplied
launch status line remains eligible. The [three-platform gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41469556864)
and an installed-code isolated test passed, and the controller-only local update
did not restart the origin, ingress or running clients. A separate synthetic
client test found that the older fallback also overrode project/local status
lines. At startup the current source reads bounded settings files into memory to
check whether they define a status line, and leaves detected or uncertain
project/local precedence to Claude. It does not persist, print or upload those
file contents. In that case the project status line is not adapted,
so its context, cost, quota and cache displays are not corrected by this bridge.
The [three-platform gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41472944905)
covered policy on Windows and locked-client project, local and linked-worktree
cases on macOS/Linux. The local launcher was updated from commit `2322d92`,
which changed only the warning text after that gate, without restarting the
origin, ingress or running clients. Windows checks and installed-code fixtures
covered the final source, including restricted-mode `true`; a private backup of
the previous launcher was retained. File changes after the check,
`/cd`, unusual Git layouts, managed policies and real plugins still require
investigation.

Browser MCP is opt-in. When used, page text and screenshots returned by the tool enter the Claude Code conversation and are sent to the Codex model endpoint. The recommended isolated browser starts without everyday Chrome cookies. Attaching to a signed-in Chrome profile gives the agent access to that profile's tabs and storage, so it requires an explicit per-session choice and normal tool permissions.

The bridge's WebFetch PDF hook processes the tool result locally and replaces only the binary-PDF summary with a warning and the path Claude Code already returned. It does not upload or copy the PDF. Claude Code's own WebFetch may leave downloaded binaries in its session `tool-results` directory; the hook does not remove them. Review that directory according to your local retention policy. [Upstream retention report](https://github.com/anthropics/claude-code/issues/104178).

If a credential is accidentally committed, revoke or rotate it first, then remove it from the repository and its full Git history before any further push. Deleting only the latest file does not remove it from earlier commits.

The optional [native capability pack](NATIVE_TOOLS.md) pins dependency hashes and immutable source snapshots. Native media calls use only the private TLS endpoint key, never OAuth. Workspace inputs reject symlink/reparse paths, absolute paths and traversal; artifacts stay private. No microphone recording or automatic playback is implemented. Desktop requires an explicit per-workspace window selection and existing OS permissions. The child receives no model credentials or global-input opt-in. Windows dispatches each action directly to one native worker, avoiding upstream uncertain-action retries, and disables screen-region GDI. These are application guardrails, not an OS sandbox against same-user programs or the selected app itself.

The desktop adapter installs a content-redacting filter on the Python MCP SDK's
`mcp.client.stdio` logger before starting its client. In an isolated test with
the locked SDK, a deliberately malformed synthetic message appeared in that
logger's unprotected parser exception, while the filter retained a generic
error without the synthetic content or exception text. This protects that
specific logger in newly started processes using an updated capability source
snapshot; it does not sanitize the child program's own stderr, other loggers,
or already running processes. See [MCP naming and SDK assessment](MCP_NAMING.md).
After the [three-platform source gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41408466871)
passed on 2026-11-02, the local installation selected a new immutable snapshot
containing only this source change. The previous snapshot and enablement choice
were retained, and the origin, managed runtime, and running processes were not
restarted. Existing native MCP processes continue using their previous code.

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
