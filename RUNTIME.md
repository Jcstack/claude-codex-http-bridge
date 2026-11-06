# Reviewed runtime rollout

The launcher can keep a fixed, authenticated TLS endpoint for new Claude Code
sessions while replacing translator processes behind it. Existing sessions on
the original direct service keep their listener and tasks. The original service
also supplies the authenticated Codex model route; neither OAuth nor an OpenAI
project key is copied into the new translator.

```text
Claude Code -> stable TLS ingress -> reviewed translator
                         ^                  |
                         |-- TLS origin guard
                                  |
                          existing Codex HTTP service -> OpenAI
```

The extra localhost hops are a migration arrangement. They do not start another
agent or change which application owns permissions, tools and conversation.
Only `/v1/messages` and `/v1/messages/count_tokens` use the compatibility
translator. Native Responses, model discovery, image/media and other `/v1/`
routes pass through the guarded origin with their bytes intact. This preserves
the origin's API surface; it does not establish model/account entitlement to
every hosted tool. Native requests participate in the active-request stop guard.
Retiring the original service requires its dependent sessions to finish. Moving
an already-running client from its old endpoint is not implemented by editing
its environment or settings files.

New managed ingress and translator processes, including TLS initialization,
receive a small system-environment allowlist rather than inheriting every shell
variable. Unrelated API keys, tokens and proxy credentials are not passed to
these loopback-only processes; explicit system CA-file and CA-directory paths
are retained. This does not sandbox them from files accessible to the user, and
does not change the environment of Claude Code, the original origin or any
already-running process.

On 2026-11-02, after the [three-platform candidate gate](https://github.com/Jcstack/harnesslift/actions/runs/41352045323)
passed, the installed helper was updated and the managed translator was
replaced through the rolling reload. The selected account, original service,
TLS ingress and selected binary bundle retained their identities; one old
stream was still draining when the reload returned. The new translator has the
restricted environment. After the old stream ended, its idle generation was
retired under the service lock; the current generation stayed unchanged. The
existing TLS ingress and original service were not restarted, so their earlier
process environments are unaffected. Historical
sessions connected directly to the original service also remain on its older
translator, outside the new incomplete-tool guard.

Later on 2026-11-02, the [credential-free three-platform candidate gate](https://github.com/Jcstack/harnesslift/actions/runs/41425623062)
passed for the effort-rejection guard and mid-conversation effort mapping. Its
source-locked macOS ARM64 bundle was verified and selected using `bridge upgrade`.
The original account, origin process, managed TLS port, and ingress were not
replaced; the selected backend executable and both local model-list routes were
checked afterward. The status snapshots immediately before and after the change
showed no active managed request; they do not prove the state at every instant.
No model inference was run as part of this activation. Legacy sessions connected
directly to the original endpoint do not receive these managed-runtime patches.
The separate experimental system-message fidelity patch was not selected.

On 2026-11-07 the [Claude error compatibility patch](upstream/patches/claude-error-compat.patch)
was selected for the managed translator after its
[three-platform native-client recovery gate](https://github.com/Jcstack/harnesslift/actions/runs/42268092997)
passed. It restores native error categories for two foreign server-error
names, allowing the tested Claude client to make its own retry decision. No
gateway replay loop was enabled. An installed-source overlay, 68 checks and
persistent-client handoff passed locally before upgrade. Original state/process,
ingress process/port, client key and account were preserved. A short synthetic
subscription control through the installed endpoint completed as nonstream
JSON with the expected marker, `end_turn` and a completion observation. New
managed requests receive the patch; already admitted requests and direct legacy
sessions retain their original translator. The physical upstream failure and
broader unattended-recovery limits remain open; see [STREAMING.md](STREAMING.md#native-claude-error-compatibility-2026-11-07).

An isolated Claude Code 2.1.316 probe found a possible future migration path:
when its user settings file changed, an ordinary long-running client completed
an already-started fake HTTP stream, then used a new authenticated fake HTTPS
gateway on the next turn with its earlier conversation intact. Adding the
temporary CA in the changed settings worked in this native-client sample; a
negative case without that CA did not reach the HTTPS server. A higher-priority
`--settings` environment kept the original route, changing the `--settings`
file itself did not change it, and `--restricted` ignored user settings. The
probe uses only synthetic credentials, a disposable CA and local endpoints;
it does not establish cross-platform or live-session migration by itself.

In particular, user settings are shared by multiple sessions, `ConfigChange`
hooks may block an edit, and removing an environment key does not clear it from
a running client. A safe implementation still needs per-session scope, an
authenticated and verified target, atomic update and rollback, in-flight and
background-task checks, and validation against the actual settings precedence.
No live user's settings were changed to run this probe. [Claude settings and
reload](https://code.claude.com/docs/en/settings#when-edits-take-effect),
[environment updates](https://code.claude.com/docs/en/env-vars#set-variables-in-settings-files),
and [configuration hooks](https://code.claude.com/docs/en/hooks#configchange).

Claude's built-in [gateway model discovery](https://code.claude.com/docs/en/llm-gateway-protocol#model-discovery)
is also a startup snapshot: it filters model IDs without `claude` or
`anthropic`, and a replacing `modelPicker` hides discovered rows. It therefore
cannot on its own keep existing GPT model pickers current. This launcher builds
its picker from the selected account's discovered catalog at session launch;
updating existing sessions safely remains open.

A separate [POSIX interactive fixture](tests/probe_picker_reload.py) reopens
`/model` in the same Claude Code 2.1.316 process after adding a synthetic model.
The new row appears when the picker comes from the disposable user settings,
but not when its source is the changed `--settings` file used at startup. This
suggests a future per-session settings design, but a shared user settings file
would affect other sessions, including plain Claude. Preserving the user's
plugins, hooks, trust decisions, session history and private configuration in
an isolated per-session design remains unverified. Moreover, Claude documents
[`modelSettings` as startup-only](https://code.claude.com/docs/en/settings#when-edits-take-effect):
adding a picker row alone does not safely update that model's configured
compaction window. The fixture does not cover the Windows interactive console.

The pinned origin's [internal execution API](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/sdk/api/handlers/model_execution.go)
accepts a specific `AuthID` for both stream and non-stream calls. Its own
credential-free tests verify propagation in both modes and, for non-stream
execution, selection of the requested active credential without falling back
when an ID is missing or disabled. Those targeted tests also passed against a
clean copy of the pinned source. A separate synthetic manager test establishes
an old account's session affinity, then verifies that an explicit streaming
pin selects the other active account despite lower priority. Missing or
disabled pins and a synthetic rate limit with retries do not fall back to a
different account in that test. These are internal Go interfaces, not a
verified client-controlled HTTP route or an implemented account handoff.
Turning on two credentials in the existing origin could change selection for
direct sessions, and duplicating a rotating refresh token across processes is
unsafe. Any use of pinned dispatch still needs authenticated scope, account
and model-catalog consistency, single-owner refresh, streaming/cancellation,
crash recovery, cache isolation and a safe migration for the existing origin.

## Build and install

Use a trusted build from the candidate workflow, or reproduce its pinned source,
reviewed patches, `go mod verify`, unit/contract tests and Go build commands in
[candidate-proxy.yml](.github/workflows/candidate-proxy.yml). Its runtime bundle
contains two executables and a manifest, with platform, upstream revision,
patch/source hashes and binary hashes. It contains no runtime data. Hash checks
establish consistency with that manifest, not an independent publisher signature;
build/download provenance still matters.

To pack binaries from the reviewed build:

```text
python runtime_bundle.py --proxy /path/to/cli-proxy-api --ingress /path/to/bridge-ingress --output /private/empty-bundle-directory
claude-codex bridge upgrade /private/empty-bundle-directory
claude-codex bridge status
claude-codex --model sol
```

Install matching launcher source before activating a new source lock. On
Windows the bundle uses native `.exe` files; the packer accepts the actual build
paths. A mismatched platform, patch/source lock, binary checksum or path is
rejected before changing the chosen runtime. Installed bundles are immutable
directories identified by the manifest hash. They never overwrite a running
executable, which also supports Windows file locking.

A previously selected bundle remains usable when launcher source is updated:
its private selection must still match the entire immutable manifest and both
binary hashes. This does not admit an old or unknown bundle as a new upgrade.
New activation always requires the current reviewed source lock. Changing a
selected manifest, even to the new source hash, fails its recorded identity.

`claude-codex bridge rollback` selects the most recently recorded previous
installed bundle and uses the normal managed-backend handoff. The previous
bundle's private manifest identity, platform and binary hashes are verified
again; it may have a different source lock because it was selected before a
launcher update. The command does not install an arbitrary old bundle. The
current source lock remains in place for future upgrades. A second rollback
switches back to the version replaced by the first, provided the recorded
identity and files still validate. Reverting a runtime also reverts its fixes.

New distinct upgrades record the former selection before publishing a new
backend. First-time installs have no prior runtime; installations upgraded
before this history mechanism was added do not automatically acquire one.
History is private, contains only bundle identities, and is bound to the
current selection. A missing, inconsistent or modified history or bundle
fails closed. A handled prepublication failure restores the selection and
history without overwriting a concurrent different writer. An abrupt process
or computer failure between file updates can leave a mismatch requiring
inspection; this is not a crash-atomic multi-file transaction.

A credential-free [rollback probe](tests/probe_runtime_rollback.py) uses real
gateway/translator binaries, a fake origin, and the same isolated Claude Code
process across a rollback. It checks an old in-flight stream, new requests,
stable TLS identity, a changed source lock, and a synthetic retirement failure
after publication. It does not establish every plugin's behavior, real model
acceptance, or real provider cache reuse after a rollback.

Once installed, ordinary new `claude-codex` launches use the managed endpoint.
The model catalog and HUD still come from the authenticated subscription route.
No OpenAI API project route is enabled. `claude-codex bridge reload` starts and
checks a replacement, atomically changes new-request routing, and stops retired
backends only after their admitted request count reaches zero. A request is never
replayed after partial output. Replacement failure restores the previous
manifest and state. Pending drain work can be completed by another reload.
On a handled `bridge upgrade` failure, the runtime selection is also restored
if it still names the candidate; a concurrent different selection is never
overwritten. A process crash or power loss during selection and publication is
not covered by this in-process rollback.

`bridge stop` refuses while managed requests are active. As with the original
service, wait for background tasks before stopping; a task can be between model
requests even when the instantaneous request count is zero.

The reviewed stack also includes [native V2 compaction](NATIVE_COMPACTION.md).
Enable it per launch with `--native-compaction`; older selected bundles remain
usable but cannot accept that option without the reviewed patch. Replay uses
the stable private origin-guard key, preserving signed state across translator
replacement without copying rotating OAuth credentials.

An experimental candidate adds a reviewed upstream gateway MCP-name backport
and an instance-prefix follow-up, described in [the community
review](COMMUNITY_REVIEW.md#follow-up-review-v8051-and-gateway-tool-names-2026-11-02).
These patches change some names seen by the model and can invalidate a cached
prefix in an existing conversation. A synthetic old-to-new tool loop preserves
the selected tool and result; it does not prove every live plugin or transcript
can be switched without impact. Those optional patches are not in the packaged
safety candidate. Building a candidate does not replace the running translator,
origin or ingress.

## Native URL citations

The [URL annotation patch](upstream/patches/url-citations.patch) maps OpenAI
`url_citation` annotations into Claude text citations, including incremental
`citations_delta` events. It preserves the answer text, URL, title and native
offsets. Repeated annotation/part/item/terminal events are deduplicated; a
non-streaming response and a real Claude resume are also covered by the fixture.

`encrypted_index` carries a namespaced bridge continuation envelope, not an
Anthropic encrypted index. It includes the native annotation and an already
emitted text-prefix hash. The request translator restores annotations only in
assistant text with matching prefix, URL and title; edited or merged text does
not receive stale offsets. This hash checks consistency, not authenticity.
Envelopes are bounded to 64 KiB per annotation. The citation-count bound is
1,024 per streaming response, or per text part in a non-streaming response.

OpenAI's [URL annotation](https://developers.openai.com/api/docs/guides/tools-web-search#output-and-citations)
locates a citation in generated output; it does not supply a quoted source
excerpt. The [Claude citation format](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool#citations)
also has `cited_text`, which this bridge leaves empty. Link display and replay
must not be described as verified quotation accuracy. File/container citations,
commentary annotations and complete native citation UI equivalence remain open.

## Local identity and credentials

The ingress binds only `127.0.0.1`. It generates a private local CA and a server
certificate limited to loopback. Claude trusts this CA through the documented
[`NODE_EXTRA_CA_CERTS` setting](https://code.claude.com/docs/en/network-config#custom-ca-certificates),
preserving any existing extra CA file. The CA is not added to the operating
system trust store. A fixed TLS identity prevents a different local listener
from receiving a client's bearer key after reclaiming the port. Leaf renewal
keeps the CA and running endpoint; the server retains the last valid key pair
while two certificate files are being replaced.

The translator trusts this CA only for one exact HTTPS loopback authority and
does not follow its redirects. Public-provider TLS roots and certificate
verification remain enabled. The origin guard uses a separate local key and
checks the legacy listener PID after connecting, before sending the legacy key.
Frontend credentials cannot use the guard route, and neither route exposes the
upstream management API.

The selected runtime also contains an ingress that checks the original
process's creation time and executable identity before each new origin
connection receives any HTTP bytes. The controller writes a private,
generation-scoped identity record before publishing the unchanged origin
manifest, so an old ingress can continue to use that manifest. A macOS build
without cgo refuses the identity-capable configuration instead of silently
skipping the check. Existing ingresses keep their original behavior until the
ingress process itself is replaced; updating the controller or translator
alone does not enable this protection. The installed Mac is still running its
earlier ingress, so that ingress does not yet perform the creation-identity
check. The selected runtime passed a [three-platform candidate gate](https://github.com/Jcstack/harnesslift/actions/runs/41445355599)
with isolated synthetic recovery and stream tests, not an actual machine reboot
or PID reuse.
Process and listener checks narrow risk but are not a proof against every
same-user race or socket inheritance scenario.

Runtime keys, certificates, configs and logs stay under the private installation
root, with POSIX private modes or current-user NTFS ACLs. Per-launch settings
carry the gateway and CA configuration so detached sessions can load them;
the user's global settings are not rewritten. The separate compatibility
translator has no OAuth files. Its retries are disabled because the existing
Codex service handles upstream retries. Its transient-error cooldown is disabled
so a recovered localhost fault does not create another minute of 503 responses;
quota/429 behavior is retained.

Proxy processes start in their own private runtime directories so they do not
automatically load a user's project `.env`. Claude Code still starts in the
requested project. Background fixtures use a separate `CLAUDE_CONFIG_DIR`
[supervisor instance](https://code.claude.com/docs/en/agent-view#where-state-is-stored).
The warm-supervisor test records a real background model reply after starting
that supervisor with a deliberately wrong gateway, then dispatching with the
private per-session profile. It tests routing/reply delivery, not the mock
model's task-state classification.

## Acceptance scope

The credential-free full-path probe verifies stable TLS routing, old-stream
drain, startup rollback, active-stop refusal, PID rejection before credentials,
immediate recovery after a restored origin, and two turns in the same real
Claude process separated by a backend reload. It uses a disposable fake model
and configuration. Live subscription checks and native Windows execution are
reported separately in [OPERATIONS.md](OPERATIONS.md).

This arrangement does not by itself repair upstream TLS failures, make every
cache lookup hit, change an existing client's startup compaction setting, or
certify every Claude plugin. Those require their own measured checks.
