# Streaming and live interaction

Checked 2026-10-30 with Claude Code 2.1.314 and CLIProxyAPI 8.0.46. Claude Code remains the harness; OpenAI supplies model inference through the local HTTP bridge. Three different capabilities are often called “live stream”:

| Capability | Current result | Protocol boundary |
| --- | --- | --- |
| Progressive text/tool output | **Works in the tested paths.** A live GPT-6.1 Sol subscription-route `claude-codex -p --output-format stream-json --include-partial-messages` run returned 320 stream events over about 34 seconds and ended successfully. A fake OpenAI API-key upstream sent two text deltas 250 ms apart; the proxy forwarded both as distinct Anthropic Messages SSE deltas before `message_stop`. The same fake route forwarded a tool's two partial JSON argument deltas before its completion event. | Claude Code consumes Anthropic Messages SSE; CLIProxyAPI translates OpenAI Responses SSE. The fake upstream is a protocol test, not a live API-key model test. [OpenAI SSE guide](https://developers.openai.com/api/docs/guides/streaming-responses). |
| GPT-6 mid-turn steering | **Not available through this Claude Code HTTP path today.** OpenAI accepts `response.steer` on a persistent Responses WebSocket while a GPT-6 response is running. CLIProxyAPI has WebSocket steering code for Codex-protocol clients, but Claude Code sends Anthropic Messages HTTP requests to this bridge, not a Responses WebSocket steering frame. A separate HTTP turn after the first finishes is ordinary follow-up, not mid-turn steering. | A transparent implementation would need a Claude Code input event while its current request is open, a bridge-owned upstream WebSocket, and a tested mapping for interrupted text, tool calls, approvals, and continuation. No such end-to-end mapping is claimed here. [OpenAI steering guide](https://developers.openai.com/api/docs/guides/steering). |
| GPT-Live full-duplex voice | **Separate protocol and UI.** Real Realtime audio/transcript streaming now works through the existing subscription, without a project API key, via the optional [MCP pack](NATIVE_TOOLS.md). This does not establish GPT-Live entitlement or a full-duplex Claude terminal: the native WebRTC bootstrap returned 400 and Claude channels require Anthropic authentication. Video and steering remain unverified. [Live WebSocket protocol](https://developers.openai.com/api/docs/guides/voice-websockets), [channels gate](https://code.claude.com/docs/en/channels). |

For the existing terminal workflow, launch `claude-codex` normally. Its interactive UI already renders the streamed response. To inspect machine-readable partial events in a headless task, pass Claude Code's `--output-format stream-json --include-partial-messages --verbose` flags; the wrapper forwards them. This is useful for measuring time to first visible output and detecting broken SSE framing without logging prompt bodies or credentials.

Claude Code's [gateway guide](https://code.claude.com/docs/en/llm-gateway-protocol#streaming)
describes SSE pings as one way to prevent client-side idle timeouts during
long pauses. The [pinned translator's configuration](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/config.example.yaml)
documents streaming keep-alives as disabled by default; neither running local
generation explicitly enables them. A separate [upstream discussion](https://github.com/router-for-me/CLIProxyAPI/discussions/2138)
reports a similar incomplete-stream symptom and a participant's configuration
change, but does not establish this installation's cause. Downstream keep-alives
cannot reconstruct a missing upstream terminal event. Their framing, timing,
error handling, cancellation and interaction with safe retries still need an
isolated end-to-end test before changing the active runtime.

### Forwarded incomplete-stream failures

In a read-only review of one tracked session and its children on 2026-11-04,
one main-session incomplete-stream record and one managed-backend error with
the same sentinel occurred in the same minute. No cross-layer request ID was
available and the screenshot had no confirmed time. These observations do not
prove they are the same request, which component caused the original missing
terminal event, or whether other clients were affected. A logged HTTP 200 is
not a completed stream. OpenAI documents `response.completed` as the terminal
completion event in its [Responses streaming reference](https://developers.openai.com/api/reference/resources/responses/streaming-events).

A separate credential-free reproduction shows a distinct recovery defect in the
pinned two-layer translator. When the fake origin closes early, the executor's
locally synthesized failure is request-scoped and a later request can reach the
origin. When that exact failure is forwarded as a `server_error` /
`request_timeout` terminal event, the receiving translator loses its scope and
cools the credential; the next independent request returns 503 without reaching
the fake origin. The [candidate fix](upstream/patches/forwarded-incomplete-stream-candidate.patch)
recognizes the exact sentinel, type and code only, with a 502 or 408 status;
it preserves the error body and prevents credential cooldown and implicit
cross-account failover for that failure. It does not resume or replay the
interrupted stream. Synthetic controls retain quota, authentication, unrelated
timeout and explicit conflicting-status behavior.

On macOS, a pinned real Claude Code client using only a fake origin completed
the partial-argument and forwarded `response.failed` cases with one synthetic
write and a retained tool result. Completed-argument and completed-item cases
also wrote at most once. A second fixture connects two separate owned proxy
processes to a fake origin: the inner proxy synthesizes the missing-completion
error itself, and the outer baseline reproduces the 503 while the candidate
admits the next request for both streaming and non-streaming clients. The pinned
Claude client also completed partial- and completed-tool cases through both
processes with one synthetic write. This is not an exactly-once guarantee for
real tools, every client version, or Windows. In another local two-layer
fixture, two interleaved synthetic tools each ran once and both results were
retained across three EOF positions; this does not establish exactly-once
execution for arbitrary parallel tools or timing. The unmodified and fixed
proxy are compared by [a standalone fixture](tests/probe_forwarded_incomplete.py).
The fix is included in the next source-lock candidate for the full proxy gate;
it is not active in the existing managed backend. The separate
[metadata-only observer](upstream/patches/codex-stream-observation-candidate.patch)
is not part of that source lock and is not active. The observer is
disabled by default; if explicitly enabled in a future candidate, it logs only
allowlisted event/outcome/error categories, bounded timings and a local request
tag. It is not an upstream network-idle measurement: time since a read can
include processing and backpressure. A [manual three-platform workflow](.github/workflows/forwarded-stream-observation.yml)
uses synthetic traffic and no account credentials. Further validation and a
draining managed-backend handoff are required before any activation.

A separate local before/after fixture compared synthetic model, instructions,
tools, input, reasoning, tool choice and prompt-cache key bytes across three
dynamic-tool stages and found them unchanged. It does not measure provider
cache hits or establish cache behavior after a real interrupted request. A
separate two-layer local fixture also compared those raw fields and the
synthetic session cache key between two identical requests on either side of
an EOF; it does not imply that a real Claude retry sends identical content.

Community reports such as [CLIProxyAPI issue 6469](https://github.com/router-for-me/CLIProxyAPI/issues/6469)
show both short and long incomplete streams; they do not establish a universal
timeout or this installation's cause. The unmerged [diagnostics proposal 6497](https://github.com/router-for-me/CLIProxyAPI/pull/6497)
is useful background but does not cover every terminal-error path. No
user-generated prompt, transcript, error body, or credential is needed in the
observer output.

An initial isolated test of the pinned translator enabled a one-second
keep-alive against a fake delayed Responses upstream. After the first output,
the downstream stream included keep-alive comments and finished normally. With
pre-first-token keep-alives disabled, a delayed upstream failure remained an
HTTP 503; enabling them committed HTTP 200 first and delivered that failure as
an in-band stream error. The latter can change client retry behavior. With
post-first-token keep-alives, an isolated Claude client completed a synthetic
response; separate fake-stream cases showed cancellation reaching the fake
origin and a truncated stream neither being replayed nor marked complete. A
second disposable TLS ingress preserved the keep-alive, let the Claude client
finish, and forwarded cancellation to the fake origin. These fixtures do not
test the active ingress or recovery from a missing upstream terminal, and have
not changed runtime settings.

On 2026-11-02 one short, low-effort Sol request used a disposable TLS ingress
and a one-second keep-alive against the selected subscription origin. It
completed in 2.81 seconds, with one keep-alive comment, one `message_stop` and
no reported stream error; the existing origin and ingress identities remained
unchanged. This is a connectivity sample, not evidence that a long request or
the recurring incomplete-stream failure is fixed.

### Incomplete tool-call safety, 2026-11-02

A credential-free fake Responses origin exposed a separate boundary bug in the
then-running translator. It sent a valid JSON argument followed by
`response.incomplete` / `max_output_tokens`, while the function item still had
`status: in_progress` and had not emitted `response.output_item.done`. The old
translator closed the tool block and the real isolated Claude client executed
one synthetic write. This is a reproduction with an owned fixture; it does not
establish that the active user session encountered this event. OpenAI documents
[`response.incomplete` and item lifecycle events](https://developers.openai.com/api/reference/resources/responses/streaming-events),
while Anthropic says an incomplete tool block at a [token limit](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons#incomplete-tool-use-blocks)
requires special handling.

The [candidate guard](upstream/patches/incomplete-tool-guard.patch) reports an
error instead of completing an unconfirmed streamed client-tool block. It also
rejects an explicitly unfinished `output_item.done`; a tool explicitly marked
completed remains eligible for normal handling. HTTP and WebSocket non-stream
requests fail with a request-scoped 502 instead of cooling the account. The
native executor and real-Claude fixtures use synthetic traffic: they cover
partial JSON, valid but unconfirmed JSON, an unfinished item, a completed-item
control, and two parallel tools with a completed first item. The Claude client
can independently retry after the error; in the tested parallel fixture the
completed result was retained and neither effect was duplicated. These samples
do not prove exactly-once behavior for every plugin, cancellation or retry.
Separately, [a before/after fixture](tests/probe_incomplete_cache_handoff.py)
compares exact synthetic model, instructions, tools, input, reasoning, choice
and session cache-key bytes across three dynamic-tool stages, including a long
gateway name. The safety-only candidate preserves them; enabling the separate
experimental name improvement intentionally fails this comparison. This is not
a measurement of the provider's actual cache hits.
If Claude records the guard's fixed error as an API-error message, the local
session audit reports the allowlisted `incomplete_tool_call` category without
printing that message. An automatic client retry may not leave such a record;
absence of the category is not proof that no guard fired.

On 2026-11-02, the safety-only candidate passed a [three-platform gate](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41345932840)
and was rolled into the local managed backend. The selected account, original
origin and stable TLS ingress remained unchanged. One old stream was still
active during handoff and subsequently drained; a new backend inference later
completed HTTP handling without a recorded stream error. HTTP 200 alone does
not prove a complete answer. No extra live inference was sent for this rollout.
The original origin and sessions still directly connected to it retain the old
translator and need a separately safe migration. This issue is distinct from the
previously observed EOF/503 errors, whose root cause remains unknown.

### POSIX cancellation and fixture isolation

On the local macOS host, the non-interactive `ctrl_c_between_effects` fixture
timed out repeatedly when the installed Claude Code 2.1.316 ran with the
existing HOME, despite a foreground pseudoterminal with `ISIG` enabled. At one
timeout the process was reported as trying to exit; this does not establish
whether SIGINT was ignored. A byte-identical, locked client at another path
passed with that HOME, and the installed client passed the original focused
fixture with an empty temporary HOME. A separate minimal pseudoterminal
control also received SIGINT. These observations narrow the difference to the
launch environment and path together, not a proven Claude or bridge root
cause; interactive sessions and Windows console cancellation remain untested.

The credential-free client fixtures now use a temporary HOME and, on Windows,
temporary profile and application-data paths. They also retain their separate
Claude configuration directory. The temporary translator helper and the
keep-alive probe also pass a limited environment to their disposable proxy
processes. Claude's [settings documentation](https://code.claude.com/docs/en/settings)
describes additional global state outside individual project settings, so a
separate configuration directory alone should not be treated as a complete
home-directory isolation guarantee. This environment change is not an OS
sandbox. The parallel fixture also falls back to terminating its own child if
the owned process-group signal is denied; its original Ctrl-C timeout remains
a failure. Other proxy launch paths and the live subscription clients have not
all been migrated to this helper and still require a separate privacy review.

### Upstream WebSocket keepalive test timing

The macOS build job in [candidate run 41487752563, attempt 1](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41487752563/attempts/1)
failed the pinned upstream `TestCodexWebsockets_KeepalivePingDuringUpload_NonstreamSessionless`:
the synthetic client attempted a write after its local test connection had
closed. This occurred before the new schema contract tests ran. The same
candidate had passed an earlier three-platform gate; neither result explains
the cause of a real user's stream failure.

In the [pinned test source](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/internal/runtime/executor/codex_websockets_executor_test.go),
the fake server sends completion and returns immediately after receiving a
pong, while that same pong releases the client's blocked payload write. A
server close racing the write is a plausible fixture-level explanation, not
yet a proven root cause. A separate test-only patch makes three fake servers
wait until their reader observes the payload before sending completion; the
original requirement that the pong arrive while the write is held remains.
The separate immediate-terminal test is left intact. The manually dispatched
observation [passed on all three operating systems](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41489076037):
30 repeats of each unmodified fixture, 50 of each modified fixture, and 10 of
the separate immediate-terminal test passed. The original failure did not
recur in that bounded sample, so these observations do not prove its cause.
The upstream [write hook](https://github.com/router-for-me/CLIProxyAPI/blob/9717e325ba6ab2c7483ea93ccb5f2047eb2e7dd5/internal/runtime/executor/codex_websockets_session.go)
is reached before the payload is written. In the [three-platform negative control](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41490027317),
deliberately prolonging that hold produced five matching closed-connection test
failures on each platform; the fake-server barrier passed five repeats with the
same hold. Each platform also passed the unmodified bounded sample, the patched
normal sample, and the separate immediate-terminal test. This demonstrates a
possible fixture race but does not prove what caused the earlier failed run.
The candidate workflow applies the barrier only to the pinned upstream test
file; production code and binaries do not include it. The [full candidate
run](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41490731389)
subsequently passed on macOS, Linux and Windows with that test-only barrier;
the [earlier failed attempt](https://github.com/Jcstack/claude-codex-http-bridge/actions/runs/41487752563/attempts/1)
remains part of the record. These checks do not establish real WebSocket
behavior.

A separate community [controlled experiment](https://github.com/puxu-msft/copilot-api-js/blob/master/exp/cc-idle-280s/REPORT.md)
on Claude Code 2.1.201 reported an additional content-progress timeout that
comments and ping events did not reset. That result is version- and setup-specific
and has not been reproduced on this installation. Claude Code's current
[network documentation](https://code.claude.com/docs/en/network-config#streaming-idle-watchdogs)
describes separate event-, byte-, and body-idle watchdogs, with gateway defaults
of roughly five minutes and bounded extensions when bytes continue arriving.
A heartbeat can therefore delay detection of a stalled upstream even if another
watchdog eventually fires. The observed 46.8- and 215-second request durations
are not themselves measurements of idle gaps. These trade-offs require a
deliberate liveness and cancellation policy; this bridge does not fabricate
empty thinking content to extend a stream.

The official primary GPT-Live WebSocket uses `/v1/live/sessions` without a
model query, then `session.start` and `session.started`. It is distinct from
Realtime `/v1/realtime` and an existing Live call's sideband. A 2026-10-31
[local route probe](tests/probe_live_route.py) returned 404 before session start.
The pinned server registers `/v1/live/:call_id`, which does not provide that
primary-session entry point. This establishes a local protocol gap, not a
denied account entitlement; the earlier WebRTC 400 does not test the primary
WebSocket either. No microphone, playback or public project API was enabled.

Claude Code disables fine-grained tool-input streaming by default behind a custom `ANTHROPIC_BASE_URL`. On the subscription route, this launcher sets `CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING=1` unless you explicitly set it to `0`. In an isolated Claude Code 2.1.314 request capture, `1` added `eager_input_streaming: true` to all 21 built-in tool definitions; `0` added it to none. A small live GPT-6.1 Sol subscription-route run completed a Bash tool call and emitted 18 `input_json_delta` events in 32 stream events. This improves the chance of seeing a long tool input progressively; it does not mean Claude Code executes incomplete JSON. [Claude Code gateway behavior](https://code.claude.com/docs/en/llm-gateway-protocol#feature-pass-through), [fine-grained tool streaming](https://platform.claude.com/docs/en/agents-and-tools/tool-use/fine-grained-tool-streaming).

The optional direct API route leaves the flag off. The pinned translator forwards `eager_input_streaming` into the OpenAI Responses function-tool schema, where its acceptance has not been established by a live API call. A strict fake API rejects extra function-tool fields; a credential-free contract test still confirms that upstream function-argument deltas can reach Claude Code progressively when the fake API emits them. Explicitly forcing the flag on the API route fails before inference. Do not infer from the fake stream that the real API grants fine-grained tool-input generation.

The next optimization gate is to measure the same task with both harnesses and the same model: time to first visible event, event gaps, completed tool calls, task success, and cache use. A stream that starts quickly but loses a function result or breaks a subagent is a regression, not an improvement. Model effort and tool definitions should change only after those measures show a benefit.
