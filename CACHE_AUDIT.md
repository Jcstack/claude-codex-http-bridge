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
