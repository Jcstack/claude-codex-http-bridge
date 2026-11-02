# Subscription prompt-cache audit

Initial controlled check: 2026-11-01. Claude Code remains the harness. These
requests use synthetic references through the installed subscription route,
without a project API key or a user transcript.

## What is established

Actual long-running session metadata contains substantial cached input. The
response mapper already converts reported cache reads and writes, subtracting
them from fresh input to avoid double-counting. It also handles the upstream
`cache_creation_tokens` alias. A reported zero write counter is not proof of a
pricing policy, and a cached-token fraction is not a request hit rate.

A small requested `gpt-6.1-sol` control kept a fixed reference and cache key,
then changed the lookup question. Every answer was correct:

| Requested policy | Repeat | Input | Reported cached input | Reported cache writes | Total seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| automatic, no policy field | 1 | 2662 | 0 | 0 | 2.153 |
| automatic, no policy field | 2 | 2662 | 2432 | 0 | 2.486 |
| implicit, 30m | 1 | 2665 | 0 | 0 | 2.535 |
| implicit, 30m | 2 | 2665 | 2432 | 0 | 2.413 |
| explicit, no breakpoint | 1 | 2660 | 0 | 0 | 2.395 |
| explicit, no breakpoint | 2 | 2660 | 2432 | 0 | 2.850 |
| explicit, with breakpoint | 1 | 2660 | 0 | 0 | 2.564 |
| explicit, with breakpoint | 2 | 2660 | 0 | 0 | 2.109 |

These observations demonstrate prefix reuse and also retain cache misses.
They do not establish a latency improvement or working manual cache controls.

## Dynamic-tool prefix gate

The credential-free `tests/probe_proxy_features.py` sends synthetic Claude
Messages requests through a candidate translator to a loopback Responses
server. For one synthetic session, it checks the exact UTF-8 bytes of the
tool definitions and their order, the instructions field, and each prior
input item when ToolSearch reveals a deferred tool and that tool is later
removed. In this fixture the synthetic Claude system prompt appears in the
first Responses input item with the developer role; the separate instructions
field is empty. The same session's nonempty `prompt_cache_key` stays stable,
while a second synthetic session receives a different key. Removing the tool changes
the allowed-tool selection intentionally; the entire request is not expected
to remain byte-identical. The test keeps captured fixture requests only in
memory and reports booleans rather than bodies or cache keys.

The gate applies to this fixture and fake API-profile origin: it does not prove
cache hits, native subscription executor behavior, account isolation, TTL,
pricing, or prefix stability for arbitrary Claude Code versions or histories.
The checked prefix follows the general guidance in
[OpenAI's prompt-caching documentation](https://developers.openai.com/api/docs/guides/prompt-caching)
and [Claude's tool-caching documentation](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching),
but public API behavior is not assigned to the subscription route by inference.

A second gate, `tests/native_cache_fixture.go`, exercises the pinned Codex
executor itself with synthetic OAuth-shaped metadata and a loopback origin.
It verifies deferred-tool reveal/removal semantics and stable tool, system and
history prefixes. For this fixture, the key stays stable across tool changes
and effort changes, and differs across synthetic sessions, agent headers and
resolved models. The selected `max` and `high` effort values are also retained.
The upstream executor tests separately cover model aliases and matching HTTP
and WebSocket key derivation. The isolated Claude client fixture emits an agent
header, and the rolling gateway fixture preserves synthetic session and agent
headers through a backend switch. These checks do not verify forwarding by
every Claude client version, actual provider cache hits, or isolation between
real accounts.

The executor fixture has also been extended to add a deferred tool that was
absent from the first request, reveal it, and then remove it. Its new controls
check the cache key, prefix bytes, discovery, and removal separately. The
[executor build and test steps](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41478669731)
passed on macOS, Linux and Windows. That workflow was canceled as superseded
after its Linux and Windows contract jobs passed; its macOS contract job did
not complete. The [later candidate gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41480503513)
passed on all three platforms. This fixture does not run a Claude client or
test provider cache hits.

Stable request prefixes alone do not prove tool-definition correctness. A
separate synthetic changed-description observation retained the old definition
inside the same Claude conversation even after resume; see [MCP_NAMING.md](MCP_NAMING.md).
In that same isolated macOS observation through the selected translator and a
fake API-compatible Responses server, the initial and resumed requests used
the same nonempty cache key and byte-identical initial tool/instruction fields;
the resumed first input retained the final pre-resume input as a prefix. The
fixture reports booleans without returning keys or request bodies. This is one
synthetic resume with a stale tool description, not proof of a provider cache
hit, correct plugin semantics, real subscription behavior, fork behavior or
cross-account isolation. The [same-resume structural gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41492286977)
passed on macOS, Windows and Linux.
An additional local synthetic `--fork-session` continuation produced a
different session ID and cache key while retaining the compared input prefix
and unchanged sampled tools and instructions. The stale description remained
visible in this fork. The [resume-and-fork structural gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41492617631)
passed on macOS, Windows and Linux. A distinct key does not prove provider-side isolation,
cache misses or hits, correct plugin behavior, or real-account separation.
Correct current tool semantics take priority over preserving a cache prefix.

## Live dynamic-tool cache observations

On 2026-11-02, two independent synthetic histories were sent sequentially
through the installed managed Messages gateway and the selected subscription
account, requesting `gpt-6.1-sol` at low effort. Each history used a unique
synthetic session ID and a stable reference of about 2,700 input tokens. The
fixture supplied a ToolSearch call and its tool-reference result itself; it
did not ask Claude Code to execute a real tool or use a user transcript.

| Round | Phase | First reported cache read | Repeated reported cache read |
| --- | --- | ---: | ---: |
| 1 | Before tool reveal | not reported | 2,560 |
| 1 | Tool revealed | 2,560 | 2,816 |
| 1 | Tool removed | not reported | 2,816 |
| 2 | Before tool reveal | not reported | 2,560 |
| 2 | Tool revealed | 2,560 | 2,816 |
| 2 | Tool removed | 2,816 | 2,816 |

Both revealed and removed phases returned the synthetic reference answer.
The initial control responses requested a synthetic tool; no tool was executed.
The creation counter was not reported in these responses. An absent read
counter is retained as unknown rather than converted into a measured zero.
One removal request did not report a read and another did: these samples do
not establish deterministic invalidation, a hit rate, an account boundary,
retention, pricing or a latency benefit. They also do not exercise a real
Claude client's ToolSearch orchestration or prove future backend behavior.

Reproduce with:

```sh
python tests/live_dynamic_cache.py --root PRIVATE_ROOT --confirm-live
```

The explicit flag sends six small synthetic model requests and consumes
subscription quota; the script stops if account selection changes, does not
start or stop services, and withholds keys, replies and session IDs.

An [open Claude Code report](https://github.com/anthropics/claude-code/issues/83756)
describes cache rebuilds after loading larger batches of MCP tools. It concerns
another user's setup and is not evidence that this bridge has the same issue.
The live samples above reveal only one synthetic tool.

`tests/probe_toolsearch_cache_batch.py` now runs the locked Claude client with
one and nine synthetic MCP tools through the candidate translator and a fake
Responses origin; their synthetic search queries are 51 and 411 bytes. In both
local cases, the selected tools were deferred and
then revealed, while tool-definition and instruction bytes, previous input
items and the synthetic session cache key remained stable. This is a structural
check of real Claude ToolSearch orchestration, not a measurement of provider
cache reads. Live multi-tool batches and other client versions remain open.

## Same-account synthetic session scope

On 2026-11-02, two independent rounds each sent the same fresh synthetic
prefix through the managed subscription route with two newly generated session
IDs, in the order A-first, A-repeat, B-first, B-repeat, A-after-B. Each request
used Sol at low effort; every response contained the expected synthetic value.
The selected account was checked before and after each request. No tool ran.

| Round | A first | A repeat | B first | B repeat | A after B |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | not reported | not reported | 2,432 | 2,432 | not reported |
| 2 | not reported | not reported | not reported | 2,432 | 2,432 |

Values are reported `cache_read_input_tokens`; creation counters were not
reported. Each request reported about 2,646–2,648 total input tokens when
uncached input and cache reads are added. “Not reported” is not proof of zero
or a cache miss. The first B request in one round reported a read, but this
does not establish which prior request populated it, how the provider partitions
or routes caches, or whether identical prefixes are shared between sessions.
The two rounds used fresh synthetic prefixes, and no second account was tested.
An isolated TLS ingress and translator sent the same synthetic content to a
fake origin with session order A, A, B, A. The translated A cache key stayed
stable and differed from B's; the pinned-executor fixture separately checks
session, child-agent and model scope. A further in-process test of that executor's
Responses route verifies that an incoming cache key is preserved in its native
request body and `Session-Id` header. These compositional tests do not inspect
the production request's internal key or prove provider cache isolation.
The [public OpenAI API caching guide](https://developers.openai.com/api/docs/guides/prompt-caching)
describes distinct keys as a way to separate accounting and help prevent
cache-hit probing. The tested Codex subscription backend is a different route,
so that API documentation alone does not establish its isolation rules.

```sh
python tests/live_cache_scope.py --root PRIVATE_ROOT --confirm-live
```

The explicit flag sends five small synthetic model requests and consumes
subscription quota; the script stops if account selection changes, does not
start or stop services, and withholds keys, replies and session IDs.

## Why acceptance is insufficient

The reviewed upstream request converter explicitly removes top-level
`prompt_cache_options`/`prompt_cache_retention` and nested
`prompt_cache_breakpoint` hints. See its
[request converter](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/internal/translator/codex/openai/responses/codex_openai-responses_request.go)
and the converter's regression tests. Consequently, these 200 responses cannot
be used as evidence that the native backend honored those fields. Nor does
this filtering alone prove that the current backend would reject them if sent.

A separate direct-backend check bypassed that normalization. The original
credential owner substituted its token internally for a synthetic request to
the fixed Codex Responses endpoint; no OAuth token or response body was
exported to the probe output. With the same selected account and requested
`gpt-6.1-sol`, the baseline completed correctly with HTTP 200. The following
individual controls returned HTTP 400 with an unsupported-parameter error
identifying the relevant cache field:

- `prompt_cache_options` with explicit mode and a 30-minute TTL.
- `prompt_cache_options` with implicit mode and no TTL.
- A nested `prompt_cache_breakpoint`, without top-level cache options.

This establishes the boundary for that account, model and backend at the time
of the check. It is not a claim about every model, future backend revisions or
the public project API. Compatibility filtering remains enabled; silently
discarding a field must not be advertised as support for its semantics.

The [current public Responses documentation](https://developers.openai.com/api/docs/guides/prompt-caching)
defines manual policies and breakpoints, while
[Claude tool-cache documentation](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching)
describes a different tool/history contract. Route-specific translation and
backend acceptance must be checked before enabling a control. Public API
retention and pricing are not assigned to the subscription route by inference.

## Provider cache diagnostics and route boundary

OpenAI's [Responses API cache diagnostics](https://developers.openai.com/api/docs/guides/prompt-caching/diagnostics)
can compare a request with a recent response from the same organization, using
`prompt_cache_options.comparison_response_id`. The result can identify a first
classified difference; `unavailable` and a missing comparison record are not
cache hits. In a streamed response, the provider places the diagnostic in the
completed-response event. Reported cache-read counters remain the measure of
actual reported reuse; a comparison by itself neither changes caching nor
guarantees a hit. The official feature's availability and retention rules
describe the public Responses API, not the private Codex subscription route.

On this Mac, [a disposable fake-upstream observation](tests/probe_cache_diagnostics_route.py)
tested both the original pinned proxy binary and the locally selected reviewed
translator in their API-profile configuration. For synthetic nonstreaming and
streaming Messages requests, a synthetic top-level comparison option did not
reach the fake Responses upstream, and a synthetic diagnostic in the completed
upstream event did not reach the Messages client. The same samples did expose
the fake cache-read token count. This does not show that Claude Code sends that
nonstandard request field, that a real provider supports or rejects diagnostic
comparisons, or that every future translator has the same behavior. No model,
account credential or running bridge service was used. The manually dispatched
[cache observation workflow](.github/workflows/cache-resume-observation.yml)
can repeat the fixture against the reviewed translator on three platforms.

The previously tested Codex backend rejected explicit and implicit
`prompt_cache_options` for one account and model; the comparison-only option
has not been independently tested there. Do not enable diagnostics on the
subscription route from the API documentation alone. A future implementation
must verify exact route and model support, preserve diagnostics through both
streaming and nonstreaming translation, scope baseline IDs to the appropriate
account and session, avoid exposing them in public logs, and obtain a fresh
positive and negative control. It must not issue extra requests, enable a
separate billed API route, or change the user's cache settings implicitly.

After the 2026-11-02 safety-backend handoff, a read-only audit of the tracked
main session and its children used a window beginning when the new generation
directory was created, shortly before publication. Four deduplicated responses
reported 2,527,636 total input tokens, including 2,524,032 cache-read tokens
(99.8574% of reported input); none of those four omitted the cache-read counter.
The aggregate is not a request hit rate, a single context size or a billing
measure. Since there is no cross-layer request ID, the window alone cannot
attribute every response to the new backend or establish causality for a hit.

A later one-hour read-only snapshot of the same tracked session and children
contained 50 deduplicated responses with usage. Their reported input included
32,784,512 cache-read tokens out of 32,861,312 total (99.7663%); none omitted
the cache-read counter. This is a token share for the observed window, not a
request hit rate, billing measure or guarantee about other sessions or accounts.
No API-error records appeared in that tracked snapshot; it does not establish
that every response completed or that no error was hidden by a retry.

A credential-free interactive macOS fixture also switches through all five
effort levels in separate Sol, Astra and Luna mapped sessions. At the fake
Responses origin, the cache key stayed the same, nonempty tool definitions
compared equal, and previous
input items remained a structural prefix. These comparisons use parsed JSON,
not a byte-for-byte assertion or a provider cache-hit measurement. The same
fixture skips the interactive console on Windows.

## Status-line cache metrics

[Claude's status-line schema](https://code.claude.com/docs/en/statusline#prompt-cache-fields)
defines `hit_ratio` as cache-read tokens divided by the reported input tokens
for the main conversation, not as the fraction of requests with a hit. The
[OpenAI API caching guide](https://developers.openai.com/api/docs/guides/prompt-caching#monitor-cache-performance)
also recommends comparing cached and total input tokens. The public API's
controls, retention and pricing are not assumed to apply to the subscription
route.

In an isolated real-Claude terminal using a fake Messages service, two
synthetic responses reported 2,000 fresh tokens, then 500 fresh plus 1,500
cache-read tokens. Claude's status line reported two requests and a 0.375
cache-read token fraction, while also displaying a `5m` lifetime and warmth
even though the fake service supplied no lifetime. The [reproducible probe](tests/probe_statusline_cache.py)
uses a temporary home and no provider. Its optional `--hud-dir` path pipes
the real client's synthetic status through the bridge adapter and the reviewed
HUD, then checks the displayed fraction and the absence of a timer. This full
chain passed on macOS and Linux in the [HUD gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41456511723),
and locally with the installed HUD package. These are fake responses and do not
establish the values in a real session. Windows console behavior remains
unverified; the Windows job covers non-interactive synthetic rendering.

The adapter continues to remove the standard `prompt_cache` object by
default. In an opt-in extension, it passes the validated `requests`,
`caching_observed` and `hit_ratio` fields under
`claude_codex_bridge.reported_main_cache`, while withholding inferred warmth,
expiry, lifetime, miss diagnosis and cache-write accounting. An absent cache
report is not proof that caching is off. These values describe counters
reported to Claude for the main conversation, not a provider-wide hit rate, a
guaranteed cache lifetime, or a bill. A continuing session can span account or
model changes, so the counters do not establish account-level cache isolation.
The shared preference passed the [three-platform source gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41455750069)
and was installed and enabled locally without restarting the origin, ingress,
or Claude clients. It applies on the next status-line refresh for existing and
future subscription sessions. An explicit per-session environment setting
takes precedence when recorded in the session profile. Older launchers did not
record an explicit `0`. The installed adapter now checks the inherited session
environment before the shared preference. If the client strips that environment
value, it cannot be recovered from an older profile. An isolated real-client
positive and negative control reproduced the former behavior and verified the
new behavior with a fake enabled preference. The [HUD gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41459059115)
passed on three platforms, with the interactive flow on macOS and Linux. The
installed adapter and HUD also passed that synthetic flow on this Mac.
`claude-codex cache telemetry status` reports the shared choice; `disable` turns
it off. Plugins need explicit support for the extension to display it. The
default remains disabled for a new installation.

An experimental [HUD adapter patch](upstream/patches/claude-hud-v0.15-reported-cache.patch)
teaches the reviewed HUD version to display the extension as “Reported
cache-read input (main)” when its existing prompt-cache display is enabled.
The [synthetic display fixture](tests/probe_hud_cache_display.py) checks the
token fraction, disabled and missing cases, malformed values and unchanged
native cache display. It never invents a lifetime or warmth. The reviewed patch
is installed in the local HUD 0.15.0 package; it does not change an upstream
release. The installed adapter and HUD were also checked together with a
synthetic frame after the shared preference was enabled. A real user-session
refresh with a cache report has not yet been observed. The main-session counter
may include model or account changes.

Passing a partial object as the standard `prompt_cache` is unsafe for some
plugins. For example, [this status-line implementation](https://github.com/tsalkin/claude-code-statusline/blob/17396d0bb11a47b184c7eb3709088dac36d7c91d/statusline.sh)
defaults a missing `warm` value to false and displays cold when a `hit_ratio`
is present. That is a source-code observation for one version, not a survey
of every plugin. It motivates the separate opt-in namespace rather than an
unsupported claim that the provider cache is cold or warm.

## Next gates

- Trace control fields across each hop using a fake origin, without enabling
  request-body logging on the user's production service.
- Recheck actual backend acceptance when the backend or model changes, before
  changing normalization. Preserve the existing refresh owner and actual rate
  limits; do not blindly remove compatibility guards.
- Establish implicit/explicit positive and negative controls, stable dynamic
  tools, compaction/resume/fork, model/effort changes and account isolation.
- Reconcile token counters with raw provider fields and preserve unknowns.
  Assess quality and comparable latency rather than optimizing one hit figure.

Reproduce a baseline with `tests/probe_native_cache_policy.py`; its optional
control requests are discovery experiments, not a compatibility certification.
No new retention or manual-cache default was enabled by this audit.
