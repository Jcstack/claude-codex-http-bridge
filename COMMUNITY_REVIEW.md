# Community proxy review and upgrade gate

Reviewed 2026-10-30; follow-ups through 2026-11-02 appear below. This project keeps Claude Code as the harness. The current authenticated localhost origin runs the reviewed CLIProxyAPI v8.0.46 release. No candidate below was given an OAuth credential in this review, and the live service was not replaced.

Follow-up native Live review, 2026-10-31: the source-matched V3 call now returns
201 locally, while media and sideband remain incomplete. OpenAI's
[201/404 issue](https://github.com/openai/codex/issues/35094) and
[service-error issue](https://github.com/openai/codex/issues/55531) distinguish
signaling, media, account checks and sideband failure. Neither report establishes
this machine's cause. The small MIT-licensed
[codex-backend-sdk](https://github.com/B4PT0R/codex-backend-sdk/blob/main/docs/backend-api.md)
documents useful native protocol observations and a silent-track data-channel
check. At review it was unarchived, last pushed Oct 22, with 10 stars and no
open issues. That is a useful implementation reference, not enough evidence for
a production dependency or universal account support. It was not installed,
given credentials, or allowed to own refresh tokens. Our bounded transport
fixtures use synthetic state; no third-party service receives credentials.

## Protocol finding

Route clarification: this installation uses the Codex backend with `use_siwc=false`. The public SIWC limitations below apply to `api.openai.com/v1`; they alone do not prove native-tool availability or exclusion on the Codex backend. [Operational correction and tests](OPERATIONS.md).

Claude Code can add a tool in a `role: "system"` message using `tool_addition`, or return `tool_reference` blocks after ToolSearch. The Claude API renders these at that position without rewriting the cached tool prefix. [Anthropic's mid-conversation tool guide](https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages), [tool reference and caching](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-reference).

The original v8.0.46 proxy strips `defer_loading` but forwards every deferred schema at request start. It also drops inline `tool_addition` and `tool_removal` changes. `tests/probe_proxy_features.py` sends four synthetic Anthropic Messages requests through a candidate binary to a loopback fake OpenAI Responses upstream. On the current pin it reports `false` for all four gates: inline tool addition, initial deferred-tool hiding, post-search reveal with a stable top-level tool list, and removal that constrains callable tools without rewriting that list. The probe contains no real credential or user conversation. Run it without `--report-only` to require all gates before promoting a candidate binary; CI uses `--report-only` to record the known gaps on the current release.

OpenAI's [API Tool Search guide](https://developers.openai.com/api/docs/guides/tools-tool-search) describes native `tool_search` with deferred tools. The separate [ChatGPT-plan preview limits](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) explicitly exclude Responses `tool_search` on that route, while allowing function/custom tools and `additional_tools` items. The tested compatibility design uses **client ToolSearch as a normal Claude Code tool**, then a cache-preserving `additional_tools` input item when the client returns a trusted `tool_reference`. A direct API-key route can be evaluated separately for native `tool_search` after the user enables it and live verification is authorized.

## Candidate evidence

| Project | Maintenance and capability evidence | Decision for this installation |
| --- | --- | --- |
| [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) | The latest inspected release is [v8.0.51](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v8.0.51), published Nov 1 at 17:08 UTC. [Issue #7112](https://github.com/router-for-me/CLIProxyAPI/issues/7112) reproduces the missing `tool_addition`; [commit `12d97cf`](https://github.com/router-for-me/CLIProxyAPI/commit/12d97cfe196be266b45f4f7e0cc394c9fdc2e099) implements it. A credential-free build of v8.0.51 passes the inline-addition gate but still fails the three deferred-tool and removal gates described below. [Issue #1894](https://github.com/router-for-me/CLIProxyAPI/issues/1894) identifies the separate deferred-schema gap. | Keep the verified patched translator and authenticated origin. Newer authentication and translation changes need full compatibility, privacy and cross-platform checks before an origin migration. |
| [pleaseai/shunt](https://github.com/pleaseai/shunt) | [v0.61.0](https://github.com/pleaseai/shunt/releases/tag/v0.61.0) was released Oct 30. Its [ToolSearch implementation issue #82](https://github.com/pleaseai/shunt/issues/82) and [progressive-reveal issue #43](https://github.com/pleaseai/shunt/issues/43) include protocol cases and tests. The v0.61.0 release assets are macOS/Linux only; the tagged source has no `tool_addition` translation. Its native subscription-route `tool_search` assumption needs a live acceptance check against the official preview limit above. Its default loopback mode has no inbound client authentication unless configured. | Strong implementation reference, especially for schema order and streaming. No credential migration or backend switch while Windows, inline tools and route behavior remain unverified. |
| [raine/claude-code-proxy](https://github.com/raine/claude-code-proxy) | [v0.1.47](https://github.com/raine/claude-code-proxy/releases/tag/v0.1.47) was published Oct 25. Open [issue #182](https://github.com/raine/claude-code-proxy/issues/182) reports `tool_reference` rendering as an unsupported block and cache-prefix rewrites; open [issue #163](https://github.com/raine/claude-code-proxy/issues/163) reports Codex compaction-state loss. | Focused alternative, but its open tool-search and continuation issues prevent a safe replacement today. |
| [Claude Code Router](https://github.com/musistudio/claude-code-router) | [v3.1.3](https://github.com/musistudio/claude-code-router/releases/tag/v3.1.3) is broad, but open [issue #1643](https://github.com/musistudio/claude-code-router/issues/1643) reproduces a lost Responses `tool_result` on the next turn. | Useful reference for provider breadth; a successful text request does not establish tool-loop correctness. |
| [jclab-joseph/claude-code-model-proxy](https://github.com/jclab-joseph/claude-code-model-proxy) | Documents a concrete multi-turn E2E matrix and experimental Claude bootstrap rewriting. The latter changes an undocumented startup response and is explicitly off by default in that project. | Borrow its acceptance scenarios, not its undocumented bootstrap interception or automatic access to another app's OAuth file. |
| [DeanDiasti/clodex](https://github.com/DeanDiasti/clodex) | New Rust bridge with built-in Codex backend, live model picker, same-machine session leases, and optional Claude-subscription routing. Its [documented release](https://github.com/DeanDiasti/clodex#quick-start) is macOS/Linux only. At reviewed commit [`3cdea4b`](https://github.com/DeanDiasti/clodex/commit/3cdea4b3d0af007672eb2407462d82c9e46df407), the [Codex request translator keeps only text in mid-conversation system messages](https://github.com/DeanDiasti/clodex/blob/3cdea4b3d0af007672eb2407462d82c9e46df407/crates/codex-backend/src/providers/codex/translate/request.rs#L918-L935), so it does not implement Claude's `tool_addition`/`tool_removal` blocks. Its HTTPS proxy approach to Claude-account features is complex and needs separate security and compatibility review. | Useful architecture reference for model routing, lifecycle, and credentials, but no demonstrated Opus-quality uplift or full dynamic-tool parity; do not replace the Windows-capable active bridge. |
| [Aotricx/Clodex](https://github.com/Aotricx/Clodex) | New single-backend Go translator with native Windows release assets. Its [own security section](https://github.com/Aotricx/Clodex#security) states that the loopback proxy authenticates no caller, so any local process can spend the subscription while it runs. At reviewed commit [`9fe9013`](https://github.com/Aotricx/Clodex/commit/9fe9013206fda6d9b91c941bb46e12e240132556), its [system-block translator warns and skips non-text blocks](https://github.com/Aotricx/Clodex/blob/9fe9013206fda6d9b91c941bb46e12e240132556/internal/translate/request.go#L206-L217); its tool mapper also does not recognize `defer_loading`. | No credential migration or service switch; inbound authentication and dynamic tools fail this project's gates. |

Stars and release frequency are screening signals. The decision above rests on source paths, reproducible issues, credential boundaries, Windows distribution and the explicit protocol gates. No alternative candidate reviewed here was given this user's credentials; the active pinned CLIProxyAPI remains the only authenticated proxy.

An Oct 31 follow-up still finds CLIProxyAPI's latest release at v8.0.48.
Raine's [state-loss issue #163](https://github.com/raine/claude-code-proxy/issues/163)
and [persistence proposal #164](https://github.com/raine/claude-code-proxy/pull/164)
remain open. Their author explicitly identifies them as an autonomous-agent
proposal pending maintainer review. The report names TTL/restart loss, a second
compaction boundary and parent/subagent identity collisions; the proposal also
retains readable backup summaries. These are useful acceptance scenarios, not
proof of a merged production fix. Our [opt-in V2 path](NATIVE_COMPACTION.md)
tests repeated boundaries, process/model replacement and unrelated requests
using self-contained authenticated state instead of a session-ID state table.
Its opaque summary has a different plugin/readability tradeoff and is not
presented as universal superiority or full model-quality parity.

None of these projects publishes a representative coding benchmark showing GPT served through Claude Code reaches **Opus 5.5 + Claude Code**. A feature checklist and a successful text/tool demo establish transport behavior, not model quality. See [the controlled harness and model comparisons](MODEL_HARNESS_EVIDENCE.md).

## Isolated implementation candidate

Deployment update: the reviewed runtime is now activated behind a separate stable TLS ingress for new local launches. The original authenticated v8.0.46 service remains as the guarded origin for it and the direct endpoint for old sessions. No OAuth credential was copied into the translator. [Runtime controller and three-platform/live evidence](RUNTIME.md), [operational results](OPERATIONS.md). The earlier candidate measurements below remain scoped to their stated commits.

The candidate starts from the **same v8.0.46 source commit** as the installed release, [`9717e32`](https://github.com/router-for-me/CLIProxyAPI/commit/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5). [The first patch](upstream/patches/12d97cf-inline-tools-backport.patch) backports only the Codex translator files from upstream's [`12d97cf` tool-addition change](https://github.com/router-for-me/CLIProxyAPI/commit/12d97cfe196be266b45f4f7e0cc394c9fdc2e099). [The second patch](upstream/patches/12d97cf-deferred-tools.patch) withholds `defer_loading` tools from top-level `tools`, then inserts their full sanitized schemas as an ordered `additional_tools` item after Claude Code's `tool_reference` result. It accepts references only when their result matches a prior `ToolSearch` call ID, and builds OpenAI function schemas from an allowlist so new Anthropic-only fields do not reach Responses. A later `tool_removal` applies a current-turn OpenAI `allowed_tools` restriction; prior tool declarations and reveals stay in the cached prefix. Forced calls to removed or unavailable tools fail closed with request-scoped HTTP 400 before any upstream call; they do not place the account in cooldown. Unknown removal references also fail closed. This covers function/tool references and mapped MCP function names, not every hosted-MCP-toolset lifecycle variant. Neither patch contains credentials.

The local isolated macOS build passed the Codex translator unit package, the fake OpenAI API function-call/stream contract, and all four `probe_proxy_features.py` gates. The [candidate workflow at commit `6b57b1b`](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41030566455) passed all four gates, translator unit tests and the streaming/tool contract on macOS, Linux and native Windows without OAuth or API keys. The removal gate also verifies that a forced removed tool returns HTTP 400 without contacting the upstream. A separate [live subscription probe](tests/live_candidate_subscription.py) sent synthetic ToolSearch history through the candidate to the **existing authenticated localhost service**: GPT-6.1 Sol accepted the `additional_tools` reveal, called the loaded fixture tool, continued after its result, and respected a later `tool_removal`. A further isolated **real Claude Code** run called `ToolSearch`, then a temporary MCP fixture tool, and completed from its result. The candidate received only the existing service's private localhost client key; no OAuth file or OpenAI project key was copied to it. A subsequent [isolated coding pilot](HARNESS_PILOT.md) using candidate commit `383f3f2` passed a synthetic SSE repair task twice in Claude Code; the later `6b57b1b` change adds request-scoped policy errors and a no-removal fast path, and was validated by the cross-platform contract run above. At that earlier pilot the active service and installer used the official v8.0.46 binary. The installer and authenticated origin retain that pin, while new local sessions now use the activated patched translator. A paired tool-loop comparison and later managed TLS handoffs passed (see OPERATIONS.md). Authenticated Windows inference and longer representative project evaluations remain unverified.

To repeat the live probe after reviewing a candidate binary, run `python3 tests/live_candidate_subscription.py /path/to/candidate --root /path/to/installed-bridge --claude-smoke`. It consumes a small amount of the existing subscription quota. The script prints only the model ID and pass/fail stages, then stops its temporary candidate process and removes the temporary local-key copy. Omit `--claude-smoke` to test the synthetic Anthropic Messages calls without launching Claude Code.

## Candidate acceptance sequence

The [third patch](upstream/patches/web-search-replay.patch) fixes native hosted-search history loss and duplicate-event SSE framing. It preserves a bounded native search item in the Claude transcript and consumes that envelope on replay. A real Claude client can resume it through a new proxy process. The fixture uses no credentials; a separate local subscription probe also succeeded. The later [URL annotation patch](upstream/patches/url-citations.patch) adds streaming/non-streaming text citations and annotation replay; exact source excerpts and full citation UI equivalence remain gaps. The [operational record](OPERATIONS.md) documents the two real stable-ingress handoffs, including a cache miss, and the remaining initial-migration constraint.

1. Freeze an upstream source commit and its dependency lock. Apply only reviewed translation changes in an isolated build; keep real OAuth files outside the build environment. This is implemented for the v8.0.46 candidate above.
2. Run the synthetic probe on macOS/Linux and native Windows. Require the four tool gates plus tool-result pairing, parallel calls, long MCP names, unknown references, duplicates, streaming/non-streaming output and interrupted turns before promotion.
3. Confirm `additional_tools` is placed after the relevant ToolSearch result or system tool addition, with the original top-level `tools` prefix unchanged. Reject a candidate that merely drops `defer_loading` or echoes schema text while changing the leading tool list.
4. Compare the candidate against the current pin on the **same** small, isolated subscription task and model, recording request model, tool calls, result pairing, cache tokens, latency and success. Never print tokens or prompt bodies in CI or public logs.
5. Review transport, inbound authentication, permissions, release checksums and Windows binary provenance. Only then change the installed service after active/background sessions finish.

## Follow-up review: v8.0.48, citations and incomplete streams

The [v8.0.46-to-v8.0.48 diff](https://github.com/router-for-me/CLIProxyAPI/compare/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5...v8.0.48) contains 32 commits, including structured usage, WebSocket logging retention, auth renewal/cooldown and compaction changes. Release activity alone is not a reason to replace an authenticated origin during dependent tasks. The reviewed inline-tool backport is already tested here. The other changes need isolated source/build/contract checks before promotion.

Raine's [current compatibility record](https://claude-code-proxy.raine.dev/reference/compatibility-and-limitations/) explicitly targets practical Claude Code usage rather than full protocol equivalence and still documents unauthenticated incoming clients. Its [web-search compatibility source](https://github.com/raine/claude-code-proxy/blob/main/src/providers/codex/translate/web_search_compat.rs) extracts response links; that is useful UI behavior, but it does not establish exact native annotation preservation. This project's new patch instead carries the native annotation, bounds its continuation envelope, and checks the unchanged text prefix before restoring offsets. It leaves Claude's quoted-source field empty because OpenAI does not supply that excerpt. A real Claude client preserved the annotation through resume in the isolated fixture.

[Issue #6469](https://github.com/router-for-me/CLIProxyAPI/issues/6469) reports both short and long incomplete streams and links [PR #6497](https://github.com/router-for-me/CLIProxyAPI/pull/6497) for metadata-only diagnostics. It supports distinguishing HTTP completion from SSE completion, not inferring a universal timeout or automatically replaying partially emitted actions. The local read-only diagnostics retain that distinction; the origin's observed TLS/EOF failures are still an open problem.

## Scoped desktop review, 2026-10-31

[Open Computer Use 1.3.0](https://github.com/opensymph/open-computer-use/releases/tag/v1.3.0) was reviewed at source `e551c13e7082ce4c097234137582fbf4a1bc647a`. The small star count did not drive adoption: source behavior, release hashes and open [shared-socket](https://github.com/opensymph/open-computer-use/issues/5) and [closed-window](https://github.com/opensymph/open-computer-use/issues/8) issues did. Our wrapper requires live native window/process identity, one-use observations and an exclusive bridge lease, bypasses the macOS shared app agent, and never exposes global/window-selection controls to the model. Windows source review found a three-attempt action-worker retry and an in-process fallback, so our adapter invokes the pinned native `op` worker directly exactly once. Window WGC/PrintWindow observation is allowed; screen-region GDI is not. An ordinary macOS fixture passed actual OS text/button changes, with no test-command bridge. Native Windows GUI CI is the next release gate. [Pack details](NATIVE_TOOLS.md).

[Munim Computer Use](https://github.com/munimtechnologies/munim-computer-use) was also reviewed for its allow/ask/block policy and recent maintenance. Its own documentation distinguishes policy from an OS sandbox. It was not installed or adopted solely on that feature list. Browser MCP remains the default for browser tasks.

The follow-up real-Claude macOS fixture exposed extra button effects from the
heuristic click path. The adapter now requests one exact AXPress, with no
descendant/hit-test fallback, and verifies actual one-button state. It also
removes screenshot data URLs from JSON context and maps resized display pixels
back to native pixels. The initial three-OS GUI gate passed at `788371c`;
subsequent Unicode/selection checks add stronger coverage. Upstream is a useful
backend, not an assertion of universally safe semantics.

CLIProxyAPI v8.0.48 was released on 2026-10-30 and its 32-commit delta from the
installed 8.0.46 pin was reviewed at release-note/file level. It includes the
inline tool-addition fix already backported here, usage and compaction-capsule
changes, but that alone does not establish a fix for the observed TLS/EOF.
[Metadata-only incomplete-SSE diagnostics #6497](https://github.com/router-for-me/CLIProxyAPI/pull/6497)
and [environment proxy handling #7103](https://github.com/router-for-me/CLIProxyAPI/pull/7103)
remain open in the reviewed snapshot. No unaudited pin bump or live origin
restart was performed. The next upstream migration needs the existing patch,
privacy, Windows and live handoff gates.

## Follow-up review: v8.0.51 and gateway tool names, 2026-11-02

The [v8.0.51 tag](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v8.0.51)
points to `7cad1cb7df48c6c5ffb7ff0b34ef8198806115bc`, 76 commits after the
installed upstream pin. The comparison is a targeted source and capability
review, not an audit of all changes. An unmodified, credential-free macOS build
passed upstream's Claude translator tests and our basic mock Responses contract.
Our dynamic-tool probe returned **true** for mid-conversation addition but
**false** for initial deferred hiding, reveal without rewriting the tool prefix,
and restricting a removed tool. The existing patched candidate passes those
gates. The independent patch applicability checks found that the reviewed TLS
patch applies cleanly to the new tag; the other patches did not apply standalone,
some because an earlier change is already upstream or because patches depend on
one another. This is not a complete port attempt.

Selected changes and their boundaries:

- [Incomplete-stream cooldown change](https://github.com/router-for-me/CLIProxyAPI/commit/f013eb66)
  marks an incomplete **OpenAI-compatible executor** stream as truncated so it
  does not cool down a credential. The current subscription route uses the Codex
  executor; this diff alone does not establish a fix for its observed EOFs or
  previous 429 incident.
- [Codex refresh scheduling change](https://github.com/router-for-me/CLIProxyAPI/commit/95f125ff)
  avoids auto-refresh attempts for local Codex credentials without renewal
  material, with explicit exceptions for other refresh owners. Its broader
  authentication lifecycle still needs review against our single-refresh-owner
  and account-switch protections before use.
- [Visible commentary](https://github.com/router-for-me/CLIProxyAPI/commit/cc4b9949)
  is off by default. The code mirrors public Codex commentary into a visible
  Claude text block and removes the adjacent copy on replay; the visible copy is
  emitted when that commentary block closes. It does not expose private
  reasoning or establish real-time audio/video, and its interaction with our
  tool, citation, cache and compaction patches has not been validated.
- [Gateway MCP name fix](https://github.com/router-for-me/CLIProxyAPI/commit/08437a6742a548b001a42569be2de6d150a2e94c),
  motivated by [issue #7155](https://github.com/router-for-me/CLIProxyAPI/issues/7155),
  retains a readable action when a gateway tool name exceeds the 64-character
  limit. The upstream rule can label same-named tools from distinct instances
  with an ordinal alone. Our [follow-up patch](upstream/patches/gateway-instance-identity.patch)
  also retains an instance-ID prefix and short stable digest when they fit; it
  uses a shorter prefix, then falls back to no instance ID if necessary. Unknown
  name formats keep upstream's fallback. A digest or ordinal is not proof of
  account identity, and especially long names can remain ambiguous.

An experimental candidate applies the upstream name patch followed by our
instance follow-up. The separately packaged safety candidate leaves those name
patches out, so applying the incomplete-tool guard does not itself rewrite MCP
tool names. Local Go integration tests check deferred tool reveal,
collisions, same-prefix instances, streamed and non-streamed reverse mapping,
replay, removal and the 64-character fallback. The old pin and upstream-only
fix each fail their respective new negative controls. A real Claude Code client
with two same-description synthetic MCP tools selected the intended instance,
called it once and replayed the result through a fake Responses origin. A second
fixture switched between old and new translators within that tool loop: the
tool result still arrived, but the serialized historical tool names changed,
so a cached prefix can change. These are synthetic local tests, not a guarantee
for arbitrary plugins or model decisions. A [credential-free three-platform
run](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41343960850)
passed for that experimental candidate. A live account handoff and broader
plugin cases remain separate gates; the installed running translator has not
been replaced by the name candidate.

The latest reviewed Shunt release remains v0.61.0. Its open [issue #880](https://github.com/pleaseai/shunt/issues/880)
reports that some admin tests can inspect real non-Codex account stores unless
those paths are isolated. [Issue #877](https://github.com/pleaseai/shunt/issues/877)
describes a plausible overlapping-login race and explicitly says it was not
observed in practice. These are project reports, not findings about this bridge.
They reinforce the decision not to run another proxy's account tests against a
real user profile or migrate refresh credentials during evaluation. Its
[session-affinity discussion](https://github.com/pleaseai/shunt/issues/873)
is useful for future design: account changes may affect continuation state and
cache, not merely which account pays for a request. It does not prove identical
behavior for this bridge's subscription route.

## Mid-conversation instruction fidelity, 2026-11-02

[CLIProxyAPI commit `f1ed891`](https://github.com/router-for-me/CLIProxyAPI/commit/f1ed8912bbed42e72499b4065c0eb93667123f98)
explicitly introduced message-level system reminders as user-visible text.
The [Magpie discussion](https://github.com/yetone/magpie/issues/2532) reports
a related authority-versus-cache tradeoff in the opposite translation
direction, and includes differing views about the impact. It does not prove
behavior of this bridge or its users' sessions. Our own synthetic test found
both the changed role and a separate expired-instruction mismatch. Official
[Anthropic](https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages)
and [OpenAI](https://developers.openai.com/api/reference/go/resources/responses)
specifications motivate the independent candidate and its negative controls;
the private Codex backend, client/plugin behavior and provider cache effects
still need verification before activation. See [the compatibility record](COMPATIBILITY.md).

## Process birth checks and cost, 2026-11-02

The managed ingress currently checks the listener's PID and port before
forwarding a private key. A reused PID and port can still represent a different
process; Python controller checks already include a creation identity. An
isolated [benchmark](tests/bench_process_identity.py) measured one disposable
listener on this macOS host: across 12 samples, the median listener-owner
lookup was 42.447 ms, an in-process Python/libproc identity check was 0.155 ms,
and starting a separate isolated Python identity verifier was 38.827 ms. A
different synthetic identity was rejected. These are three separate checks,
not a network latency or throughput benchmark, and the timings do not predict
other machines. The fixture passed no user credentials and contacted no model.

The same [benchmark run's individual platform jobs](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41441024856)
reported median in-process versus subprocess identity checks of 0.220 versus
55.164 ms on macOS, 0.214 versus 31.832 ms on Linux, and 0.212 versus 76.240 ms
on Windows, with 12 samples per platform. These are separate shared runners
and do not support cross-machine speed rankings.

A separate local [native identity experiment](tests/probe_native_identity.py)
matched the Python controller's identity for an owned process whose executable
path contains Unicode on macOS. It rejected a different process, changed birth time,
changed executable and an exited process. Across 100 local batches of 32 checks,
the median batch-average was 0.002523 ms per check and p95 was 0.004921 ms.
The separate experiment does not reproduce an actual reused PID, and the
measurement is not an end-to-end request or portability guarantee.
The Linux, Windows and macOS gateway jobs in [this source run](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41442611268)
also passed the isolated native identity probe. It covered a non-UTF-8 Linux
executable path, Windows Unicode case mapping, process exit and synthetic
birth/executable mismatches. The runners covered Linux x86-64, Windows x86-64
and macOS ARM64; they did not reproduce actual PID reuse or validate every OS
architecture. Windows uses the same invariant-locale case mapping as
[Python's `normcase`](https://raw.githubusercontent.com/python/cpython/3.11/Lib/ntpath.py)
instead of assuming that Go's Unicode case conversion is identical.

Apple's [libproc source](https://github.com/apple-oss-distributions/xnu/blob/main/libsyscall/wrappers/libproc/libproc.c)
uses `proc_pidinfo` and `proc_pidpath`; Windows documents [process creation
times](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes)
and [executable paths](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-queryfullprocessimagenamew).
Linux exposes process start information through [procfs](https://www.kernel.org/doc/html/latest/filesystems/proc.html).
These APIs motivate a native per-request candidate rather than assuming that a
separate interpreter is free. A macOS C binding introduces a compiler and
cross-compilation requirement; a pure-Go dynamic binding would introduce a
new dependency. The actively maintained [purego project](https://github.com/ebitengine/purego)
had an open [ARM64 by-value struct ABI report](https://github.com/ebitengine/purego/issues/544)
and an [overhead discussion](https://github.com/ebitengine/purego/issues/399)
when checked. Those reports do not establish a defect in calls that pass only
pointers. The source candidate uses the platform-native APIs without the extra
dependency. The already-running ingress does not gain the check from a source
or translator update; platform-specific candidate gates and a way to migrate
that ingress without interrupting its streams remain necessary.

The candidate rejects a missing or altered private identity before sending any
HTTP bytes to an owned synthetic origin. Local isolation also covered recovery,
a real Claude client across translator rollback, and in-flight stream drain.
Five paired local runs with fake origins and highly repetitive 100 KB, 1 MB and
4 MB payloads had median times of 0.113/0.111, 0.192/0.189 and 0.488/0.484
seconds for the previous/candidate gateways respectively. These small
synthetic samples do not measure provider latency, prove an improvement, or
cover actual PID reuse, all architectures, or live-session ingress migration.

## Status-line settings scopes (2026-11-02)

[Claude's settings documentation](https://code.claude.com/docs/en/settings)
describes command-line, project-local, project and user precedence, including
special project-local placement for worktrees. An upstream [issue report](https://github.com/anthropics/claude-code/issues/80791)
describes a profile's ancestor `.claude` directory being treated as project
scope. A disposable non-Git parent-directory fixture using Claude Code 2.1.316
did not reproduce that report; it does not duplicate every condition in the
report and is not evidence that the upstream issue is resolved.

Our separate isolated fixture did reproduce a bridge-specific precedence issue:
the generated command-line status line overrode project and local settings.
The installed startup mitigation leaves detected or uncertain cases to the native
client, with no bridge context correction for that status line. Local synthetic tests
covered ordinary project/local files and a real linked Git worktree with its
local setting in the main checkout. Mid-session file changes, `/cd`, unusual Git
layouts, authenticated Windows interaction and arbitrary plugins remain open.

Claude's [status-line documentation](https://code.claude.com/docs/en/statusline#workspace-trust-required)
states that status-line commands follow workspace trust. A closed
[ccstatusline report](https://github.com/sirmalloc/ccstatusline/issues/552)
describes a version-specific nested-repository trust symptom; its closure does
not establish behavior in this installation. The active ccstatusline project
had a recent push on 2026-11-02, but its popularity and that report do not prove
compatibility or safety for this bridge.

[statusline-injector](https://github.com/agfpd/statusline-injector#how-it-works)
documents a different approach: a `SessionStart` hook rewrites a project
`statusLine.command` to add a wrapper. As of 2026-11-02 its repository was not
archived, had last pushed on 2026-10-17, and had no open issues; the absence of
issues is not a maintenance or security audit. Its README claims local-only
storage, which was not independently verified here. The project was inspected
read-only and not installed or executed. Automatically editing project settings
would introduce trust, concurrency, rollback and `/cd` questions, so the bridge
does not adopt that mechanism as a default.

## macOS monitor identity (2026-11-03)

The existing monitor cannot voluntarily reload its code. Apple's open-source
[libproc interface](https://github.com/apple-oss-distributions/xnu/blob/main/libsyscall/wrappers/libproc/libproc.h)
exposes `proc_signal_with_audittoken`; its
[implementation](https://github.com/apple-oss-distributions/xnu/blob/main/libsyscall/wrappers/libproc/libproc.c)
passes the token to the kernel. XNU also defines the combined BSD and process
generation reader in a [private header](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/proc_info_private.h).
[CodexBar](https://github.com/steipete/CodexBar/blob/main/docs/DEVELOPMENT.md)
uses generation-bound signaling for its own test-process cleanup. At review the
repository was active and MIT-licensed, with about 23,000 stars and 102 open
issues. Those numbers are context, not an audit; no dependency was installed.

On this Mac, [our owned-process fixture](tests/test_macos_quota_handoff.py)
observed a wrong generation rejected, including after same-PID `exec`, and a
matching signal reaching only the owned target. It also held the account lock
during a fake usage operation, then released it before signaling the owned
monitor and allowing a synthetic successor to acquire its locks. The sentinel
survived. The successor then initialized the actual watcher against a fake
runtime and recovery result, producing a protocol-2 report and a ledger. This
is not proof of actual PID reuse or a safe live migration. Before
any real handoff we must independently establish that the intended monitor owns
the lock, bind all identity checks to its generation, ensure a successor is
ready, and test failure and rollback paths. The private metadata ABI and other
macOS versions may differ; unsupported reads must fail closed. Windows and
Linux need their own identity-bound mechanisms.

One read-only observation of the existing monitor found the expected command,
an open lock file, and unchanged process identity across the observation; it
did not establish exclusive lock ownership. On this host the native executable
matched the current interpreter's native process image but not Python's
`sys.executable` launcher path. A synthetic negative control also showed that
an unrelated reader appears as an opener of the same file; the local `lsof`
lock field was blank for both the flock holder and reader in that fixture.
That observation is not a claim about every macOS version. Any eventual owner
check must account for both facts. Apple's [fcntl documentation](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/fcntl.2.html)
describes the read-only `F_GETLK` query; the local macOS manual specifies that
an `flock` conflict does not reveal a process owner. An owned fixture observed
both a conflict and its removal without acquiring the lock. The status source
reports this observation on macOS without claiming process liveness; it does
not acquire a live lock, which could race a watcher startup.

The separate [monitor-evidence candidate](quota_monitor_identity.py) combines a
recent private report, an identity snapshot, expected process arguments, lock
file identity, `F_GETLK`, and the set of processes visible to `lsof`. It rejects
multiple visible openers and repeats the identity and lock observations. Local
fixtures cover ambiguity and malformed expectations, and one read-only check
matched the existing monitor. The evidence is not atomic, `lsof` visibility is
not an exclusive-ownership proof, and the candidate sends no signals. It is not
installed or used automatically; no live handoff follows from this result.

The separate [fixture-only handoff experiment](quota_monitor_handoff.py) uses
the account lock, prepares a successor, rechecks evidence, and uses an
audit-token-bound signal in a credential-free temporary runtime. It refuses
non-fixture roots and processes that are not direct children of the caller. A
synthetic adversarial control replaced the source just while the successor
loaded it, then restored the original inode before the second observation. The
unreviewed child was armed despite the original source hash and matching
metadata; the signal was mocked and the original test monitor remained alive.
This blocks live use. Atomic versioned controller publication and consistent
dependency imports remain prerequisites, along with handoff and rollback tests.
The fixture redirects its child home and uses synthetic source; it is not an OS
sandbox for arbitrary untrusted programs.

A separate [fixture-only snapshot publisher](quota_snapshot_fixture.py) checks
an explicit source manifest and publishes a read-only directory with Darwin's
exclusive atomic rename; it does not replace an existing destination. Apple
[documents that support is volume-dependent](https://developer.apple.com/documentation/foundation/urlresourcevalues/volumesupportsexclusiverenaming);
the fixture fails closed when the operation is unavailable. Local
tests cover concurrent publishers, malformed or changed files, failed staging,
unsupported rename, and process exits immediately before and after publication.
An interrupted staging directory remains for a later safe cleanup design; it is
not silently deleted. A watcher loaded from the published fixture snapshot and
its immediate imports used a separate temporary runtime root for synthetic
quota recovery, without importing a conflicting top-level source. This is not
a complete dependency audit, a power-loss guarantee, same-user tamper
resistance, or a live migration. The handoff does not use this publisher yet.
